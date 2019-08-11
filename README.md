# OpenWRT Home Assistant device tracker

## What's this? ##
I got fed up with all the Home Assistant device tracking solution for OpenWRT that just didn't work reliably, so I created my own.
This little script runs on the OpenWRT device, polls the currently connected WiFi clients and updates their status in
Home Assistant by calling the Home Assistant REST API. It only sends API requests on device join/leave. It handles connectivity
issues with Home Assistant gracefully and ensures that device state is alway in sync, even after restarts, etc.

## Installation ##

* Log in to your OpenWRT device
* Place device.py and settings.json somewhere persistent (I use /etc/config)
* Install python + deps: opkg update && opkg install python3-requests
* Adjust settings.json to your needs (see below)
* run device.py (in a screen for now)

Making it a proper daemon is on the todo list..

## Configuration ##
The settings file looks like this:

```json
{
  "hass_url": "http://hassio.local:8123",
  "hass_token" : "<Home Assistant REST API Bearer Token>",
  "interfaces": ["hostapd.wlan0", "hostapd.wlan1"],
  "offline_after": 3,
  "poll_interval": 15,
  "full_sync_polls": 10,
  "debug": false
}
```

Some settings will need a bit of explaining:
* hass_url: The URL to your Home Assistant device, including the port (8123 is the default Hass.io port)
* hass_token: This is a Home Assistant 'Long-lived token'. You can create it in the HA web-ui by clicking in your user-name,
  then scolling all the way down to 'Long-lived tokens' and clicking 'Create Token'
* interfaces: This is an array of Wifi interface names to poll, prefixed with 'hostapd' (it's the ubus service name)
* offline_after: Set a device as not_home after is has been absent for this many poll intervals
* full_sync_polls: Re-sync the device state of all devices every X poll intervals. This is to ensure device state is in sync,
  even after HA restarts or events are missed etc.
* debug: Enable of disable debugging (prints state information on stdout when enabled)
