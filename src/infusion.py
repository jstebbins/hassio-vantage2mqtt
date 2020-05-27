"""
Vantage inFusion control
Reads Design Center configuration
Captures status changes
Permits controlling lights and buttons
"""

import socket
import base64
import json
import time
import string
import xml.etree.ElementTree as ET
import re
import io
import logging
import asyncio
from zeroconf import ServiceBrowser, Zeroconf, ServiceStateChange, DNSAddress

def handle_zeroconf_service_state_change(zeroconf, service_type, name, state):
    """
    zeroconf service change handler

    param zeroconf:     Zeroconf instance
    param service_type: zeroconf service type
    param name:         service name
    param state:        new service state
    """

    log = logging.getLogger()
    log.debug("state %s", str(state))
    if state is not ServiceStateChange.Added:
        return None, None

    ip = port = None
    info = zeroconf.get_service_info(service_type, name)
    for a in info.addresses:
        log.debug("check IP: %d.%d.%d.%d", a[0], a[1], a[2], a[3])
        if not ip or ip.startswith("169.254."):
            ip = "%d.%d.%d.%d" % (a[0], a[1], a[2], a[3])
            log.debug("set %s", ip)
    port = info.port

    # info.addresses only contains link-local address for some reason
    # search for a non-link-local address
    # link-local is often not routed
    records = zeroconf.cache.entries_with_name(info.server)
    log.debug(records)
    for r in records:
        if isinstance(r, DNSAddress):
            a = r.address
            log.debug("check IP: %d.%d.%d.%d", a[0], a[1], a[2], a[3])
            if not ip or ip.startswith("169.254."):
                ip = "%d.%d.%d.%d" % (a[0], a[1], a[2], a[3])
                log.debug("set %s", ip)

    log.debug("IP: %s:%d", ip, port)
    return ip, port

class ConfigException(Exception):
    """
    ConfigException raised when an invalid configuration is given
    """

