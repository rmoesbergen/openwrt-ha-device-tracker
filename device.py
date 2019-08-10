#!/usr/bin/env python3
#

import subprocess
import requests
import json
import time


class Settings:
    def __init__(self):
        self._settings = {}
        with open('settings.json', 'r') as settings:
            self._settings = json.load(settings)

    def __getattr__(self, item):
        return self._settings.get(item)


class PresenceDetector:
    def __init__(self):
        self.settings = Settings()
        self.full_sync_counter = self.settings.full_sync_polls
        self.clients_seen = {}

    def ha_seen(self, client, seen=True):
        location = "home"
        if not seen:
            location = "not_home"

        body = {"mac": client, "location_name": location}
        try:
            response = requests.post(f'{self.settings.hass_url}/api/services/device_tracker/see', json=body,
                                     headers={'Authorization': f'Bearer {self.settings.hass_token}'})
            if self.settings.debug:
                print(response.content)
        except Exception as e:
            if self.settings.debug:
                print(str(e))
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
                    print(f"full sync {client}")
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
                        if client not in self.clients_seen:
                            if self.ha_seen(client):
                                self.clients_seen[client] = self.settings.offline_after
                        else:
                            self.clients_seen[client] = self.settings.offline_after

            time.sleep(self.settings.poll_interval)
            self.full_sync()

            if self.settings.debug:
                print(self.clients_seen)


if __name__ == "__main__":
    detector = PresenceDetector()
    detector.polling_loop()
