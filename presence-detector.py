#!/usr/bin/env python3
#

import subprocess
import json
import signal
import time
import argparse
import syslog
from typing import Dict, Any, List
from threading import Thread
import requests


class Logger:
    def __init__(self, enable_debug: bool) -> None:
        self.enable_debug = enable_debug

    def log(self, text: str, is_debug: bool = False) -> None:
        if is_debug and not self.enable_debug:
            return

        level = syslog.LOG_DEBUG if is_debug else syslog.LOG_INFO
        syslog.openlog(
            ident="presence-detector",
            facility=syslog.LOG_DAEMON,
            logoption=syslog.LOG_PID,
        )
        syslog.syslog(level, text)


class Settings:
    def __init__(self, config_file: str) -> None:
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
            "debug": False,
        }
        with open(config_file, "r", encoding="utf-8") as settings:
            self._settings.update(json.load(settings))

    def __getattr__(self, item: str) -> Any:
        return self._settings.get(item)


class PresenceDetector(Thread):
    def __init__(self, config_file: str) -> None:
        super().__init__()
        self._settings = Settings(config_file)
        self._logger = Logger(self._settings.debug)
        self._full_sync_counter = self._settings.full_sync_polls
        self._clients_seen: Dict[str, int] = {}
        self._watchers: List[UbusWatcher] = []
        self._killed = False

    def _ha_seen(self, client: str, seen: bool = True) -> bool:
        if seen:
            location = self._settings.location
        else:
            location = self._settings.away

        body = {"mac": client, "location_name": location}
        if client in self._settings.params:
            body.update(self._settings.params[client])

        try:
            response = requests.post(
                f"{self._settings.hass_url}/api/services/device_tracker/see",
                json=body,
                headers={"Authorization": f"Bearer {self._settings.hass_token}"},
            )
            self._logger.log(f"API Response: {response.content!r}", is_debug=True)
        except Exception as e:
            self._logger.log(str(e), is_debug=True)
            # Force full sync when HA returns
            self._full_sync_counter = 0
            return False

        if not response.ok:
            self._full_sync_counter = 0

        return response.ok

    def full_sync(self) -> None:
        # Sync state of all devices once every X polls
        self._full_sync_counter -= 1
        if self._full_sync_counter <= 0:
            ok = True
            for client, offline_after in self._clients_seen.items():
                if offline_after == self._settings.offline_after:
                    self._logger.log(f"full sync {client}", is_debug=True)
                    ok &= self._ha_seen(client)
            # Reset timer only when all syncs were successful
            if ok:
                self._full_sync_counter = self._settings.full_sync_polls

    def set_client_away(self, client: str) -> None:
        self._logger.log(f"Device {client} is now away")
        ok = self._ha_seen(client, False)
        if ok:
            # Away call to HA was successful -> remove from list
            del self._clients_seen[client]
        else:
            # Call failed -> retry next time
            self._clients_seen[client] = 1

    def set_client_home(self, client: str):
        if client in self._settings.do_not_track:
            return
        # Add ap prefix if ap_name defined in settings
        if self._settings.ap_name:
            client = f"{self._settings.ap_name}_{client}"
        if client not in self._clients_seen:
            self._logger.log(f"Device {client} is now at {self._settings.location}")
            if self._ha_seen(client):
                self._clients_seen[client] = self._settings.offline_after
        else:
            self._clients_seen[client] = self._settings.offline_after

    def _get_all_online_clients(self) -> Dict[str, Any]:
        # Reset seen clients to 'offline_after'
        clients = {}
        for interface in self._settings.interfaces:
            process = subprocess.run(
                ["ubus", "call", interface, "get_clients"],
                capture_output=True,
                text=True,
                check=False,
            )
            if process.returncode != 0:
                self._logger.log(
                    f"Error running ubus for interface {interface}: {process.stderr}"
                )
                continue
            response = json.loads(process.stdout)
            clients.update(response["clients"])
        return clients

    def start_watchers(self):
        for interface in self._settings.interfaces:
            # Start an ubus watcher for every interface
            watcher = UbusWatcher(interface, self)
            watcher.start()
            self._watchers.append(watcher)

    def stop_watchers(self):
        # Signal all watchers to stop
        for watcher in self._watchers:
            watcher.stop()

    @property
    def stopped(self):
        return self._killed

    def stop(self, *args):
        self._logger.log("Stopping...")
        self.stop_watchers()
        self._killed = True

    def run(self) -> None:
        # Start ubus watcher(s) for every interface
        self.start_watchers()

        # The main (sync) polling loop
        while not self._killed:
            for client in self._clients_seen.copy():
                self._clients_seen[client] -= 1
                if self._clients_seen[client] > 0:
                    continue
                # Client has not been seen x times, mark as away
                self.set_client_away(client)

            for client in self._get_all_online_clients():
                self.set_client_home(client)

            time.sleep(self._settings.poll_interval)
            self.full_sync()

            self._logger.log(f"Clients seen: {self._clients_seen}", is_debug=True)


class UbusWatcher(Thread):
    """Watches live ubus events and signals presence detector of leave/join events"""

    def __init__(self, interface: str, detector: PresenceDetector) -> None:
        super().__init__()
        self._detector = detector
        self._interface = interface
        self._killed = False

    def stop(self):
        self._killed = True

    def run(self) -> None:
        ubus = subprocess.Popen(
            ["ubus", "subscribe", self._interface], stdout=subprocess.PIPE, text=True
        )
        if not ubus.stdout:
            return
        for line in iter(ubus.stdout.readline, "{}"):
            if self._killed:
                ubus.kill()
                return
            event = {}
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Ignore incomplete / invalid json
                pass
            if "assoc" in event:
                self._detector.set_client_home(event["assoc"]["address"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        help="Filename of configuration file",
        default="/etc/config/settings.json",
    )
    args = parser.parse_args()

    detector = PresenceDetector(args.config)
    detector.start()
    signal.signal(signal.SIGTERM, detector.stop)
    signal.signal(signal.SIGINT, detector.stop)

    while not detector.stopped:
        time.sleep(1)


if __name__ == "__main__":
    main()
