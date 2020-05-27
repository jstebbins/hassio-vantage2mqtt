"""
HomeAssistant MQTT Discovery and entity control
"""

import json
import re
import logging
import paho.mqtt.client as mqtt

class MQTTClient:
    """
    Wrapper simplification of paho.mqtt
    Base class used by HomeAssistant
    """

    def __init__(self, mqttCfg):
        """
        param mqttCfg:   dictionary of MQTT connection settings
        """

        self._log = logging.getLogger("mqttclient")
        self._ip = mqttCfg["ip"]
        self._port = mqttCfg["port"]
        self._user = mqttCfg["username"]
        self._passwd = mqttCfg["password"]
        self._client = None
        self._connect_attempted = False
        self.connected = False
        self.subscribe_topic = "vantage/site/+/#"
        self.filter_topic = "vantage/site/+/+"
        self.on_unfiltered = None
        self.on_filtered = None

    def on_connect(self):
        """
        Callback for connection established
        """
        self._log.debug("connection established")
        self.connected = True

    def on_disconnect(self):
        """
        Callback for connection lost
        """
        self._log.debug("connection lost")
        self.connected = False

    def connect(self):
        """
        Connect to MQTT Broker, initialize callbacks, and listen for topics
        """

        self._log.debug("connect")
        if self._connect_attempted:
            self._client.reconnect()
            # Susscribe to topics of interest
            self._client.subscribe(self.subscribe_topic, 1)
            self._client.loop_start()
            return

        self._client = mqtt.Client(client_id="VantageGateway")
        self._client.username_pw_set(self._user, self._passwd)

        # Add message subscription match callback
        if self.on_filtered is not None:
            self._client.message_callback_add(self.filter_topic,
                                              self.on_filtered)
        self._log.debug('filter "%s"', self.filter_topic)

        # Handle messages without specific callbacks
        if self.on_unfiltered is not None:
            self._client.on_message = self.on_unfiltered

        self._client.on_connect = self.on_connect
        self._client.on_disconnect = self.on_disconnect

        # Connect to MQTT and wait for messages
        self._connect_attempted = True
        self._client.connect(self._ip, self._port, 60)
        # Susscribe to topics of interest
        self._log.debug('subscribe "%s"', self.subscribe_topic)
        self._client.subscribe(self.subscribe_topic, 1)
        # MQTT runs in a loop in it's own thread
        self._client.loop_start()

    def close(self):
        """
        close the MQTT client connection
        """
        self._client.disconnect()

    # Wrapper for MQTT publish, add logging
    def publish(self, topic, value, qos=1, retain=True):
        """
        Publish a message to the MQTT Broker

        param topic:  topic to publish to
        param value:  value to publish
        param qos:    MQTT qos
        param retain: receiver to retain the message
        """

        self._log.debug("publish: %s -> %s", topic, value)
        self._client.publish(topic, value, qos=qos, retain=retain)

