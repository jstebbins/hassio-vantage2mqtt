#!/usr/bin/env python
"""
Vantage inFusion to MQTT Gateway
"""

import sys
import getopt
import socket
import base64
import json
import string
import xml.etree.ElementTree as ET
import re
import io
import time
import logging
import asyncio
import jsonschema
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
        self._ip = mqttCfg["network"]["ip"]
        self._port = mqttCfg["network"]["port"]
        self._user = mqttCfg["auth"]["user"]
        self._passwd = mqttCfg["auth"]["password"]
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

class InFusionConfig:
    """
    Initializes Vantage inFusion device dictionary
    """
    SOCK_BUFFER_SIZE = 8192

    # Reads Design Center configuration and creates dictionary of
    # devices to be provided during MQTT discovery
    def __init__(self, cfg, devicesFile=None, xml_config_file=None):
        """
        Reads Vantage inFusion configuration from 1 of 3 locations:
            1. Try json cache file
            2. Try Design Center xml cache file
            3. Read Design Center xml backup from inFuion config port

        param cfg:             dictionary of Vantage inFusion settings
        param devicesFile:     json cache file to read devices from
        param xml_config_file: xml cache file to read devices from
        """

        self._log = logging.getLogger("vantage")
        # Vantage TCP access
        ip = cfg["network"]["ip"]
        port = cfg["network"]["config_port"]

        self.updated = False
        self.devices = None
        self.dc_valid = False
        self.infusion_memory_valid = False
        self.objects = None

        enabled_devices = self.get_enabled_devices(cfg)

        # Read configured devices
        self._log.debug("Reading devices configureation...")
        if devicesFile:
            try:
                self._log.debug("Try devices configuration %s", devicesFile)
                with open(devicesFile) as fp:
                    self.objects = json.load(fp)
                    fp.close()
            except:
                pass
        if self.objects:
            self.devices = self.filter_objects(enabled_devices, self.objects)
            return # Valid devices configuration found

        # Prefer Design Center configuration file if available.
        # Reading it is faster than downloading Vantage inFusion memory card
        if xml_config_file:
            try:
                self._log.debug("Try Design Center configuration %s", xml_config_file)
                xml_tree = ET.parse(xml_config_file)
                xml_root = xml_tree.getroot()
                self.xml_config = ET.tostring(xml_root)
                self.objects = self.create_objects(xml_root)
            except Exception as err:
                self._log.warning("Failed to parse %s, %s",
                                  xml_config_file, str(err))

        if self.objects is None:
            # Try Vantage inFusion memory card
            try:
                self._log.debug("Try inFusion Memory card ip %s:%d", ip, port)
                self.xml_config = self.read_infusion_memory(ip, port)
                xml_root = ET.fromstring(self.xml_config)
                # Parse Design Center configuration into devices that will
                # be provided during MQTT discovery
                self.objects = self.create_objects(xml_root)
                if self.objects is not None:
                    self.infusion_memory_valid = True
            except Exception as err:
                self._log.warning("Failed to read memory card, %s", str(err))
                raise Exception("No valid Design Center configuration found")
        else:
            self.dc_valid = True

        # Filter out the devices we do not want to enable
        self.devices = self.filter_objects(enabled_devices, self.objects)

        self.site_name = self.lookup_site(self.objects)
        self.updated = True

    def get_enabled_devices(self, cfg):
        """
        Creates a list of enabled devices from config booleans

        param cfg: configuration dictionary
        returns:   list of Vantage Design Center device types
        """

        enabled_devices = []
        if cfg.get("buttons"):
            enabled_devices.append("Button")
        if cfg.get("lights"):
            enabled_devices.append("Light")
            enabled_devices.append("DimmerLight")
        if cfg.get("motors"):
            enabled_devices.append("Motor")
        if cfg.get("relays"):
            enabled_devices.append("Relay")
        return enabled_devices

    # Connect to Vantage inFusion configuration port and read
    # the configuration.
    #
    # The data read is a base64 encoded string encapsulated
    # in a simple XML structure.  The configuration itself is
    # XML after decoding the base64 string.
    #
    # returns: the decoded XML configuration
    def read_infusion_memory(self, ip, port):
        """
        Connect to inFusion TCP config port, read Design Center xml

        param ip:   IP address of inFusion
        param port: config port
        returns: Design Center xml string
        """

        if ip is not None and port is not None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((ip, port))
            sock.send(
                "<IBackup><GetFile><call>Backup\\Project.dc"
                "</call></GetFile></IBackup>\n".encode("ascii"))
            sock.settimeout(1)
            size = 0
            config = bytearray()
            try:
                while True:
                    # receive data from Vantage
                    data = sock.recv(self.SOCK_BUFFER_SIZE)
                    if not data:
                        break
                    size += len(data)
                    config.extend(data)
            except socket.timeout:
                sock.close()
            except:
                self._log.critical(
                    "Error reading config from IP %s port %d", ip, port)
                raise

            config = config.decode('ascii')
            config = config[config.find('<?File Encode="Base64" /'):]
            config = config.replace('<?File Encode="Base64" /', '')
            config = config[:config.find('</return>'):]
            config = base64.b64decode(config)
            return config.decode('utf-8')
        raise Exception("IP and port required to read inFusion memory")

    # Build object dictionary
    #
    # Object dictionary consists of all objects that have
    # valid VID, Parent or Area, and Name
    #
    # This dictionary only used as an intermediate data structure
    # to simplify and speed up looking up objects in the
    # Design Center configuration file
    #
    # returns: a dictionary of objects found
    #          Dictionary keys are Design Center object IDs (VID)
    def create_objects(self, xml_root):
        """
        Create a dictionary of Design Center objects from an xml tree

        param xml_root: the xml tree
        returns: dictionary of objects
        """
        self._log.debug("create_objects")
        objects = {}
        for item in xml_root:
            print(item)
            if item.tag == "Objects":
                for obj in item.iter(None):
                    vid = obj.get('VID')
                    if not vid:
                        continue
                    name = obj.find('Name').text
                    if obj.tag == "Load":
                        load_type = obj.find('LoadType').text
                        if load_type in ("Incandescent", "Halogen", "LED Dim",
                                         "Flour. Electronic Dim"):
                            obj_type = "DimmerLight"
                        elif load_type in ("LED non-Dim",
                                           "Flour. Electronic non-Dim",
                                           "Cold Cathode"):
                            obj_type = "Light"
                        elif load_type == "Motor":
                            obj_type = "Motor"
                        elif load_type in ("Low Voltage Relay",
                                           "High Voltage Relay"):
                            obj_type = "Relay"
                    else:
                        obj_type = obj.tag
                    name = obj.find('Name').text
                    parent = obj.find('Area')
                    if parent is None:
                        parent = obj.find('Parent')
                    if parent is not None and name:
                        objects[vid] = {"VID" : vid, "type" : obj_type,
                                        "parent" : parent.text, "name" : name}
        return objects

    # Eliminate leading/trailing whitespace, make lowercase, replace
    # punctuation and internal spaces with '_'
    #
    # returns: string
    def uidify(self, name):
        """
        Make a string suitable for using as a 'unique_id'
        Substitutes punctuation and white space to '_'

        param name: string to translate
        returns:    string
        """

        subchars = string.punctuation + " "
        trans = str.maketrans(subchars, '_'*len(subchars))
        return name.strip().lower().translate(trans)

    # Construct a full name from the item name and the "Area" hierarchy names
    #
    # returns: string
    def create_fullname(self, objects, vid):
        """
        Creates a long name from a device name and the heirarchy of
        Design Center 'Area' objects it is contained in

        param objects: dictionary of Design Center objects
        param vid:     Design Center device ID
        returns:       unique_id, long name string
        """

        fullname = name = objects[vid]["name"]
        fulluid = self.uidify(name)
        next_vid = objects[vid]["parent"]
        while next_vid in objects:
            # If Area and not root of hierarchy, prepend Area name
            if (objects[next_vid]["type"] == "Area" and
                    objects[next_vid]["parent"] in objects):
                name = objects[next_vid]["name"]
                fullname = name + ", " + fullname
                fulluid = self.uidify(name) + "." + fulluid
            next_vid = objects[next_vid]["parent"]
        fulluid = fulluid + "." + vid
        return (fulluid, fullname)

    # Build device dictionary
    #
    # Device names are not unique, so a unique device id (uid) is built
    # by prefixing it's name with the "Area" hierarchy and sufixing with the
    # Design Center object ID.
    #
    # returns: a dictionary of devices found
    #          Dictionary keys are Design Center object IDs (VID)
    def filter_objects(self, device_type_list, objects):
        """
        Create a dictionary of devices from a dictionary of Design Center objects
        by filtering based on object 'type'

        param objects: dictionary of Design Center objects
        returns:       dictionary of devices
        """

        devices = {}
        unique_devices = {}
        for vid, item in objects.items():
            uid, fullname = self.create_fullname(objects, vid)
            item["fullname"] = fullname
            item["uid"] = uid
            if item["type"] in device_type_list:
                unique_devices[uid] = item
        for uid, item in unique_devices.items():
            vid = item["VID"]
            devices[vid] = item
        return devices

    # Find the name of the root object in the Design Center configuration.
    # This will be used as the site name if not provided in this modules
    # configuration file.
    #
    # returns: string
    def lookup_site(self, objects):
        """
        Traverses to top most 'Area' object in Design Center object
        heirarchy and returns it's name as the site name

        param objects: dictionary of Design Center objects
        returns:       site name string
        """

        site_name = "Default"
        vid = next(iter(self.devices.values()))["VID"]
        next_vid = objects[vid]["parent"]
        while next_vid in objects:
            if objects[next_vid]["type"] == "Area":
                site_name = objects[next_vid]["name"]
            next_vid = objects[next_vid]["parent"]
        return site_name

