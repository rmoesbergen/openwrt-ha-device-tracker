#!/usr/bin/env python3
#

import subprocess
import requests
import json
import time
import argparse
import syslog


class Logger:
    def __init__(self, enable_debug):
        self.enable_debug = enable_debug

    def log(self, text, is_debug=False):
        if is_debug and not self.enable_debug:
            return

        level = syslog.LOG_DEBUG if is_debug else syslog.LOG_INFO
        syslog.openlog(ident="presence-detector", facility=syslog.LOG_DAEMON, logoption=syslog.LOG_PID)
        syslog.syslog(level, text)


class Settings:
    def __init__(self, config_file):
        self._settings = {
            "hass_url": "http://homeassistant.local:8123",
            "interfaces": ["hostapd.wlan0"],
            "do_not_track": [],
            "params": {},
            "offline_after": 3,
            "poll_interval": 15,
            "full_sync_polls": 10,
            "location": "home",
            "away": "not_home",
            "debug": False
        }
        with open(config_file, 'r') as settings:
            self._settings.update(json.load(settings))

    def __getattr__(self, item):
        return self._settings.get(item)


class PresenceDetector:
    def __init__(self, config_file):
        self.settings = Settings(config_file)
        self.logger = Logger(self.settings.debug)
        self.full_sync_counter = self.settings.full_sync_polls
        self.clients_seen = {}

    def ha_seen(self, client, seen=True):
        if seen:
            location = self.settings.location
        else:
            location = self.settings.away

        body = {"mac": client, "location_name": location}
        if client in self.settings.params:
            body.update(self.settings.params[client])

        try:
            response = requests.post(f'{self.settings.hass_url}/api/services/device_tracker/see', json=body,
                                     headers={'Authorization': f'Bearer {self.settings.hass_token}'})
            self.logger.log(f"API Response: {response.content}", is_debug=True)
        except Exception as e:
            self.logger.log(str(e), is_debug=True)
            # Force full sync when HA returns
            self.full_sync_counter = 0
            return False

        if not response.ok:
            self.full_sync_counter = 0

        return response.ok

    def full_sync(self):
        # Sync state of all devices once every X polls
        self.full_sync_counter -= 1
        if self.full_sync_counter <= 0:
            ok = True
            for client in self.clients_seen:
                if self.clients_seen[client] == self.settings.offline_after:
                    self.logger.log(f"full sync {client}", is_debug=True)
                    ok &= self.ha_seen(client)
            # Reset timer only when all syncs were successful
            if ok:
                self.full_sync_counter = self.settings.full_sync_polls

    def polling_loop(self):
        while True:
            to_delete = []
            for client in self.clients_seen:
                self.clients_seen[client] -= 1
                if self.clients_seen[client] <= 0:
                    # Has not been seen x times, mark as away
                    self.logger.log(f"Device {client} is now away")
                    if self.ha_seen(client, False):
                        to_delete.append(client)
                    else:
                        # Call failed -> retry next time
                        self.clients_seen[client] = 1

            for del_client in to_delete:
                del self.clients_seen[del_client]

            # Reset seen clients to 'offline_after'
            for interface in self.settings.interfaces:
                process = subprocess.run(['ubus', 'call', interface, 'get_clients'], capture_output=True, text=True)
                if process.returncode == 0:
                    clients = json.loads(process.stdout)
                    for client in clients['clients']:
                        if client in self.settings.do_not_track:
                            continue
                        # Add ap prefix if ap_name defined in settings
                        if self.settings.ap_name:
                            client = f"{self.settings.ap_name}_{client}"
                        if client not in self.clients_seen:
                            self.logger.log(f"Device {client} is now at {self.settings.location}")
                            if self.ha_seen(client):
                                self.clients_seen[client] = self.settings.offline_after
                        else:
                            self.clients_seen[client] = self.settings.offline_after

            time.sleep(self.settings.poll_interval)
            self.full_sync()

            self.logger.log(f"Clients seen: {self.clients_seen}", is_debug=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", help="Filename of configuration file", default="/etc/config/settings.json")
    args = parser.parse_args()

    detector = PresenceDetector(args.config)
    detector.polling_loop()
