#!/usr/bin/env python3
# pylint: disable=too-few-public-methods,invalid-name

"""
A Wi-Fi device presence detector for Home Assistant that runs on OpenWRT
"""

import argparse
import json
import signal
import subprocess
import syslog
import time
from threading import Thread
from typing import Dict, Any, List, Callable, Optional

from urllib import request
from urllib.error import URLError, HTTPError


class Logger:
    """Class to handle logging to syslog"""

    def __init__(self, enable_debug: bool) -> None:
        self.enable_debug = enable_debug

    def log(self, text: str, is_debug: bool = False) -> None:
        """Log a line to syslog. Only log debug messages when debugging is enabled."""
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
    """Loads all settings from a JSON file and provides built-in defaults"""

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
    """Presence detector that uses ubus polling to detect online devices"""

    def __init__(self, config_file: str) -> None:
        super().__init__()
        self._settings = Settings(config_file)
        self._logger = Logger(self._settings.debug)
        self._full_sync_counter = self._settings.full_sync_polls
        self._clients_seen: Dict[str, int] = {}
        self._watchers: List[UbusWatcher] = []
        self._killed = False

    @staticmethod
    def _post(url: str, data: dict, headers: dict):
        req = request.Request(
            url, data=json.dumps(data).encode("utf-8"), headers=headers
        )
        with request.urlopen(req, timeout=5) as response:
            return type(
                "", (), {"content": response.read(), "ok": response.code < 400}
            )()

    def _ha_seen(self, client: str, seen: bool = True) -> bool:
        """Call the HA device tracker 'see' service to update home/away status"""
        if seen:
            location = self._settings.location
        else:
            location = self._settings.away

        body = {"mac": client, "location_name": location, "source_type": "router"}
        if client in self._settings.params:
            body.update(self._settings.params[client])

        try:
            response = self._post(
                f"{self._settings.hass_url}/api/services/device_tracker/see",
                data=body,
                headers={"Authorization": f"Bearer {self._settings.hass_token}"},
            )
            self._logger.log(f"API Response: {response.content!r}", is_debug=True)
        except (URLError, HTTPError, TimeoutError) as ex:
            self._logger.log(str(ex), is_debug=True)
            # Force full sync when HA returns
            self._full_sync_counter = 0
            return False

        if not response.ok:
            self._full_sync_counter = 0

        return response.ok

    def full_sync(self) -> None:
        """Syncs the state of all devices once every X polls"""
        self._full_sync_counter -= 1
        if self._full_sync_counter <= 0:
            sync_ok = True
            for client, offline_after in self._clients_seen.copy().items():
                if offline_after == self._settings.offline_after:
                    self._logger.log(f"full sync {client}", is_debug=True)
                    sync_ok &= self._ha_seen(client)
            # Reset timer only when all syncs were successful
            if sync_ok:
                self._full_sync_counter = self._settings.full_sync_polls

    def set_client_away(self, client: str) -> None:
        """Mark a client as away in HA"""
        self._logger.log(f"Device {client} is now away")
        if self._ha_seen(client, False):
            # Away call to HA was successful -> remove from list
            if client in self._clients_seen:
                del self._clients_seen[client]
        else:
            # Call failed -> retry next time
            self._clients_seen[client] = 1

    def set_client_home(self, client: str):
        """Mark a client as home in HA"""
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
        """Call ubus and get all online clients"""
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

    def _on_leave(self, client: str):
        """Callback for the Ubus watcher thread when a client leaves"""
        if self._settings.offline_after <= 1:
            self.set_client_away(client)

    def start_watchers(self):
        """Start ubus watcher threads for every interface"""
        for interface in self._settings.interfaces:
            # Start an ubus watcher for every interface
            watcher = UbusWatcher(interface, self.set_client_home, self._on_leave)
            watcher.start()
            self._watchers.append(watcher)

    def stop_watchers(self):
        """Signal all ubus watchers to stop"""
        for watcher in self._watchers:
            watcher.stop()

    @property
    def stopped(self):
        """Should this Thread be stopped?"""
        return self._killed

    def stop(self, _signum: Optional[int] = None, _frame: Optional[int] = None):
        """Stop this thread as soon as possible"""
        self._logger.log("Stopping...")
        self.stop_watchers()
        self._killed = True

    def run(self) -> None:
        """Main loop for the presence detector"""
        # Start ubus watcher(s) for every interface
        self.start_watchers()

        # The main (sync) polling loop
        while not self._killed:
            seen_now = self._get_all_online_clients()
            # Periodically perform a full sync of all clients in case of connection failure
            self.full_sync()
            # Perform a regular 'changes only' sync with HA
            for client in seen_now:
                self.set_client_home(client)

            # Mark unseen clients as away after 'offline_after' intervals
            for client in self._clients_seen.copy():
                if client in seen_now:
                    continue
                self._clients_seen[client] -= 1
                if self._clients_seen[client] > 0:
                    continue
                # Client has not been seen x times, mark as away
                self.set_client_away(client)

            time.sleep(self._settings.poll_interval)

            self._logger.log(f"Clients seen: {self._clients_seen}", is_debug=True)


class UbusWatcher(Thread):
    """Watches live ubus events and signals presence detector of leave/join events"""

    def __init__(
        self,
        interface: str,
        on_join: Callable[[str], None],
        on_leave: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._on_join = on_join
        self._on_leave = on_leave
        self._interface = interface
        self._killed = False

    def stop(self):
        """Stops this watcher thread"""
        self._killed = True

    def run(self) -> None:
        """Main loop for the ubus event watcher thread"""
        while not self._killed:
            # pylint: disable=consider-using-with
            ubus = subprocess.Popen(
                ["ubus", "subscribe", self._interface],
                stdout=subprocess.PIPE,
                text=True,
            )
            # Give ubus time to start and/or fail
            time.sleep(1)
            # Check if it failed to start
            return_code = ubus.poll()
            if return_code is not None or ubus.stdout is None:
                # Starting ubus failed -> interface does not exist (yet)? let's retry later
                ubus.wait()
                continue
            # Startup OK, start reading stdout
            while not self._killed:
                line = ubus.stdout.readline()
                event = {}
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Ignore incomplete / invalid json
                    pass
                if "assoc" in event:
                    self._on_join(event["assoc"]["address"])
                elif "disassoc" in event:
                    self._on_leave(event["disassoc"]["address"])
            ubus.terminate()
            ubus.wait()


def main():
    """Main entrypoint: parse arguments and start all threads"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        help="Filename of configuration file",
        default="/etc/config/presence-detector.settings.json",
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
