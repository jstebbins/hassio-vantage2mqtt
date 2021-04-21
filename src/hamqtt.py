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

    def __init__(self, mqttCfg, dry_run=False):
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
        self.subscribe_topic = "gateway/+/site/+/#"
        self.filter_topic = "gateway/+/site/+/+"
        self.on_unfiltered = None
        self.on_filtered = None
        self.on_status = None
        self._dry_run = dry_run
        self._gateway_name = "Gateway"

    def on_connect(self, client, userdata, flags, rc):
        """
        Callback for connection established
        """

        if rc != 0:
            self._log.error("MQTT connection failed %d", rc)
            return

        self._log.warning("MQTT connection established")
        # Susscribe to topics of interest
        self._client.subscribe("homeassistant/status", 1)
        self._log.warning('MQTT subscribe "%s"', self.subscribe_topic)
        self._client.subscribe(self.subscribe_topic, 1)
        self.connected = True

    def on_disconnect(self):
        """
        Callback for connection lost
        """
        self._log.warning("MQTT connection lost")
        self.connected = False

    def connect(self):
        """
        Connect to MQTT Broker, initialize callbacks, and listen for topics
        """

        self._log.warning("MQTT connect with ID %s", self._gateway_name + "Gateway")
        if self._dry_run:
            return

        if self._connect_attempted:
            self._client.reconnect()
            self._client.loop_start()
            return

        self._client = mqtt.Client(client_id=self._gateway_name + "Gateway")
        self._client.username_pw_set(self._user, self._passwd)

        # Add message subscription match callback
        if self.on_status is not None:
            self._client.message_callback_add("homeassistant/status",
                                              self.on_status)
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
        self._client.loop_start()

    def close(self):
        """
        close the MQTT client connection
        """
        if self._dry_run:
            return
        self._log.warning('MQTT disconnect"')
        self._client.disconnect()

    # Wrapper for MQTT publish, add logging
    def publish(self, topic, value, qos=0, retain=False):
        """
        Publish a message to the MQTT Broker

        param topic:  topic to publish to
        param value:  value to publish
        param qos:    MQTT qos
        param retain: receiver to retain the message
        """

        self._log.debug("publish: %s -> %s", topic, value)
        if self._dry_run:
            return
        mi = self._client.publish(topic, value, qos=qos, retain=retain)
        if mi.rc != mqtt.MQTT_ERR_SUCCESS:
            self._log.error("failed (%d) to publish: %s -> %s", mi.rc, topic, value)

