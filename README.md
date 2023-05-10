# OpenWRT Home Assistant device tracker

## What's this? ##
This script is a WiFi device tracker that runs on OpenWRT devices. It watches ubus join/leave events of WiFi clients in realtime and updates their status in
Home Assistant by calling the Home Assistant REST API(device_tracker.see endopint). This causes WiFi events to be noticed by Home Assistant instantly, making your home automation a lot more responsive.
It can handle network failures or Home Assistant being offline, and will recover once Home Assistant is back online. It does this by adding all events to a queue and making
sure every event is accepted by Home Assistant before removing it from the queue.

## Installation ##

* Log in to your OpenWRT device
* Place presence-detector.py and presence-detector.settings.json somewhere persistent (I use /etc/config)
* Make presence-detector.py executable: chmod +x presence-detector.py
* Place the init-script from this repo's init.d directory into /etc/init.d on your device
* Make the init-script executable: chmod +x /etc/init.d/presence-detector
* Install python + deps: opkg update && opkg install python3-light python3-urllib python3-idna. If you need to connect to Home Assistant via HTTPS, also install python3-openssl.
* Adjust /etc/config/presence-detector.settings.json to your needs (see below)
* run 'service presence-detector enable' to enable the service at startup
* run 'service presence-detector start', or simply reboot

## Configuration ##
The settings file looks like this:

```json
{
  "hass_url": "http://hassio.local:8123",
  "hass_token" : "<Home Assistant REST API Bearer Token>",
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
  "debug": false
}
```

Some settings will need a bit of explaining:
* hass_url: The URL to your Home Assistant device, including the port (8123 is the default HassOS port).
* hass_token: This is a Home Assistant 'Long-lived token'. You can create it in the HA web-ui by clicking on your user-name,
  then scolling all the way down to 'Long-lived tokens' and clicking 'Create Token'.
* interfaces: This is an array of Wifi interface names to watch, prefixed with 'hostapd.' You can get a list of interface names by running: `ubus list hostapd.*` on your device.
* filter_is_denylist: Determines if the filter setting is a denylist or an allowlist
* filter: A list (json array) of devices to monitor or ignore, depending on the `filter_is_denylist` setting.
* params: A dictionary containing additional parameters for specific devices (see the example above). These are sent together with the MAC address and location name. For information on which keys you can add, see [here](https://www.home-assistant.io/integrations/device_tracker/#device_trackersee-service). For info on how these params appear in HA see [below](#home-assistant)
* ap_name: If you have only one access point, leave as "". If this runs on multiple access points, give a name here, e.g. "ap1". The mac address of every wifi device will be prefixed with this name in HA.
* location: Custom location name to be assigned to online devices. Default: "home"
* away: Custom location name to be sent when a device is no longer connected. Default: "not_home"
* debug: Enable or disable debugging (prints state information on stdout when enabled). Default: false

## Home Assistant ##
Correct as at HA version: 2023.5.2

As mentioned, this script calls the device_tracker.see service in HA. This will create an entry in the known_devices.yaml file. Entries in this file create entities inside HA which you can use for automations. Using the example params shown above the known_devices.yaml entry will be:
```
dave:
  name: Dave
  mac: 00:00:00:00:00:00
  icon:
  picture:
  track: true
```
You can select the icon this entity will use by entering an MDI code for the icon ie `icon: mdi:cellphone-basic`. Updating the icon or other seetings manually requires a restart of HA.

This will create an entity that looks like:
![Entity](entity.png)

Testing indicates that HA only cares about the device MAC address for updating the entity state. This means you can omit the params in presence-detector.settings.json entirely and freely edit known_devices.yaml to give the entity any name you like as long as you keep the MAC address the same. This also means you can add new devices to known_devices.yaml directly without them being present on your network if you know the MAC address.

The entities created from entries in known_devices.yaml will be read only, which means you can not edit or delete the entity from within the HA UI. The reason for this is explained [here](https://www.home-assistant.io/faq/unique_id). If you want to edit an entity, change the entry in known_devices.yaml and restart Home Assistant. If you want to delete an entity then remove it from known_devices.yaml and the entity will disappear from the HA UI shortly. **NB** These entities will persist over reboots even if deleted, I'm assuming there's a background task that cleans them up during normal operation.


## Logging ##
The program will run as a 'service' in the background and will log interesting events to syslog.
You can read these events by running 'logread' on your device, or using your favorite web-ui. 
