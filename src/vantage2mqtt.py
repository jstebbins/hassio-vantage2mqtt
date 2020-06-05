#!/usr/bin/env python3
"""
Vantage inFusion to MQTT Gateway
"""

import sys
import getopt
import json
import time
import logging
import asyncio
import jsonschema
from infusion import InFusionConfig, InFusionException, ConfigException
from infusion import InFusionClient
from hamqtt import HomeAssistant

class VantageGateway:
    """
    Vantage inFusion to MQTT gateway
    """
    PROTO_UNKNOWN = 0
    PROTO_HOMIE = 1
    PROTO_HOMEASSISTANT = 2

    def __init__(self, cfg, entities, objects, protocol, short_names=False, dry_run=False):
        """
        param cfg:      dictionary of gateway settings
        param protocol: MQTT device discovery protocol to use
        """

        self._log = logging.getLogger("gateway")
        self.protocol = protocol
        self.cfg = cfg
        self._site_name = cfg["vantage"]["site-name"]

        if self.protocol == self.PROTO_HOMEASSISTANT:
            self._proto = HomeAssistant("Vantage", self._site_name, cfg["mqtt"], dry_run)
        self._proto.on_filtered = self.on_mqtt_filtered
        self._proto.on_unfiltered = self.on_mqtt_unfiltered
        self._min_reconnect_interval = 1
        self._max_reconnect_interval = 120

        try:
            self._infusion = InFusionClient(cfg["vantage"])
        except ConfigException as err:
            self._log.critical("Error: %s", str(err))
            sys.exit(1)

        self._infusion.on_state = self.on_vantage_state
        self._infusion.on_unhandled = self.on_vantage_unhandled
        self._entities = entities
        self._objects = objects

        # The controller likes to toggle things around for a bit
        # Wait till we have updated the controller with full status
        # before allowing it to change values
        self.wait_for_settle = True
        self.short_names = short_names

        # Callbacks
        self.on_vantage_config_changed = None

    def read_vantage_config(self):
        """
        Read Vantage Design Center configuration and parse out the
        elements that will be communicated in the MQTT device discovery
        phase
        """

        self._log.debug("read_vantage_config")
        try:
            vantage_cfg = InFusionConfig(self.cfg["vantage"])
        except (InFusionException, ConfigException) as err:
            self._log.critical("Error: %s", str(err))
            raise

        if vantage_cfg.infusion_memory_valid and vantage_cfg.updated:
            if self.on_vantage_config_changed is not None:
                self.on_vantage_config_changed(vantage_cfg.xml_config,
                                               vantage_cfg.objects)
            self._entities = vantage_cfg.entities
            self._objects = vantage_cfg.objects

    def switch_command(self, oid, command):
        """
        process MQTT command for a switch
        """

        entity = self._entities.get(oid)
        if oid not in self._entities or command == "status":
            return
        # Validate entity is a button
        if entity["type"] != "Switch":
            return

        self._log.debug("switch command: %s %s", command, oid)
        if command == "set":
            self._infusion.send_command("BTN %s" % oid)

    def light_command(self, oid, command, param):
        """
        process MQTT command for a switch
        """

        entity = self._entities.get(oid)
        if not entity or command == "status":
            return
        # Validate entity is a light
        if entity["type"] not in ("Light", "DimmerLight"):
            return

        self._log.debug("light command: %s %s %s", oid, command, str(param))
        if command == "set":
            try:
                value = json.loads(param)
            except json.JSONDecodeError as err:
                self._log.error("light: JSON error: %s", str(err))
                value = {}
                return

            brightness = value.get("brightness")
            state = value.get("state")
            transition = value.get("transition")
            if state == "OFF":
                brightness = 0
            if state == "ON" and not brightness:
                brightness = 100
            if entity["type"] == "DimmerLight" and transition:
                self._infusion.send_command("RAMPLOAD %s %d %d" %
                                            (oid, brightness, transition))
            else:
                self._infusion.send_command("LOAD %s %d" % (oid, brightness))

    def site_command(self, oid, command, param):
        """
        Control commands that can be manually sent via MQTT topic
        'vantage/site/<SiteName>/all/<command>' to reconfigure
        the gateway
        """

        if oid == "all":
            if command == "flush":
                # Force flushing of currently known inFusion entities from HA
                self._proto.flush_entities(self._entities)
            elif command == "register":
                # Force re-registering of inFusion entities
                # json dict param allows setting 'short-names'
                try:
                    value = json.loads(param)
                except json.JSONDecodeError as err:
                    self._log.error("light: JSON error: %s", str(err))
                    value = {}
                short_names = value.get("short-names")
                if short_names is not None:
                    self.short_names = short_names
                self._proto.register_entities(self._entities, self._objects,
                                              self.short_names)
            elif command == "reload":
                # Force reloading from inFusion memory card
                try:
                    self.read_vantage_config()
                except (InFusionException, ConfigException):
                    return
                self._proto.register_entities(self._entities, self._objects,
                                              self.short_names)
            elif command == "refresh":
                # Get new status from all inFusion entities
                self.update_state(self._entities)

    # Translate MQTT command and send to vantage
    def on_mqtt_filtered(self, mosq, obj, msg):
        #pylint: disable=unused-argument
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
        entity_type, oid, command = self._proto.split_topic(msg.topic)
        if not oid or not command:
            return
        if entity_type == "switch":
            self.switch_command(oid, command)
        elif entity_type == "light":
            self.light_command(oid, command, value)
        elif entity_type == "site":
            self.site_command(oid, command, value)
        else:
            self._log.warning('Unknown entity type "%s"', entity_type)

    def on_mqtt_unfiltered(self, mosq, obj, msg):
        #pylint: disable=unused-argument
        """
        MQTT callback for topics not handled by any other callbacks

        param mosq: ignored
        param obj:  ignored
        param msg:  contains the MQTT topic and value
        """

        mqttmsg = str(msg.payload.decode("utf-8"))
        self._log.debug(">unknown: %s - %s", msg.topic, mqttmsg)

    def on_vantage_state(self, entity_type, oid, state):
        """
        Vantage callback for button status

        param oid:   Vanntage inFusion entity ID
        param onoff: button on/off state
        """

        topic = self._proto.gateway_entity_state_topic(entity_type, oid)
        value = self._proto.translate_state(entity_type, state)
        self._proto.publish(topic, value)

    def on_vantage_unhandled(self, line):
        """
        Vantage callback for status not handled by any other callbacks
        """
        self._log.debug(">vantage unknown: %s", line)

    def update_state(self, entities):
        """
        Update the controllers state for all buttons
        """

        for oid, item in entities.items():
            if item["type"] == "Switch":
                self._infusion.send_command("GETLED %s" % oid)
            elif item["type"] in ("DimmerLight", "Light"):
                self._infusion.send_command("GETLOAD %s" % oid)

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
                self._log.warning("inFusion connect error, retrying: %s", str(err))
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

    async def _connect(self, flush=False):
        """
        Connect to MQTT broker and Vantage inFusion TCP port.
        Register Vantage inFusion entities with controller.
        Listen for MQTT topics from controller and publish status.
        Forward commands from controller to inFusion and listen for status.

        param flush:       flush old entity settings when registering
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
        # If requested on the command line, flush old entities from controller
        if flush:
            self._proto.flush_entities(self._entities)
        # Register inFusion entities with controller through MQTT broker
        self._proto.register_entities(self._entities, self._objects,
                                      self.short_names)
        # Read current state of entities from inFusion and send to MQTT broker
        self.update_state(self._entities)
        time.sleep(1)
        self.wait_for_settle = False
        self._log.debug("ready")

        # Run forever, or at least until an exception occurs
        while True:
            await self._infusion.connection_lost_future
            self._log.warning("Connection to inFusion lost, reconnecting")
            # Connection to inFusion lost, try reconnecting
            await self.connect_infusion()
            # Wait for inFusion to be ready for us to send it commands
            # and retrieve status
            await self._infusion.connected_future
            # Enable status feedback for button LEDs
            self.enable_status()
            # Refresh state of entities
            self.update_state(self._entities)

    def connect(self, flush=False):
        """
        Kick off asyncio
        """
        asyncio.run(self._connect(flush))

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
        "    -c --config     - Read options from file (default vantage.json)\n"
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
                    "site-name" : {"type" : "string"},
                    "zeroconf" : {"type" : "boolean"},
                    "ip" : {"type" : "string"},
                    "command_port" : {"type" : "number"},
                    "config_port" : {"type" : "number"},
                    "skip-device-cache" : {"type" : "boolean"},
                    "dcconfig" : {"type" : "string"},
                    "dccache" : {"type" : "boolean"},
                    "debug" : {"type" : "boolean"},
                    "short-names" : {"type" : "boolean"},
                    "buttons" : {"type" : "boolean"},
                    "lights" : {"type" : "boolean"},
                    "motors" : {"type" : "boolean"},
                    "relays" : {"type" : "boolean"}
                },
                "additionalProperties" : False
            },
            "mqtt" : {
                "type" : "object",
                "properties" : {
                    "username" : {"type" : "string"},
                    "password" : {"type" : "string"},
                    "ip" : {"type" : "string"},
                    "port" : {"type" : "number"}
                },
                "required" : ["ip", "port"],
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
            opts, _ = getopt.getopt(
                argv[1:], "hvdncw:c:fs",
                ["help", "verbose", "debug", "dry-run", "cache", "write=",
                 "config=", "flush", "homeassistant", "shortnames"])

        except getopt.GetoptError:
            self.usage()
            sys.exit(2)

        self.cfg = {}
        self.verbose = None
        self._dry_run = None
        self.dc_save = None
        self.cache_dc = None
        self.flush_devices = False
        self.protocol = VantageGateway.PROTO_HOMEASSISTANT
        self.use_short_device_names = None
        self.config = self.CONFIG_FILENAME
        for opt, arg in opts:
            if opt in ("-h", "--help"):
                self.usage()
                sys.exit()
            elif opt in ("-v", "--verbose"):
                self.verbose = logging.INFO
            elif opt in ("-d", "--debug"):
                self.verbose = logging.DEBUG
            elif opt in ("-n", "--dry-run"):
                self._dry_run = True
            elif opt in ("-w", "--write"):
                self.dc_save = arg
            elif opt in ("-c", "--config"):
                self.config = arg
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

        try:
            with open(self.config) as fp:
                self.cfg = json.load(fp)
                jsonschema.validate(self.cfg, self.gatewayConfigSchema)
        except jsonschema.exceptions.ValidationError as err:
            self._log.critical("Invalid configuration file: %s", str(err))
            sys.exit(1)
        except Exception as err:
            self._log.critical("Error: %s", str(err))
            raise
        fp.close()

    def read_vantage_config(self):
        """
        Read Vantage Design Center configuration and parse out the
        elements that will be communicated in the MQTT device discovery
        phase
        """

        try:
            dc = self.cfg["vantage"].get("dcconfig")
            if self.cfg["vantage"].get("skip-device-cache"):
                self._log.debug("Skipping device cache")
                return InFusionConfig(self.cfg["vantage"], xml_config_file=dc)
            return InFusionConfig(self.cfg["vantage"],
                                  self.DEVICES_FILENAME, dc)
        except (InFusionException, ConfigException) as err:
            self._log.critical("Error: %s", str(err))
            sys.exit(1)

    def write_design_center_config(self, xml):
        """
        If requested on the command line, write the Vantage
        Design Center configuration out to a file
        """

        self._log.debug("write_design_center_config")
        # If started in caching mode and dcconfig was provided,
        # write the Design Center configuration that was read
        # from the Vantage inFusion memory card to dcconfig file
        dc = self.cfg["vantage"].get("dcconfig")
        if self.cache_dc and dc:
            try:
                with open(dc, "w") as fp:
                    fp.write(xml)
            except IOError as err:
                self._log.warning(
                    "Warning: failed to update %s, %s", dc, str(err))
            fp.close()

        # If command line arg -w/--write was provided,
        # write the Design Center configuration that was read
        # from the Vantage inFusion memory card to the file
        # provided on the command line
        if self.dc_save:
            try:
                with open(self.dc_save, "w") as fp:
                    fp.write(xml)
            except IOError as err:
                self._log.warning(
                    "Warning: failed to update %s, %s", self.dc_save, str(err))
            fp.close()

    def write_devices_config(self, devices):
        """
        Save the discovered devices to a file
        """

        self._log.debug("write_devices_config")
        # Save devices parsed from Design Center configuration
        # to our devices configuration file
        try:
            with open(self.DEVICES_FILENAME, "w") as fp:
                json.dump(devices, fp, indent=4, sort_keys=True)
        except IOError as err:
            self._log.warning(
                "Warning: failed to update %s, %s", self.DEVICES_FILENAME, str(err))
        fp.close()

    def on_vantage_config_changed(self, xml, objects):
        """
        Callback to write inFusion object cache and Design Center
        configuration files when an update from the inFusion memory
        card is forced

        param xml:     Design Center configuration
        param objects: updated inFusion objects
        """

        self._log.debug("on_vantage_config_changed")
        # Write Vantage inFusion configuration to file, if requested
        self.write_design_center_config(xml)

        # If we read a new Vantage inFusion configuration, update
        # the devices file
        self.write_devices_config(objects)

    def launch_gateway(self, entities, objects, dry_run):
        """
        Start the gateway
        """

        self._log.debug("launch_gateway")
        if self.use_short_device_names is None:
            self.use_short_device_names = self.cfg["vantage"].get("short-names")
        gateway = VantageGateway(self.cfg, entities, objects, self.protocol,
                                 self.use_short_device_names, dry_run)
        gateway.on_vantage_config_changed = self.on_vantage_config_changed
        try:
            gateway.connect(self.flush_devices)
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
        self.read_config()
        if self.verbose is None:
            if self.cfg["vantage"].get("debug"):
                logging.getLogger().setLevel(logging.DEBUG)
        if self.cache_dc is None:
            self.cache_dc = self.cfg["vantage"].get("dccache")

        # Read Vantage inFusion configuration,
        # may come from inFusion memory card
        vantage_cfg = self.read_vantage_config()

        # Write Vantage inFusion configuration to file, if requested
        if vantage_cfg.infusion_memory_valid:
            self.write_design_center_config(vantage_cfg.xml_config)

        # Use Vantage Design Center confguration root object name
        # for site name if not set in gateway configuration file
        if not self.cfg["vantage"].get("site-name"):
            self.cfg["vantage"]["site-name"] = vantage_cfg.site_name

        # If we read a new Vantage inFusion configuration, update
        # the devices file
        if vantage_cfg.updated:
            self.write_devices_config(vantage_cfg.objects)

        # Launch the gateway
        self.launch_gateway(vantage_cfg.entities, vantage_cfg.objects, self._dry_run)

### Main programm
if __name__ == '__main__':
    main = Main(sys.argv)
    main.run()