class InFusionServer(asyncio.Protocol):
    """
    Vantage inFusion host command handler
    Establishes TCP connection to inFusion host command port.
    Sends commands and receives status.
    """

    def __init__(self, tcpCfg):
        """
        param tcpCfg: dictionary of Vantage inFusion connection settings
        """

        self._log = logging.getLogger("infusion")
        # Vantage TCP access
        self._ip = tcpCfg["network"]["ip"]
        self._port = tcpCfg["network"]["command_port"]

        # Callbacks
        self.on_state = None
        self.on_unhandled = None

        # Vantage returns more than one line per recv
        # Buffer the data so we can read on line at a time
        self.state_buf = io.BytesIO()

        self._transport = None
        self.connection = None
        self.connected = False
        self.connected_future = None
        self.connection_lost_future = None

    def connection_made(self, transport):
        """
        Connection has been established to inFusion
        Tell inFusion what status we require
        """

        self._log.debug("connection_made")
        self._transport = transport
        # Mark future as completed
        self.connected = True
        self.connected_future.set_result(None)

    def connection_lost(self, exc):
        """
        Connection has been lost to inFusion
        """

        self._log.debug("connection_lost")
        if exc is not None:
            self._log.debug("Connection error: %s", str(exc))
        # Mark future as completed
        self.connected = False
        self.connection_lost_future.set_result(None)

    async def connect(self):
        """
        Connect to inFuision, initialize callbacks, and listen status
        """

        self._log.debug("connect")
        # establish TCP connection to Vantage inFusion
        loop = asyncio.get_running_loop()
        self.connected_future = loop.create_future()
        self.connection_lost_future = loop.create_future()
        self.connection = await loop.create_connection(lambda: self,
                                                       self._ip, self._port)

    def close(self):
        """
        Close the connection to inFusion
        """
        self._transport.close()

    def send_command(self, command):
        """
        Send a host command to Vantage inFusion

        param command: the command string
        """

        if self._transport is None:
            self._log.warning('Transport not ready: message "%s"', command)
            return
        self._log.debug("Vantage Command: %s", command)
        self._transport.write(bytes(command + "\r", 'utf-8'))

    def decode_button_state(self, line):
        """
        Decode a status message from Vantage inFusion

        param line: the status line to decode
        returns:    vid, state
        """

        if line:
            pat = "(S:|R:GET)LED ([0-9]+) ([0-1]) .*"
            m = re.search(pat, line)
            if m:
                vid = m.group(2)
                state = "OFF"
                if m.group(3) == "1":
                    state = "ON"
                return vid, {"state" : state}
        return None, None

    def decode_load_state(self, line):
        """
        Decode a status message from Vantage inFusion

        param line: the status line to decode
        returns:    vid, state
        """

        if line:
            pat = "(S:|R:GET)LOAD ([0-9]+) ([0-9]+)\\.[0-9]*"
            m = re.search(pat, line)
            if m:
                vid = m.group(2)
                state = "OFF"
                brightness = int(m.group(3))
                if brightness > 0:
                    state = "ON"
                return vid, {"brightness" : brightness, "state" : state}
        return None, None

    def decode_state(self, line):
        """
        Decode a status message from Vantage inFusion

        param line: the status line to decode
        returns:    device_type, vid, state
        """

        vid, state = self.decode_button_state(line)
        if vid and state:
            return "switch", vid, state
        vid, state = self.decode_load_state(line)
        if vid and state:
            return "light", vid, state
        return None, None, None

    def data_received(self, data):
        """
        Vantage inFusion status reading thread
        Reads status lines in a loop and calls notification callbacks
        """

        self.state_buf.seek(0)
        self.state_buf.truncate(0)
        self.state_buf.write(data)
        self.state_buf.seek(0)
        line = self.state_buf.readline()
        while line:
            line = line.decode('utf-8').rstrip()
            self._log.debug("line: %s", line)
            device_type, vid, state = self.decode_state(line)
            if device_type and vid and state:
                if self.on_state is not None:
                    self.on_state(device_type, vid, state)
            else:
                if self.on_unhandled is not None:
                    self.on_unhandled(line)
            line = self.state_buf.readline()

