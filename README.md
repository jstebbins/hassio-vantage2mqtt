## hass.io addon providing Vantage inFusion to MQTT gateway

Uses HomeAssistant MQTT Discovery to automatically populate HA entities and devices
for switches and lights from Design Center Configuration backup stored in 
the inFusion memory card.

## How it works

In a nutshell:

* Reads the Vantage Design Center xml configuration from the inFusion memory card
* Parses out buttons and lights from the configuration
* Registers these with an MQTT broker using the Home Assistant discovery protocol
* Queries the status of every device and publishes initial status
* Responds to commands published via MQTT to control buttons and lights
* Responds to status messages from inFusion TCP port and updates HA with new status

In addition to the MQTT topic protocol defined by HA, there are a few additional
command topics that can be used to control the gateway. These topics all have
the format `vantage/site/<sitename>/all/<command>` where `sitename` is lower-cased
`site-name` defined in the addon options when installed and `command` is one of:

* refresh  - Query inFusion for status of all devices and publish
* flush    - Flush all inFusion devices from HA
* register - Register all inFusion devices with HA
* reload   - Flush, read inFusion memory, register

So, if you make changes to your inFusion configuration and save those to the
inFusion backup memory card, you can update HA with those changes by:

```
mosquitto_pub -h homeassistant -p 1883 -u "username" -P "password" -t "vantage/all/yoursite/all/reload" -m ''
```

These topics can also be published from the HA Developer Tools MTQQ pane.

## Installation

* Enable "Advanced Mode" in your user profile
* Install the `Terminal & SSH` addon, configure and start it
* `ssh root@homeassistant` and `mkdir addons/vantage2mqtt`, then exit
* `scp -r Dockerfile config.json requirements.txt src/ root@homeassistant:addons/vantage2mqtt/`
* From HA Supervisor `Add-ons store`, select `Check for updates` from menu in upper right corner
* `Local add-ons` in the store should now contain `Vantage2MQTT` (You may need to reload the page), select it
* Built it
* Configure it, see configuration below
* Start it
* Profit!

## Configuration

Configuration has 2 sections, `vantage` and `mqtt`

vantage:

```
site-name    - A unique name to distinguish your vantage system from any other systems or devices using the HA MQTT discovery protocol
ip           - IP address or hostname of inFusion
command_port - inFusion host command port (3001)
config_port  - inFusion backup configuration port (2001)
dcconfig     - Filename to use if dccache is set
dccache      - Make a backup of the inFusion configuration xml
debug        - Spam copious amounts of log messages
short-names  - Use "short" HA entity and device names, see Device Names below
buttons      - Enable registration of buttons
lights       - Enable registration of lights
motors       - TBD
relays       - TBD
```

mqtt:

```
ip           - IP address or hostname of MQTT Broker
port         - MQTT Broker port
username     - MQTT Broker username
password     - MQTT Broker password
```

## Device Names

The gateway has 2 methods of constructing names for devices and entities that
are registered with HA.

Long names (default) are constructed by starting with the name of the `Button` or
`Load` from the Design Center xml and prepending the names of all `Area` objects
in the hierarchy containing the `Button` or `Load`.

Short names are constructed only from the name of the `Button` or `Load`.

The advantage of long names is you can easily tell what and where each entity is
in HA based on it's long name.  The disadvantage is the names are typically over
specified and too long.  You'll most likely need to edit them to look nice in the UI.

The advantage of short names is they will probably look right in the UI without
editing.  The disadvantage is you might need to toggle them to discover what they
are.  I.e. there may be many generically named buttons like "Light", or "Chandilier".

Personally, I use the long names and just edit anything that will show in Lovelace.

## Debugging

In addition to the the `debug` option in the configuration, you can watch the
activity between the gateway and the MQTT broker by subscribing to the following
topics.

* homeassistant/+/sitename/#
* vantage/+/sitename/#

Messages prefixed with `homeassistant` are registration messages sent from the
gateway to HA.

Messages prefixed with `vantage` are either control messages sent by HA or status
messages sent by the gateway.

