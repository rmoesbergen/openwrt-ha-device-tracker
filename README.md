# OpenWRT Home Assistant device tracker

## What's this? ##
I got fed up with all the Home Assistant device tracking solution for OpenWRT that just didn't work reliably, so I created my own.
This little script runs on the OpenWRT device, polls the currently connected WiFi clients and updates their status in
Home Assistant by calling the Home Assistant REST API. It only sends API requests on device join/leave. It handles connectivity
issues with Home Assistant gracefully and ensures that device state is always in sync, even after restarts, connectivity loss, etc.

## Installation ##

* Log in to your OpenWRT device
* Place presence-detector.py and settings.json somewhere persistent (I use /etc/config)
* Make presence-detector.py executable: chmod +x presence-detector.py
* Place the init-script from this repo's init.d directory into /etc/init.d on your device
* Install python + deps: opkg update && opkg install python3-requests
* Adjust settings.json to your needs (see below)
* run 'service presence-detector enable' to enable the service at startup
* run 'service presence-detector start', or simply reboot

## Configuration ##
The settings file looks like this:

```json
{
  "hass_url": "http://hassio.local:8123",
  "hass_token" : "<Home Assistant REST API Bearer Token>",
  "interfaces": ["hostapd.wlan0", "hostapd.wlan1"],
  "do_not_track": ["01:23:45:67:89:ab"],
  "params": {
    "00:00:00:00:00:00": {
      "mac": "ff:ff:ff:ff:ff:ff",
      "hostname": "Dave",
      "dev_id": "phonedave"
    }
  },
  "offline_after": 3,
  "poll_interval": 15,
  "full_sync_polls": 10,
  "ap_name": "",
  "zone": "my_zone",
  "debug": false
}
```

Some settings will need a bit of explaining:
* hass_url: The URL to your Home Assistant device, including the port (8123 is the default Hass.io port).
* hass_token: This is a Home Assistant 'Long-lived token'. You can create it in the HA web-ui by clicking on your user-name,
  then scolling all the way down to 'Long-lived tokens' and clicking 'Create Token'.
* interfaces: This is an array of Wifi interface names to poll, prefixed with 'hostapd.' (it's the ubus service name).
* do_not_track: This is an array of devices to ignore.
* params: A dictionary containing additional parameters for specific devices. Those are sent together with MAC address and location name. Note here you could also override MAC and location name. For information on which keys you can add, see [here](https://www.home-assistant.io/integrations/device_tracker/#device_trackersee-service).
* offline_after: Set a device as not_home after is has been absent for this many poll intervals.
* poll_interval: Poll interval in seconds.
* full_sync_polls: Re-sync the device state of all devices every X poll intervals. This is to ensure device state is in sync,
  even after HA restarts, connectivity loss, or missed events.
* ap_name: If only one access point, leave as "". If script should run on multiple access points, give a name here, e.g. "ap1". The mac address will be prefixed by this on HA.
* zone: Custom zone name to be assigned to spotted devices.
* debug: Enable or disable debugging (prints state information on stdout when enabled).

## Logging ##
The program will run as a 'service' in the background and will log interesting events to syslog.
You can read these events by running 'logread' on your device, or using your favorite web-ui. 