class VantageGateway:
    """
    Vantage inFusion to MQTT gateway
    """
    PROTO_UNKNOWN = 0
    PROTO_HOMIE = 1
    PROTO_HOMEASSISTANT = 2

    def __init__(self, cfg, devices, protocol):
        """
        param cfg:      dictionary of gateway settings
        param protocol: MQTT device discovery protocol to use
        """

        self._log = logging.getLogger("gateway")
        try:
            self.protocol = protocol
            self.cfg = cfg
            self._site_name = cfg["vantage"]["location"]["name"]
        except Exception as err:
            self._log.critical("Configuration file Error: %s", str(err))
            sys.exit(1)

        if self.protocol == self.PROTO_HOMEASSISTANT:
            self._proto = HomeAssistant(self._site_name, cfg["mqtt"])
        self._proto.on_filtered = self.on_mqtt_filtered
        self._proto.on_unfiltered = self.on_mqtt_unfiltered
        self._min_reconnect_interval = 1
        self._max_reconnect_interval = 120

        self._infusion = InFusionServer(cfg["vantage"])
        self._infusion.on_state = self.on_vantage_state
        self._infusion.on_unhandled = self.on_vantage_unhandled
        self._devices = devices

        # The controller likes to toggle things around for a bit
        # Wait till we have updated the controller with full status
        # before allowing it to change values
        self.wait_for_settle = True

    def switch_command(self, vid, command):
        """
        process MQTT command for a switch
        """

        device = self._devices.get(vid)
        if vid not in self._devices or command == "status":
            return
        # Validate device is a button
        if device["type"] != "Button":
            return

        self._log.debug("switch command: %s %s", command, vid)
        if command == "set":
            self._infusion.send_command("BTN %s" % vid)

    def light_command(self, vid, command, param):
        """
        process MQTT command for a switch
        """

        device = self._devices.get(vid)
        if not device or command == "status":
            return
        # Validate device is a light
        if device["type"] not in ("Light", "DimmerLight"):
            return

        self._log.debug("light command: %s %s %s", vid, command, str(param))
        if command == "set":
            try:
                value = json.loads(param)
            except json.JSONDecodeError as err:
                self._log.error("light: JSON error: %s", str(err))
                return

            brightness = value.get("brightness")
            state = value.get("state")
            transition = value.get("transition")
            if state == "OFF":
                brightness = 0
            if state == "ON" and not brightness:
                brightness = 100
            if device["type"] == "DimmerLight" and transition:
                self._infusion.send_command("RAMPLOAD %s %d %d" %
                                            (vid, brightness, transition))
            else:
                self._infusion.send_command("LOAD %s %d" % (vid, brightness))

    # Translate MQTT command and send to vantage
    def on_mqtt_filtered(self, mosq, obj, msg):
        """
        MQTT callback for 'button set' topics

        param mosq: ignored
        param obj:  ignored
        param msg:  contains the MQTT topic and value
        """

        value = str(msg.payload.decode("utf-8"))
        self._log.debug(">vantage: %s - %s", msg.topic, value)
        if self.wait_for_settle:
            return
        device_type, vid, command = self._proto.split_topic(msg.topic)
        if not vid or not command:
            return
        if device_type == "switch":
            self.switch_command(vid, command)
        elif device_type == "light":
            self.light_command(vid, command, value)
        else:
            self._log.warning('Unknown device type "%s"', device_type)

    def on_mqtt_unfiltered(self, mosq, obj, msg):
        """
        MQTT callback for topics not handled by any other callbacks

        param mosq: ignored
        param obj:  ignored
        param msg:  contains the MQTT topic and value
        """

        mqttmsg = str(msg.payload.decode("utf-8"))
        self._log.debug(">unknown: %s - %s", msg.topic, mqttmsg)

    def on_vantage_state(self, device_type, vid, state):
        """
        Vantage callback for button status

        param vid:   Vanntage inFusion device ID
        param onoff: button on/off state
        """

        topic = self._proto.gateway_device_state_topic(device_type, vid)
        value = self._proto.translate_state(device_type, state)
        self._proto.publish(topic, value)

    def on_vantage_unhandled(self, line):
        """
        Vantage callback for status not handled by any other callbacks
        """
        self._log.debug(">vantage unknown: %s", line)

    def update_state(self, devices):
        """
        Update the controllers state for all buttons
        """

        for vid, item in devices.items():
            if item["type"] == "Button":
                self._infusion.send_command("GETLED %s" % vid)
            elif item["type"] in ("DimmerLight", "Light"):
                self._infusion.send_command("GETLOAD %s" % vid)

    def connect_mqtt(self):
        """
        Reconnection loop for starting MQTT connection
        """

        reconnect_interval = self._min_reconnect_interval
        while True:
            try:
                # Connect to MQTT broker
                # Once connected, it will handle reconnects automatically
                self._proto.connect()
                return
            except OSError as err:
                print("MQTT connect error, retrying: %s", str(err))
                time.sleep(reconnect_interval)
                if reconnect_interval < self._max_reconnect_interval:
                    reconnect_interval *= 2

    async def connect_infusion(self):
        """
        Reconnection loop for starting inFusion connection
        """

        # establish TCP connection to Vantage inFusion
        # catch connection related exceptions so we can retry as appropriate
        reconnect_interval = self._min_reconnect_interval
        while True:
            try:
                await self._infusion.connect()
                return
            except OSError as err:
                print("inFusion connect error, retrying: %s", str(err))
                time.sleep(reconnect_interval)
                if reconnect_interval < self._max_reconnect_interval:
                    reconnect_interval *= 2

    def enable_status(self):
        """
        Enable the status messages we require from inFusion
        """

        self._log.debug("enable_status")
        if self.cfg["vantage"].get("buttons"):
            self._infusion.send_command("status LED")
        if self.cfg["vantage"].get("lights"):
            self._infusion.send_command("status LOAD")

    async def _connect(self, short_names=False, flush=False):
        """
        Connect to MQTT broker and Vantage inFusion TCP port.
        Register Vantage inFusion devices with controller.
        Listen for MQTT topics from controller and publish status.
        Forward commands from controller to inFusion and listen for status.

        param short_names: use short names when registering devices
        param flush:       flush old device settings when registering
        """

        self._log.debug("_connect")
        # Connect to inFusion and MQTT broker
        # These will loop until connection is made
        await self.connect_infusion()
        # Wait for inFusion to be ready for us to send it commands
        # and retrieve status
        await self._infusion.connected_future
        self.connect_mqtt()

        # Enable status feedback for button LEDs
        self.enable_status()
        # If requested on the command line, flush old devices from controller
        if flush:
            self._proto.flush_devices(self._devices)
        # Register inFusion devices with controller through MQTT broker
        self._proto.register_devices(self._devices, short_names)
        # Read current state of devices from inFusion and send to MQTT broker
        self.update_state(self._devices)
        time.sleep(1)
        self.wait_for_settle = False
        self._log.debug("ready")

        # Run forever, or at least until an exception occurs
        while True:
            await self._infusion.connection_lost_future
            self._log.warning("Connection to inFusion lost, reconnecting")
            # Connection to inFusion lost, try reconnecting
            self.connect_infusion()
            # Enable status feedback for button LEDs
            self._infusion.send_command("status LED")
            # Refresh state of devices
            self.update_state(self._devices)

    def connect(self, short_names=False, flush=False):
        """
        Kick off asyncio
        """
        asyncio.run(self._connect(short_names, flush))

    def close(self):
        """
        close the gateway
        """
        self._infusion.close()
        self._proto.close()