class InFusionException(Exception):
    """
    InFusionException raised when an error occurs negociating with inFusion
    """

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

        self._log = logging.getLogger("InFusionConfig")
        self._log.debug("InFusionConfig init")

        # Vantage TCP access
        ip = cfg.get("ip")
        port = cfg.get("config_port")
        zeroconf = cfg.get("zeroconf")
        if not zeroconf and (not ip or port is None):
            raise ConfigException("Zeroconf or IP/Port is required")

        self.updated = False
        self.entities = None
        self.infusion_memory_valid = False
        self.objects = None

        enabled_devices = self.get_enabled_devices(cfg)

        # Read configured devices
        self._log.debug("Reading devices configuration...")
        if devicesFile:
            try:
                self._log.debug("Try devices configuration %s", devicesFile)
                with open(devicesFile) as fp:
                    self.objects = json.load(fp)
                    fp.close()
            except json.JSONDecodeError as err:
                self._log.warning("Failed to parse %s, %s",
                                  devicesFile, str(err))
            except IOError as err:
                self._log.warning("Failed read %s, %s", devicesFile, str(err))
        if self.objects:
            self.entities = self.filter_objects(enabled_devices, self.objects)
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
            except ET.ParseError as err:
                self._log.warning("Failed to parse %s, %s",
                                  xml_config_file, str(err))

        if self.objects is None:
            # Try Vantage inFusion memory card
            if zeroconf:
                self._log.debug("Lookup _aci service via zeroconf")
                zc = Zeroconf()
                browser = ServiceBrowser(zc, "_aci._tcp.local.",
                                         handlers=[self.on_zeroconf_service_state_change])
                timeout = 10
                self._reading = False
                while self.objects is None and (self._reading or timeout):
                    time.sleep(1)
                    if not self._reading:
                        timeout -= 1
                zc.close()
                if self.objects is None:
                    raise InFusionException("Failed to read inFusion memory")
            else:
                self.read_infusion_memory(ip, port)

        # Filter out the devices we do not want to enable
        self.entities = self.filter_objects(enabled_devices, self.objects)

        self.site_name = self.lookup_site(self.objects)
        self.updated = True
        self._log.debug("InFusionConfig init complete")

    def on_zeroconf_service_state_change(self, zeroconf, service_type, name, state_change):
        """
        zeroconf service change callback

        param zeroconf:     Zeroconf instance
        param service_type: zeroconf service type
        param name:         service name
        param state_change: new service state
        """

        self._log.debug("zeroconf service change")
        ip, port = handle_zeroconf_service_state_change(zeroconf, service_type,
                                                        name, state_change)

        if ip and port and self.objects is None:
            try:
                self.read_infusion_memory(ip, port)
            except InFusionException as err:
                self._log.warning("Error reading inFusion memory: %s", err)

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

        self._log.debug("Try inFusion Memory card ip %s:%d", ip, port)
        if ip is not None and port is not None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((ip, port))
            sock.send(
                "<IBackup><GetFile><call>Backup\\Project.dc"
                "</call></GetFile></IBackup>\n".encode("ascii"))
            sock.settimeout(1)
            size = 0
            config = bytearray()
            self._reading = True
            try:
                while True:
                    # receive data from Vantage
                    data = sock.recv(self.SOCK_BUFFER_SIZE)
                    if not data:
                        break
                    size += len(data)
                    self._log.debug("read size %d", size)
                    config.extend(data)
            except socket.timeout:
                sock.close()
            except OSError as err:
                self._reading = False
                sock.close()
                raise InFusionException(
                    "Error reading inFusion config: %s" % str(err))

            config = config.decode('ascii')
            config = config[config.find('<?File Encode="Base64" /'):]
            config = config.replace('<?File Encode="Base64" /', '')
            config = config[:config.find('</return>'):]
            config = base64.b64decode(config)
            self.xml_config = config.decode('utf-8')

            xml_root = ET.fromstring(self.xml_config)
            # Parse Design Center configuration into devices that will
            # be provided during MQTT discovery
            self.infusion_memory_valid = True
            self.objects = self.create_objects(xml_root)

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
                    name = obj.find('Name')
                    if name is None or not name.text:
                        continue
                    objects[vid] = {"VID" : vid, "type" : obj_type,
                                    "name" : name.text}

                    parent = obj.find('Parent')
                    if parent is None:
                        parent = obj.find('Area')
                    model = obj.find('Model')
                    serial = obj.find('SerialNumber')
                    if parent is not None:
                        objects[vid]["parent"] = parent.text
                    if model is not None:
                        objects[vid]["model"] = model.text
                    if serial is not None:
                        objects[vid]["serial"] = serial.text

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
    def process_hierarchy(self, objects, vid):
        """
        Creates a long name from a entity name and the heirarchy of
        Design Center 'Area' objects it is contained in

        param objects: dictionary of Design Center objects
        param vid:     Design Center device ID
        returns:       unique_id, long name string
        """

        # Create fullname and fulluid based on "Area"
        fullname = name = objects[vid]["name"]
        fulluid = self.uidify(name)
        next_vid = objects[vid].get("parent")
        while next_vid in objects:
            # If Area and not root of hierarchy, prepend Area name
            if (objects[next_vid]["type"] == "Area" and
                    objects[next_vid].get("parent") in objects):
                name = objects[next_vid]["name"]
                fullname = name + ", " + fullname
                fulluid = self.uidify(name) + "." + fulluid
            next_vid = objects[next_vid].get("parent")
        fulluid = fulluid + "." + vid

        # Find device this entity is a part of
        next_vid = objects[vid].get("parent")
        while next_vid in objects:
            # If Dimmer or Module, this is the device that owns the entity
            if (objects[next_vid]["type"] == "Dimmer" or
                    objects[next_vid]["type"] == "Module"):
                objects[vid]["device_vid"] = next_vid
                break
            next_vid = objects[next_vid].get("parent")

        return (fulluid, fullname)

    # Build entity dictionary
    #
    # Device names are not unique, so a unique entity id (uid) is built
    # by prefixing it's name with the "Area" hierarchy and sufixing with the
    # Design Center object ID.
    #
    # returns: a dictionary of entities found
    #          Dictionary keys are Design Center object IDs (VID)
    def filter_objects(self, device_type_list, objects):
        """
        Create a dictionary of entities from a dictionary of Design Center objects
        by filtering based on object 'type'

        param objects: dictionary of Design Center objects
        returns:       dictionary of entities
        """

        self._log.debug("filter_objects")
        entities = {}
        unique_entities = {}
        for vid, item in objects.items():
            uid, fullname = self.process_hierarchy(objects, vid)
            item["fullname"] = fullname
            item["uid"] = uid
            if item["type"] in device_type_list:
                unique_entities[uid] = item
        for uid, item in unique_entities.items():
            vid = item["VID"]
            entities[vid] = item
        return entities

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

        self._log.debug("lookup_site")
        site_name = "Default"
        vid = next(iter(self.entities.values()))["VID"]
        next_vid = objects[vid].get("parent")
        while next_vid in objects:
            if objects[next_vid]["type"] == "Area":
                site_name = objects[next_vid]["name"]
            next_vid = objects[next_vid].get("parent")
        return site_name

