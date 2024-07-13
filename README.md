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
It watches ubus join/leave events of Wi-Fi clients in realtime and updates their status in Home Assistant by calling the
Home Assistant REST API (device_tracker.see endpoint). This causes Wi-Fi events to be noticed by Home Assistant instantly,
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
* Install python + deps: opkg update && opkg install python3-light python3-urllib python3-idna. If you need to connect to Home Assistant via HTTPS, also install python3-openssl.
* Adjust /etc/config/presence-detector.settings.json to your needs (see below)
* run 'service presence-detector enable' to enable the service at startup
* run 'service presence-detector start', or simply reboot

### Steps to perform in Home Assistant

* Enable the [device_tracker](https://www.home-assistant.io/integrations/device_tracker/) integration in Home Assistant, by adding the following to your HA configuration.yaml:
```yaml
# Enable device_tracker and 'see' REST API
device_tracker:
```

### The version entity
To verify that the communication between Home Assistant and the presence detector is OK and at the latest version, the presence detector registers a 'presence_detector_version' entity with the current version number of the script in Home Assistant. You can view it as follows:
* Log in to Home Assistant
* Navigate to Settings -> Devices & Services -> Entities
* Search for 'presence_detector_version'.
If all is well, you should see a version entity for each router you installed it on, with the current version number as its value.

## OpenWRT device configuration
The settings file on your OpenWRT device looks like this:

```json
{
  "hass_url": "http://hassio.local:8123",
  "hass_token": "<Home Assistant REST API Bearer Token>",
  "interfaces": ["hostapd.wlan0", "hostapd.wlan1"],
  "filter_is_denylist": true,
  "filter": ["01:23:45:67:89:ab"],
  "params": {
    "00:00:00:00:00:00": {
      "host_name": "Dave",
      "dev_id": "phonedave"
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
* hass_url: The URL to your Home Assistant device, including the port (8123 is the default HassOS port).
* hass_token: This is a Home Assistant 'Long-lived token'. You can create it in the HA web-ui by clicking on your user-name,
  then scolling all the way down to 'Long-lived tokens' and clicking 'Create Token'.
* interfaces: This is an array of Wi-Fi interface names to watch, prefixed with 'hostapd.' You can get a list of interface names by running: `ubus list hostapd.*` on your OpenWRT device.
* filter_is_denylist: Determines if the filter setting is a denylist or an allowlist
* filter: A list (json array) of devices to monitor or ignore, depending on the `filter_is_denylist` setting.
* params: An optional dictionary containing additional parameters for specific devices (see the example above).
If specified, these are sent to HA together with the MAC address and location name. For information on which keys you can add, see [here](https://www.home-assistant.io/integrations/device_tracker/#device_trackersee-service).
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

As mentioned, this script calls the device_tracker.see service in HA.
This will create an entry in the known_devices.yaml file in the HA configuration directory. Entries in this file create entities inside HA which you can use for automations. Using the example params shown above the known_devices.yaml entry will be:
```
dave:
  name: Dave
  mac: 00:00:00:00:00:00
  icon:
  picture:
  track: true
```
Note that these entries will be created automatically the first time a device is 'seen' by your OpenWRT router. 
After an entry has been created, you can update the configuration to enhance the appearance of the device in HA.
You can select the icon this entity will use by entering an [MDI](https://pictogrammers.com/library/mdi/) code for the icon, ie `icon: mdi:cellphone-basic`.
Updating the icon or other settings manually requires a restart of HA.

Adding this icon will create an entity that looks like:
![Entity](entity.png)

Testing indicates that HA only cares about the device MAC address for updating the entity state.
This means you can omit the params in presence-detector.settings.json entirely and freely edit known_devices.yaml to give the entity any name you like
as long as you keep the MAC address the same.
This also means you can add new devices to known_devices.yaml directly without them being present on your network if you know the MAC address.

The entities created from entries in known_devices.yaml will be read only in the HA web UI.
The reason for this is explained [here](https://www.home-assistant.io/faq/unique_id).
If you want to edit an entity, change the entry in known_devices.yaml and restart Home Assistant.
If you want to delete an entity then remove it from known_devices.yaml and the entity will disappear from the HA UI shortly.

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
* Double-check you have enabled the `device_tracker` integration in HA and restarted to activate it
* Check connectivity to your HA device:
```
wget http://your-ha-hostname:8123/api/services/device_tracker/see -O -
```
This should return a `HTTP error 405`, because we didn't send authentication credentials. Any other code (like 404, or connection refused) means
the connection is not OK and/or the device_tracker integration has not been enabled. See [above](#steps-to-perform-in-home-assistant) how to do this
* Disconnect and then re-connect a Wi-Fi device and check if you see logging that resembles this:
```text
Sun May 28 18:58:46 2023 daemon.debug presence-detector[4949]: Posting to HA: {'mac': 'theMAC', 'location_name': 'home', 'source_type': 'router', 'host_name': 'host_name', 'dev_id': 'dev_id'}
Sun May 28 18:58:46 2023 daemon.debug presence-detector[4949]: API Response: b'[]'
```
If you see these lines then calling HA was successful and you should see a new entity in HA under Settings -> Devices & Services -> Entities
If not, make sure the presence-detector service is [running and configured correctly](#steps-to-perform-on-your-openwrt-device)
* Make sure the Wi-Fi interface names on your router still match the "interfaces" setting. New OpenWRT version upgrades have been known to rename the default Wi-Fi interface names.