class Main:
    """
    Command line processing and top level control for the gateway
    """

    # Module configuration filename
    CONFIG_FILENAME = "vantage.json"
    DEVICES_FILENAME = "vantage-devices.json"

    USAGE = (
        "%s [-v -h]\n"
        "    -h --help       - show this message\n"
        "    -w --write      - Save design center XML read from controller\n"
        "    -f --flush      - Flush Home Assistant controller config\n"
        "    --homeassistant - Use Home Assistant discovery protocol (default)\n"
        "    -s --shortnames - Use short devices names\n"
        "    -v --verbose    - be verbose\n"
        "    -d --debug      - be verbose\n")

    def usage(self):
        """
        Show usage message
        """

        print(self.USAGE % sys.argv[0])

    # Schema defining the format and required fields of this modules
    # configuration file.
    gatewayConfigSchema = {
        "type" : "object",
        "properties" : {
            "vantage" : {
                "type" : "object",
                "properties" : {
                    "location" : {
                        "type" : "object",
                        "properties" : {
                            "name" : {"type" : "string"}
                        },
                        "required" : ["name"],
                        "additionalProperties" : False
                    },
                    "network" : {
                        "type" : "object",
                        "properties" : {
                            "ip"           : {"type" : "string"},
                            "command_port" : {"type" : "number"},
                            "config_port"  : {"type" : "number"}
                        },
                        "required" : ["ip", "command_port", "config_port"],
                        "additionalProperties" : False
                    },
                    "dcconfig" : {"type" : "string"},
                    "dccache" : {"type" : "boolean"},
                    "debug" : {"type" : "boolean"},
                    "short-names" : {"type" : "boolean"},
                    "buttons" : {"type" : "boolean"},
                    "lights" : {"type" : "boolean"},
                    "motors" : {"type" : "boolean"},
                    "relays" : {"type" : "boolean"}
                },
                "required" : ["network"],
                "additionalProperties" : False
            },
            "mqtt" : {
                "type" : "object",
                "properties" : {
                    "auth" : {
                        "type" : "object",
                        "properties" : {
                            "user" : {"type" : "string"},
                            "password" : {"type" : "string"}
                        },
                        "required" : ["user", "password"],
                        "additionalProperties" : False,
                    },
                    "network" : {
                        "type" : "object",
                        "properties" : {
                            "ip" : {"type" : "string"},
                            "port" : {"type" : "number"}
                        },
                        "required" : ["ip", "port"],
                        "additionalProperties" : False
                    }
                },
                "required" : ["network"],
                "additionalProperties" : False
            }
        },
        "required" : ["vantage", "mqtt"],
        "additionalProperties" : False
    }

    # Parse command line arguments
    def __init__(self, argv):
        """
        Command line argument parsing

        param argv: command line arguments
        """

        self._log = logging.getLogger("main")
        try:
            opts, args = getopt.getopt(
                argv[1:], "hvdcw:fs",
                ["help", "verbose", "debug", "cache", "write=",
                 "flush", "homeassistant", "shortnames"])

        except getopt.GetoptError:
            self.usage()
            sys.exit(2)

        self.verbose = None
        self.dc_save = None
        self.cache_dc = None
        self.flush_devices = False
        self.protocol = VantageGateway.PROTO_HOMEASSISTANT
        self.use_short_device_names = True
        for opt, arg in opts:
            if opt in ("-h", "--help"):
                self.usage()
                sys.exit()
            elif opt in ("-v", "--verbose"):
                self.verbose = logging.INFO
            elif opt in ("-d", "--debug"):
                self.verbose = logging.DEBUG
            elif opt in ("-w", "--write"):
                self.dc_save = arg
            elif opt in ("-c", "--cache"):
                self.cache_dc = True
            elif opt in ("-f", "--flush"):
                self.flush_devices = True
            elif opt in ("-s", "--shortnames"):
                self.use_short_device_names = True
            else:
                self.usage()
                sys.exit(2)
        if self.verbose is None:
            logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        else:
            logging.basicConfig(stream=sys.stderr, level=self.verbose)

    def read_config(self):
        """
        Open gateway config file
        It specifies:
          Vantage inFusion network connection settings
          Vantage Design Center configuration file name
          MQTT Broker network connection settings
        """

        cfg = None
        try:
            with open(self.CONFIG_FILENAME) as fp:
                cfg = json.load(fp)
                jsonschema.validate(cfg, self.gatewayConfigSchema)
        except jsonschema.exceptions.ValidationError as err:
            self._log.critical("Invalid configuration file: %s", str(err))
            sys.exit(1)
        except Exception as err:
            self._log.critical("Error: %s", str(err))
            raise
        fp.close()
        return cfg

    def read_vantage_config(self, cfg):
        """
        Read Vantage Design Center configuration and parse out the
        elements that will be communicated in the MQTT device discovery
        phase
        """

        try:
            dc = cfg["vantage"].get("dcconfig")
            return InFusionConfig(cfg["vantage"], self.DEVICES_FILENAME, dc)
        except Exception as err:
            self._log.critical("Error: %s", str(err))
            sys.exit(1)

    def write_design_center_config(self, cfg, vantage_cfg):
        """
        If requested on the command line, write the Vantage
        Design Center configuration out to a file
        """

        # If started in caching mode and dcconfig was provided,
        # write the Design Center configuration that was read
        # from the Vantage inFusion memory card to dcconfig file
        dc = cfg["vantage"].get("dcconfig")
        if self.cache_dc and dc and vantage_cfg.infusion_memory_valid:
            with open(dc, "w") as fp:
                fp.write(vantage_cfg.xml_config)
            fp.close()

        # If command line arg -w/--write was provided,
        # write the Design Center configuration that was read
        # from the Vantage inFusion memory card to the file
        # provided on the command line
        if self.dc_save:
            with open(self.dc_save, "w") as fp:
                fp.write(vantage_cfg.xml_config)
            fp.close()

    def write_devices_config(self, devices):
        """
        Save the discovered devices to a file
        """

        # Save devices parsed from Design Center configuration
        # to our devices configuration file
        try:
            with open(self.DEVICES_FILENAME, "w") as fp:
                json.dump(devices, fp, indent=4, sort_keys=True)
        except:
            self._log.warning(
                "Warning: failed to update %s", self.DEVICES_FILENAME)
        fp.close()

    def launch_gateway(self, cfg, devices):
        """
        Start the gateway
        """

        gateway = VantageGateway(cfg, devices, self.protocol)
        try:
            if not self.use_short_device_names:
                self.use_short_device_names = cfg["vantage"].get("short-names")
            gateway.connect(self.use_short_device_names, self.flush_devices)
        except KeyboardInterrupt:
            self._log.info("Stopped by user")
        except Exception as err:
            self._log.error("Error: %s", str(err))
            raise

    def run(self):
        """
        Initialize the gateway and launch it
        """

        # Read gateway configuration file
        cfg = self.read_config()
        if self.verbose is None:
            if cfg["vantage"].get("debug"):
                logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
        if self.cache_dc is None:
            self.cache_dc = cfg["vantage"].get("dccache")

        # Read Vantage inFusion configuration,
        # may come from inFusion memory card
        vantage_cfg = self.read_vantage_config(cfg)

        # Write Vantage inFusion configuration to file, if requested
        self.write_design_center_config(cfg, vantage_cfg)

        # Use Vantage Design Center confguration root object name
        # for site name if not set in gateway configuration file
        if (cfg["vantage"].get("location") is None or
                not cfg["vantage"]["location"].get("name")):
            cfg["vantage"]["location"] = {}
            cfg["vantage"]["location"]["name"] = vantage_cfg.site_name

        # If we read a new Vantage inFusion configuration, update
        # the devices file
        if vantage_cfg.updated:
            self.write_devices_config(vantage_cfg.objects)

        # Launch the gateway
        self.launch_gateway(cfg, vantage_cfg.devices)

### Main programm
if __name__ == '__main__':
    main = Main(sys.argv)
    main.run()