class InFusionClient(asyncio.Protocol):
    """
    Vantage inFusion host command handler
    Establishes TCP connection to inFusion host command port.
    Sends commands and receives status.
    """

    def __init__(self, cfg):
        """
        param cfg: dictionary of Vantage inFusion connection settings
        """

        self._log = logging.getLogger("InFusionClient")

        # Vantage TCP access
        self._ip = cfg.get("ip")
        self._port = cfg.get("command_port")
        self._zeroconf = cfg.get("zeroconf")
        if not self._zeroconf and (not self._ip or self._port is None):
            raise ConfigException("Zeroconf or IP/Port is required")

        # Callbacks
        self.on_state = None
        self.on_unhandled = None

        # Vantage returns more than one line per recv
        # Buffer the data so we can read on line at a time
        self.state_buf = io.BytesIO()

        self._transport = None
        self.connection = None
        self.connected = False
        self.zeroconf_future = None
        self.connected_future = None
        self.connection_lost_future = None
        self._loop = None

    def on_zeroconf_service_state_change(self, zeroconf, service_type, name, state_change):
        """
        zeroconf service change callback

        param zeroconf:     Zeroconf instance
        param service_type: zeroconf service type
        param name:         service name
        param state_change: new service state
        """

        async def _set_future(future):
            future.set_result(None)

        self._log.debug("zeroconf service change")
        ip, port = handle_zeroconf_service_state_change(zeroconf, service_type,
                                                        name, state_change)

        if ip and port:
            self._ip = ip
            self._port = port
            self._log.debug("zeroconf_future set_result")
            asyncio.run_coroutine_threadsafe(_set_future(self.zeroconf_future), self._loop).result()

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
        self._loop = asyncio.get_running_loop()
        if self._zeroconf:
            self._log.debug("Lookup service _hc via zeroconf")
            self.zeroconf_future = self._loop.create_future()
            zc = Zeroconf()
            browser = ServiceBrowser(zc, "_hc._tcp.local.",
                                     handlers=[self.on_zeroconf_service_state_change])
            self._log.debug("await zeroconf_future")
            await self.zeroconf_future
            self._log.debug("zeroconf_future done")
            zc.close()

        # establish TCP connection to Vantage inFusion
        self._log.debug("create_connection IP %s:%d", self._ip, self._port)
        self.connected_future = self._loop.create_future()
        self.connection_lost_future = self._loop.create_future()
        self.connection = await self._loop.create_connection(lambda: self,
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
        returns:    entity_type, vid, state
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
            entity_type, vid, state = self.decode_state(line)
            if entity_type and vid and state:
                if self.on_state is not None:
                    self.on_state(entity_type, vid, state)
            else:
                if self.on_unhandled is not None:
                    self.on_unhandled(line)
            line = self.state_buf.readline()
