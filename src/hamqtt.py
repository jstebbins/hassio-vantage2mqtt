"""
HomeAssistant MQTT Discovery and device control
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
    HomeAssistant protocol MQTT device discovery adapter
    """

    # Register devices with a 'HomeAssistant' compatible controller
    def __init__(self, site_name, mqttCfg):
        """
        param site_name: site name, used as component of MQTT topics
        param mqttCfg:   dictionary of MQTT connection settings
        """

        super().__init__(mqttCfg)
        self._log = logging.getLogger("homeassistant")
        self._site_name = site_name

        # Subscribe any device, any VID, all commands
        # Catch-all for anything unexpected, logged when debug enabled
        self.subscribe_topic = self.gateway_topic("+", "+", "#")
        # Topic filter for all expected commands to inFusion
        self.filter_topic = self.gateway_topic("+", "+", "+")

    def topic(self, root, device_name, vid, command):
        """
        Generic topic
        Topic format "<domain>/<device type>/<site>/<VID>/<command>
        domain       - "homeassistant" or "vantage"
        device types - HA device types, e.g. switch, light
        site         - name defined by config
        VID          - VID from Vantage Design Center config
        commands     - e.g. config, set, state, brightness...
        """
        return "%s/%s/%s/%s/%s" % (root, device_name,
                                   self._site_name.lower(), vid, command)

    def discovery_topic(self, device_name, vid, command):
        """
        Home Assistant Topics
        Format       - "homeassistant/<device type>/<site>/<VID>/<command>
        device types - HA device types, e.g. switch, light
        site         - name defined by config
        VID          - VID from Vantage Design Center config
        commands     - config
        """

        return self.topic("homeassistant", device_name, vid, command)

    def gateway_topic(self, device_name, vid, command):
        """
        Gateway Topics
        Format       - "vantage/<device type>/<site>/<VID>/<command or status>"
        device types - HA device types, e.g. switch, light
        site         - name defined by config
        VID          - VID from Vantage Design Center config
        commands     - vary by device type
            switch   - set
            light    - set, brightness
        status       - vary by device type
            switch   - state
            light    - state, brightness_state
        """

        return self.topic("vantage", device_name, vid, command)

    def gateway_device_state_topic(self, device_type, vid):
        """
        Construct a state topic for this protocol
        Users of HomeAssistant class call this to obtain a properly
        formatted device state topic for the HomeAssistant protocol
        """

        return self.gateway_topic(device_type, vid, "state")

    def register_light(self, device, short_names=False, flush=False):
        """
        Register a Vantage inFusion light with HomeAssistant controller

        param device:      device dictionary
        param short_names: use short names when registering devices
        param flush:       flush old devices settings from controller
        """

        self._log.debug("register_light")
        if device["type"] not in ("DimmerLight", "Light"):
            return # Only lights

        if short_names:
            names = "name"
        else:
            names = "fullname"
        vid = device["VID"]
        topic_config = self.discovery_topic("light", vid, "config")
        config = {
            "schema"                   : "json",
            "unique_id"                : device["uid"],
            "name"                     : device[names],
            "command_topic"            : self.gateway_topic("light", vid, "set"),
            "state_topic"              : self.gateway_topic("light", vid, "state"),
            "qos"                      : 1,
            "retain"                   : True,
            "device"                   : {
                "identifiers"          : device["uid"],
                "name"                 : device[names]
            }
        }
        if device["type"] == "DimmerLight":
            config["brightness"] = True
            config["brightness_scale"] = 100
        else:
            config["brightness"] = False
        config_json = json.dumps(config)
        if flush:
            self.publish(topic_config, "")
        else:
            self.publish(topic_config, config_json)

    def register_button(self, device, short_names=False, flush=False):
        """
        Register a Vantage inFusion button with HomeAssistant controller

        param device:      device dictionary
        param short_names: use short names when registering devices
        param flush:       flush old devices settings from controller
        """

        self._log.debug("register_button")
        if device["type"] != "Button":
            return # Only buttons

        if short_names:
            names = "name"
        else:
            names = "fullname"
        vid = device["VID"]
        topic_config = self.discovery_topic("switch", vid, "config")
        config = {
            "unique_id"     : device["uid"],
            "name"          : device[names],
            "command_topic" : self.gateway_topic("switch", vid, "set"),
            "state_topic"   : self.gateway_topic("switch", vid, "state"),
            "icon"          : "mdi:lightbulb",
            "qos"           : 1,
            "retain"        : True,
            "device"        : {
                "identifiers" : device["uid"],
                "name"        : device[names]
            }
        }
        config_json = json.dumps(config)
        if flush:
            self.publish(topic_config, "")
        else:
            self.publish(topic_config, config_json)

    def register_motor(self, device, short_names=False, flush=False):
        """
        Register a Vantage inFusion motor with HomeAssistant controller

        param device:      device dictionary
        param short_names: use short names when registering devices
        param flush:       flush old devices settings from controller
        """

    def register_relay(self, device, short_names=False, flush=False):
        """
        Register a Vantage inFusion relay with HomeAssistant controller

        param device:      device dictionary
        param short_names: use short names when registering devices
        param flush:       flush old devices settings from controller
        """

    def register_devices(self, devices, short_names=False, flush=False):
        """
        Register Vantage inFusion devices with HomeAssistant controller

        param devices:     dictionary of devices
        param short_names: use short names when registering devices
        param flush:       flush old devices settings from controller
        """

        for vid, device in devices.items():
            if device["type"] == "Button":
                self.register_button(device, short_names, flush)
            elif device["type"] in ("DimmerLight", "Light"):
                self.register_light(device, short_names, flush)
            elif device["type"] == "Motor":
                self.register_motor(device, short_names, flush)
            elif device["type"] == "Relay":
                self.register_relay(device, short_names, flush)

    def flush_devices(self, devices):
        """
        Flush old device settings from HomeAssistant controller

        param devices: list of devices to flush
        """

        self._log.debug("flush_devices")
        self.register_devices(devices, flush=True)

    def split_topic(self, topic):
        """
        Parse parameters from a HomeAssistant MQTT topic

        param topic: the MQTT topic
        returns:     device type, VID, command
        """

        pat = self.gateway_topic("([^/]+)", "([^/]+)", "([^/]+)")
        m = re.search(pat, topic)
        if m:
            return m.group(1), m.group(2), m.group(3)
        self._log.error("split_topic: match failed")
        return None, None, None

    def translate_state(self, device_type, state):
        """
        Translates internal device state to a HomeAssistant MQTT state value
        It just happens that internal state very closely mirrors HomeAssistant
        state values

        Other protocols, e.g. Homie, would do more here.
        """

        if device_type == "switch":
            return state.get("state")
        if device_type == "light":
            return json.dumps(state)
        self._log.warning('Unknown device type "%s"', device_type)
        return None