class HomeAssistant(MQTTClient):
    """
    HomeAssistant protocol MQTT entity discovery adapter
    """

    # Register entities with a 'HomeAssistant' compatible controller
    def __init__(self, gateway_name, site_name, mqttCfg, dry_run=False):
        """
        param site_name: site name, used as component of MQTT topics
        param mqttCfg:   dictionary of MQTT connection settings
        """

        super().__init__(mqttCfg, dry_run)
        self._log = logging.getLogger("homeassistant")
        self._gateway_name = gateway_name
        self._site_name = site_name

        # Subscribe any entity, any OID, all commands
        # Catch-all for anything unexpected, logged when debug enabled
        self.subscribe_topic = self.gateway_topic("+", "+", "#")
        # Topic filter for all expected commands to inFusion
        self.filter_topic = self.gateway_topic("+", "+", "+")

    def topic(self, root, entity_name, oid, command):
        """
        Generic topic
        Topic format "<domain>/<entity type>/<site>/<OID>/<command>
        domain       - "homeassistant" or "<gateway_name>"
        entity types - HA entity types, e.g. switch, light
        site         - name defined by config
        OID          - Ojbect ID
        commands     - e.g. config, set, state, brightness...
        """
        return "%s/%s/%s/%s/%s" % (root, entity_name,
                                   self._site_name.lower(), oid, command)

    def discovery_topic(self, entity_name, oid, command):
        """
        Home Assistant Topics
        Format       - "homeassistant/<entity type>/<site>/<OID>/<command>
        entity types - HA entity types, e.g. switch, light
        site         - name defined by config
        OID          - Ojbect ID
        commands     - config
        """

        return self.topic("homeassistant", entity_name, oid, command)

    def gateway_topic(self, entity_name, oid, command):
        """
        Gateway Topics
        Format       - "<gateway_name>/<entity type>/<site>/<OID>/<command or status>"
        entity types - HA entity types, e.g. switch, light
        site         - name defined by config
        OID          - Ojbect ID
        commands     - vary by entity type
            switch   - set
            light    - set, brightness
        status       - vary by entity type
            switch   - state
            light    - state, brightness_state
        """

        return self.topic(self._gateway_name.lower(), entity_name, oid, command)

    def gateway_entity_state_topic(self, entity_type, oid):
        """
        Construct a state topic for this protocol
        Users of HomeAssistant class call this to obtain a properly
        formatted entity state topic for the HomeAssistant protocol
        """

        return self.gateway_topic(entity_type, oid, "state")

    @staticmethod
    def add_device_info(config, device, names):
        """
        Adds device dictionary to endity

        param config: registration config dictionary
        param device: details of device to add
        param names:  'name' for shortnames, 'fullname' for longnames
        """

        if device is None:
            return

        config["device"] = {
            "name"        : device[names]
        }
        manufacturer = device.get("manufacturer")
        model = device.get("model")
        serial = device.get("serial")
        if serial:
            config["device"]["identifiers"] = serial
        else:
            config["device"]["identifiers"] = device["uid"]
        if manufacturer:
            config["device"]["manufacturer"] = manufacturer
        if model:
            config["device"]["model"] = model

    def register_light(self, entity, device, short_names=False, flush=False):
        """
        Register a light with HomeAssistant controller

        param entity:      entity dictionary
        param short_names: use short names when registering entities
        param flush:       flush old entities settings from controller
        """

        self._log.debug("register_light")
        if entity["type"] not in ("DimmerLight", "Light"):
            return # Only lights

        oid = entity["OID"]
        topic_config = self.discovery_topic("light", oid, "config")
        if flush:
            self.publish(topic_config, "", retain=True)
            return

        if short_names:
            names = "name"
        else:
            names = "fullname"
        config = {
            "schema"                   : "json",
            "unique_id"                : entity["uid"],
            "name"                     : entity[names],
            "command_topic"            : self.gateway_topic("light", oid, "set"),
            "state_topic"              : self.gateway_topic("light", oid, "state"),
            "qos"                      : 0,
            "retain"                   : False,
        }
        if entity["type"] == "DimmerLight":
            config["brightness"] = True
            config["brightness_scale"] = 100
        else:
            config["brightness"] = False
        self.add_device_info(config, device, names)

        config_json = json.dumps(config)
        self.publish(topic_config, config_json, retain=True)

    def register_switch(self, entity, device, short_names=False, flush=False):
        """
        Register a switch with HomeAssistant controller

        param entity:      entity dictionary
        param short_names: use short names when registering entities
        param flush:       flush old entities settings from controller
        """

        self._log.debug("register_switch")
        if entity["type"] != "Switch" and entity["type"] != "Relay":
            return # Only switches

        oid = entity["OID"]
        topic_config = self.discovery_topic("switch", oid, "config")
        if flush:
            self.publish(topic_config, "", retain=True)
            return

        if short_names:
            names = "name"
        else:
            names = "fullname"
        config = {
            "unique_id"     : entity["uid"],
            "name"          : entity[names],
            "command_topic" : self.gateway_topic("switch", oid, "set"),
            "state_topic"   : self.gateway_topic("switch", oid, "state"),
            "icon"          : "mdi:light-switch",
            "qos"           : 0,
            "retain"        : False,
        }
        self.add_device_info(config, device, names)

        config_json = json.dumps(config)
        self.publish(topic_config, config_json, retain=True)
        if entity["type"] == "Relay":
            self._log.debug("Relay: %s: %s", topic_config, config_json)
        else:
            self._log.debug("Switch: %s: %s", topic_config, config_json)

    def register_motor(self, entity, device, short_names=False, flush=False):
        """
        Register a motor with HomeAssistant controller

        param entity:      entity dictionary
        param short_names: use short names when registering entities
        param flush:       flush old entities settings from controller
        """

    def register_relay(self, entity, device, short_names=False, flush=False):
        """
        Register a relay with HomeAssistant controller
        A relay just looks like another switch to Home Assistant

        param entity:      entity dictionary
        param short_names: use short names when registering entities
        param flush:       flush old entities settings from controller
        """

        self.register_switch(entity, device, short_names, flush)

    def register_entities(self, entities, objects,
                          short_names=False, flush=False):
        """
        Register entities with HomeAssistant controller

        param entities:    dictionary of entities
        param short_names: use short names when registering entities
        param flush:       flush old entities settings from controller
        """

        for _, entity in entities.items():
            device_oid = entity.get("device_oid")
            device = None
            if device_oid is not None and objects is not None:
                device = objects.get(device_oid)
            if entity["type"] == "Switch":
                self.register_switch(entity, device, short_names, flush)
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
        self.register_entities(entities, None, flush=True)

    def split_topic(self, topic):
        """
        Parse parameters from a HomeAssistant MQTT topic

        param topic: the MQTT topic
        returns:     entity type, OID, command
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
