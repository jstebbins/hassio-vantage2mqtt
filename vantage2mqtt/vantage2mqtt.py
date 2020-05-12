#!/usr/bin/env python
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
from infusion import InFusionConfig
from infusion import InFusionClient
from hamqtt import HomeAssistant

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

        self._infusion = InFusionClient(cfg["vantage"])
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