class HomeAssistant(MQTTClient):
    """
    HomeAssistant protocol MQTT entity discovery adapter
    """

    # Register entities with a 'HomeAssistant' compatible controller
    def __init__(self, site_name, mqttCfg):
        """
        param site_name: site name, used as component of MQTT topics
        param mqttCfg:   dictionary of MQTT connection settings
        """

        super().__init__(mqttCfg)
        self._log = logging.getLogger("homeassistant")
        self._site_name = site_name

        # Subscribe any entity, any VID, all commands
        # Catch-all for anything unexpected, logged when debug enabled
        self.subscribe_topic = self.gateway_topic("+", "+", "#")
        # Topic filter for all expected commands to inFusion
        self.filter_topic = self.gateway_topic("+", "+", "+")

    def topic(self, root, entity_name, vid, command):
        """
        Generic topic
        Topic format "<domain>/<entity type>/<site>/<VID>/<command>
        domain       - "homeassistant" or "vantage"
        entity types - HA entity types, e.g. switch, light
        site         - name defined by config
        VID          - VID from Vantage Design Center config
        commands     - e.g. config, set, state, brightness...
        """
        return "%s/%s/%s/%s/%s" % (root, entity_name,
                                   self._site_name.lower(), vid, command)

    def discovery_topic(self, entity_name, vid, command):
        """
        Home Assistant Topics
        Format       - "homeassistant/<entity type>/<site>/<VID>/<command>
        entity types - HA entity types, e.g. switch, light
        site         - name defined by config
        VID          - VID from Vantage Design Center config
        commands     - config
        """

        return self.topic("homeassistant", entity_name, vid, command)

    def gateway_topic(self, entity_name, vid, command):
        """
        Gateway Topics
        Format       - "vantage/<entity type>/<site>/<VID>/<command or status>"
        entity types - HA entity types, e.g. switch, light
        site         - name defined by config
        VID          - VID from Vantage Design Center config
        commands     - vary by entity type
            switch   - set
            light    - set, brightness
        status       - vary by entity type
            switch   - state
            light    - state, brightness_state
        """

        return self.topic("vantage", entity_name, vid, command)

    def gateway_entity_state_topic(self, entity_type, vid):
        """
        Construct a state topic for this protocol
        Users of HomeAssistant class call this to obtain a properly
        formatted entity state topic for the HomeAssistant protocol
        """

        return self.gateway_topic(entity_type, vid, "state")

    def add_device_info(self, config, device, names):
        if device is not None:
            config["device"] = {
                "name"        : device[names],
                "manufacturer": "Vantage"
            }
            model = device.get("model")
            serial = device.get("serial")
            if serial:
                config["device"]["identifiers"] = serial
            else:
                config["device"]["identifiers"] = device["uid"]
            if model:
                config["device"]["model"] = model

    def register_light(self, entity, device, short_names=False, flush=False):
        """
        Register a Vantage inFusion light with HomeAssistant controller

        param entity:      entity dictionary
        param short_names: use short names when registering entities
        param flush:       flush old entities settings from controller
        """

        self._log.debug("register_light")
        if entity["type"] not in ("DimmerLight", "Light"):
            return # Only lights

        vid = entity["VID"]
        topic_config = self.discovery_topic("light", vid, "config")
        if flush:
            self.publish(topic_config, "")
            return

        if short_names:
            names = "name"
        else:
            names = "fullname"
        config = {
            "schema"                   : "json",
            "unique_id"                : entity["uid"],
            "name"                     : entity[names],
            "command_topic"            : self.gateway_topic("light", vid, "set"),
            "state_topic"              : self.gateway_topic("light", vid, "state"),
            "qos"                      : 1,
            "retain"                   : True,
        }
        self.add_device_info(config, device, names)
        if entity["type"] == "DimmerLight":
            config["brightness"] = True
            config["brightness_scale"] = 100
        else:
            config["brightness"] = False

        config_json = json.dumps(config)
        self.publish(topic_config, config_json)

    def register_button(self, entity, short_names=False, flush=False):
        """
        Register a Vantage inFusion button with HomeAssistant controller

        param entity:      entity dictionary
        param short_names: use short names when registering entities
        param flush:       flush old entities settings from controller
        """

        self._log.debug("register_button")
        if entity["type"] != "Button":
            return # Only buttons

        vid = entity["VID"]
        topic_config = self.discovery_topic("switch", vid, "config")
        if flush:
            self.publish(topic_config, "")
            return

        if short_names:
            names = "name"
        else:
            names = "fullname"
        config = {
            "unique_id"     : entity["uid"],
            "name"          : entity[names],
            "command_topic" : self.gateway_topic("switch", vid, "set"),
            "state_topic"   : self.gateway_topic("switch", vid, "state"),
            "icon"          : "mdi:lightbulb",
            "qos"           : 1,
            "retain"        : True,
        }
        self.add_device_info(config, device, names)

        config_json = json.dumps(config)
        self.publish(topic_config, config_json)

    def register_motor(self, entity, short_names=False, flush=False):
        """
        Register a Vantage inFusion motor with HomeAssistant controller

        param entity:      entity dictionary
        param short_names: use short names when registering entities
        param flush:       flush old entities settings from controller
        """

    def register_relay(self, entity, short_names=False, flush=False):
        """
        Register a Vantage inFusion relay with HomeAssistant controller

        param entity:      entity dictionary
        param short_names: use short names when registering entities
        param flush:       flush old entities settings from controller
        """

    def register_entities(self, entities, objects,
                          short_names=False, flush=False):
        """
        Register Vantage inFusion entities with HomeAssistant controller

        param entities:    dictionary of entities
        param short_names: use short names when registering entities
        param flush:       flush old entities settings from controller
        """

        for vid, entity in entities.items():
            device_vid = entity.get("device_vid")
            device = None
            if device_vid is not None:
                device = objects.get(device_vid)
            if entity["type"] == "Button":
                self.register_button(entity, device, short_names, flush)
            elif entity["type"] in ("DimmerLight", "Light"):
                self.register_light(entity, device, short_names, flush)
            elif entity["type"] == "Motor":
                self.register_motor(entity, device, short_names, flush)
            elif entity["type"] == "Relay":
                self.register_relay(entity, device, short_names, flush)

    def flush_entities(self, entities):
        """
        Flush old entity settings from HomeAssistant controller

        param entities: list of entities to flush
        """

        self._log.debug("flush_entities")
        self.register_entities(entities, flush=True)

    def split_topic(self, topic):
        """
        Parse parameters from a HomeAssistant MQTT topic

        param topic: the MQTT topic
        returns:     entity type, VID, command
        """

        pat = self.gateway_topic("([^/]+)", "([^/]+)", "([^/]+)")
        m = re.search(pat, topic)
        if m:
            return m.group(1), m.group(2), m.group(3)
        self._log.error("split_topic: match failed")
        return None, None, None

    def translate_state(self, entity_type, state):
        """
        Translates internal entity state to a HomeAssistant MQTT state value
        It just happens that internal state very closely mirrors HomeAssistant
        state values

        Other protocols, e.g. Homie, would do more here.
        """

        if entity_type == "switch":
            return state.get("state")
        if entity_type == "light":
            return json.dumps(state)
        self._log.warning('Unknown entity type "%s"', entity_type)
        return None
