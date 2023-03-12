# OpenWRT Home Assistant device tracker

## What's this? ##
This script is a WiFi device tracker that runs on OpenWRT devices. It watches ubus join/leave events of WiFi clients in realtime and updates their status in
Home Assistant by calling the Home Assistant REST API. This causes WiFi events to be noticed by Home Assistant instantly, making your home automation a lot more responsive.
It can handle network failures or Home Assistant being offline, and will recover once Home Assistant is back online. It does this by adding all events to a queue and making
sure every event is accepted by Home Assistant before removing it from the queue.

## Installation ##

* Log in to your OpenWRT device
* Place presence-detector.py and presence-detector.settings.json somewhere persistent (I use /etc/config)
* Make presence-detector.py executable: chmod +x presence-detector.py
* Place the init-script from this repo's init.d directory into /etc/init.d on your device
* Install python + deps: opkg update && opkg install python3-light python3-urllib python3-idna
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
      "hostname": "Dave",
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
* params: A dictionary containing additional parameters for specific devices (see the example above). These are sent together with the MAC address and location name. For information on which keys you can add, see [here](https://www.home-assistant.io/integrations/device_tracker/#device_trackersee-service).
* ap_name: If you have only one access point, leave as "". If this runs on multiple access points, give a name here, e.g. "ap1". The mac address of every wifi device will be prefixed with this name in HA.
* location: Custom location name to be assigned to online devices. Default: "home"
* away: Custom location name to be sent when a device is no longer connected. Default: "not_home"
* debug: Enable or disable debugging (prints state information on stdout when enabled). Default: false

## Logging ##
The program will run as a 'service' in the background and will log interesting events to syslog.
You can read these events by running 'logread' on your device, or using your favorite web-ui. 
