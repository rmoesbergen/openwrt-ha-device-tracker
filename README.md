# OpenWRT Home Assistant device tracker

## Table of contents
* [What is this?](#what-is-this)
* [Installation](#installation)
   * [Steps to perform on your OpenWRT device](#steps-to-perform-on-your-openwrt-device)
   * [Steps to perform in Home Assistant](#steps-to-perform-in-home-assistant)
* [OpenWRT device configuration](#openwrt-device-configuration)
* [(Optional) Home Assistant Configuration](#optional-home-assistant-configuration)
* [An example automation](#an-example-automation)
   * [Linking multiple devices to one person](#linking-multiple-devices-to-one-person)
* [Troubleshooting](#troubleshooting)

## What is this?
This script is a Wi-Fi device tracker that runs on OpenWRT devices.
It is an alternative for the built-in [OpenWRT luci](https://www.home-assistant.io/integrations/luci/) or [OpenWRT ubus](https://www.home-assistant.io/integrations/ubus/) integrations that uses a 'push' model instead of 'polling' for updates.
It watches ubus join/leave events of Wi-Fi clients in realtime and updates their status in Home Assistant by using the
Home Assistant MQTT device_tracker integration. This causes Wi-Fi events to be noticed by Home Assistant instantly,
making your home automation a lot more responsive.
It can handle network failures or Home Assistant being offline, and will recover once Home Assistant is back online.
It does this by adding all events to a queue and making sure every event is accepted by Home Assistant before removing it from the queue.

## Installation

### Steps to perform on your OpenWRT device
* Log in to your OpenWRT device
* Place presence-detector.py and presence-detector.settings.json somewhere persistent (I use /etc/config)
* Make presence-detector.py executable: chmod +x presence-detector.py
* Place the init-script from this repo's init.d directory into /etc/init.d on your device
* Make the init-script executable: chmod +x /etc/init.d/presence-detector
* Install python + deps: opkg update && opkg install python3-light python3-paho-mqtt.
* Adjust /etc/config/presence-detector.settings.json to your needs (see below)
* run 'service presence-detector enable' to enable the service at startup
* run 'service presence-detector start', or simply reboot

### Steps to perform in Home Assistant
* Enable the [device_tracker](https://www.home-assistant.io/integrations/device_tracker/) integration in Home Assistant, by adding the following to your HA configuration.yaml:
```yaml
# Enable device_tracker
device_tracker:
```
* Install the Mosquitto MQTT broker in Home Assistant or install one yourself. If you run HassOS, this can be done by installing the 'Mosquitto' add-on.
* Install the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) in Home Assistant.

## OpenWRT device configuration
The settings file on your OpenWRT device looks like this:

```json
{
  "mqtt_host": "hassio.local",
  "mqtt_port": 1883,
  "mqtt_username": "ha",
  "mqtt_password": "<PASSWORD>",
  "interfaces": ["hostapd.wlan0", "hostapd.wlan1"],
  "filter_is_denylist": true,
  "filter": ["01:23:45:67:89:ab"],
  "params": {
    "XX:YY:DD:AA:TT:QQ": {
      "name": "Dave",
      "icon": "mdi:cellphone-basic"
    }
  },
  "ap_name": "",
  "location": "home",
  "away": "not_home",
  "fallback_sync_interval": 0,
  "debug": false
}
```

These settings will need a bit of explaining:
* mqtt_host: The hostname of the MQTT broker. In HassOS this is the same as the hostname of your Home Assistant server.
* mqtt_port: The port of the MQTT broker. Defaults to 1883.
* mqtt_username: The username to use for authentication to the MQTT broker.
* mqtt_password: The password to use for authentication to the MQTT broker.
* interfaces: This is an array of Wi-Fi interface names to watch, prefixed with 'hostapd.' You can get a list of interface names by running: `ubus list hostapd.*` on your OpenWRT device.
* filter_is_denylist: Determines if the filter setting is a denylist or an allowlist
* filter: A list (json array) of devices to monitor or ignore, depending on the `filter_is_denylist` setting.
* params: An optional dictionary containing additional parameters for specific devices (see the example above).
If specified, these are sent to HA together with the MAC address and location name. For information on which keys you can add, see [here](https://www.home-assistant.io/integrations/device_tracker.mqtt/#device_tracker-mqtt-configuration-variables).
For info on how these params appear in HA see [below](#home-assistant-configuration)
* ap_name: If you have only one access point, leave as "". If this runs on multiple access points, give a name here, e.g. "ap1". The mac address of every Wi-Fi device will be prefixed with this name in HA.
* location: Custom location name to be assigned to online devices. Default: "home"
* away: Custom location name to be sent when a device is no longer connected. Default: "not_home"
* fallback_sync_interval: Interval in seconds to perform a full sync of online/offline devices to HA. Enable this as a fallback option if you have issues with devices not being detected as 'offline' when they go out of WiFi range.
**NOTE**: this will perform a 'ubus get clients' call every X seconds, which could increase load on the router. Default 0 (disabled)
* source_type: This is the type of device that gets sent to HA to represent the tracking device. It defaults to 'router', but can also be set to 'gps' to allow for different zones/locations to work.
* debug: Enable or disable debugging (prints state information on stdout when enabled). Default: false

## (Optional) Home Assistant Configuration
This section goes into a bit more detail about where the entity information discovered by OpenWRT is stored in HA, and how you can tune
the appearance of the Wi-Fi device entities. This is all optional and not needed to get a functioning automation going.

As mentioned, this script uses MQTT to register and update device state. The device entities will be automatically created, so no need manually add them to your configuration.yaml.
If you want the devices to have a nice name or icon, you can add them to the presence-detector.settings.json file in the 'params' section.

You can select the icon this entity will use by entering an [MDI](https://pictogrammers.com/library/mdi/) code for the icon, ie `icon: mdi:cellphone-basic`.
Updating the icon or other settings manually requires a restart of presence-detector.

Adding this icon will create an entity that looks like:
![Entity](entity.png)

Testing indicates that HA only cares about the device MAC address for updating the entity state.
This means you can omit the params in presence-detector.settings.json entirely and devices will still be created in HA.
But they will only have the mac address as the name and identifier.

**NB** These entities will persist over reboots even if deleted, I'm assuming there's a background task that cleans them up during normal operation.

## An example automation
Here's how to create an example automation that uses the OpenWRT device state:
* Navigate to "Settings" > "Automations & Scenes" and click "Create Automation"
* Choose "Create new automation".
* Click "ADD TRIGGER".
* Choose "State"
* From "Entity" select the Wi-Fi tracked device, it should be prefixed with 'device_tracker'
* In "From" choose "Home"
* In "To" choose "Away"
* Under "Actions" specify what you want to happen when the device is "home" (e.g. turn on a light)
* Click "Save"
* Duplicate the automation
* Swap the "From" and "To" options.
* Change "Actions" to do the opposite of the first automation (e.g. turn the light off)
* Click "Save"

Now you can test the automation by turning the device's Wi-Fi radio on/off or clicking "disconnect" in the OpenWRT webGUI.

### Linking multiple devices to one "Person"
If you have multiple devices that can indicate if a "Person" is home or not, you can use the "Person" integration.
Instead of tracking one device and basing automations on that, you can trigger automations on the Person being "Home" or "Away".
Only if all devices assigned to the Person are "Away", will the Person be marked as "Away".

You can configure this as follows:
* Navigate to "Settings" -> "People" and click on the person you want to assign devices to
* Under "Select the devices that belong to this person.", add the device_tracker.* devices you want to assign
* Click "Update"
* In your automation trigger choose "State" and use "person.<Name>" as the Entity.

## Troubleshooting
The program will run as a 'service' in the background and will log interesting events to syslog.
You can read these events by running 'logread' on your OpenWRT device, or using your favorite web-ui.

In case something doesn't work, here are a some things you can do to check your setup:
* Check the OpenWRT logging with 'logread' and see if there are any error messages.
* Double-check you have enabled the MQTT integration in HA and that it is connected to the correct broker.
* Check connectivity to your MQTT broker from the router by running:
```
nc <MQTT_HOST> <MQTT_PORT>
```
This should not return an error and open a connection. If you type some text it should disconnect. 
If the connection not OK, check your firewall settings, if the broker is running, etc.
* Disconnect and then re-connect a Wi-Fi device and check if you see logging that resembles this:
```text
Thu Jun  4 14:41:53 2026 daemon.debug presence-detector[31196]: Publishing to homeassistant/device_tracker/xx_xx_xx_xx_xx_xx/state: home
```
If you see these lines then publishing to MQTT was succesful and you should see a new entity in HA under Settings -> Devices & Services -> Entities
If not, make sure the MQTT integrations is [enabled](#steps-to-perform-on-your-openwrt-device)
* Make sure the Wi-Fi interface names on your router still match the "interfaces" setting. New OpenWRT versions have been known to rename the default Wi-Fi interface names.
