#!/usr/bin/env python3
# pylint: disable=too-few-public-methods,invalid-name

"""
A Wi-Fi device presence detector for Home Assistant that runs on OpenWRT
"""

import argparse
import json
import queue
import signal
import subprocess
import syslog
import time
from dataclasses import dataclass
from enum import IntEnum
from queue import Queue
from threading import Thread
from typing import Any, List, Callable, Optional, Tuple
from urllib import request

VERSION = "2.1.1"


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
            "filter_is_denylist": True,
            "filter": [],
            "params": {},
            "location": "home",
            "away": "not_home",
            "fallback_sync_interval": 0,
            "debug": False,
        }
        with open(config_file, "r", encoding="utf-8") as settings:
            self._settings.update(json.load(settings))

        # Lowercase all MAC addresses in the filter and params settings
        self._settings["filter"] = [device.lower() for device in self.filter]
        self._settings["params"] = {
            device.lower(): params for device, params in self.params.items()
        }

    def __getattr__(self, item: str) -> Any:
        return self._settings.get(item)


@dataclass
class QueueItem:
    """Represents a device item on the queue"""

    class Action(IntEnum):
        """Possible queue item actions"""

        ADD = 1
        DELETE = 2
        QUIT = 3

    device: str
    action: Action


class PresenceDetector(Thread):
    """Presence detector that uses ubus polling to detect online devices"""

    def __init__(self, config_file: str) -> None:
        super().__init__()
        self._settings = Settings(config_file)
        self._logger = Logger(self._settings.debug)
        self._queue: Queue = Queue()
        self._watchers: List[UbusWatcher] = []
        self._killed = False
        self._last_seen_clients: set[str] = set([])

    @staticmethod
    def _post(url: str, data: dict, headers: dict) -> Tuple[str, bool]:
        req = request.Request(
            url, data=json.dumps(data).encode("utf-8"), headers=headers
        )
        with request.urlopen(req, timeout=5) as response:
            return response.read(), response.code < 400

    def _ha_seen(self, device: str, seen: bool = True) -> bool:
        """Call the HA device tracker 'see' service to update home/away status"""
        if seen:
            location = self._settings.location
        else:
            location = self._settings.away

        body = {"mac": device, "location_name": location, "source_type": "router"}
        if device in self._settings.params:
            body.update(self._settings.params[device])
        if self._settings.ap_name:
            body["mac"] = f"{self._settings.ap_name}_{device}"

        self._logger.log(f"Posting to HA: {body}", True)

        try:
            response, ok = self._post(
                f"{self._settings.hass_url}/api/services/device_tracker/see",
                data=body,
                headers={"Authorization": f"Bearer {self._settings.hass_token}"},
            )
            self._logger.log(f"API Response: {response!r}", is_debug=True)
        except Exception as ex:  # pylint: disable=broad-except
            self._logger.log(str(ex), is_debug=True)
            return False

        return ok

    def set_device_away(self, device: str) -> None:
        """Mark a client as away in HA"""
        if not self._should_handle_device(device):
            return
        self._queue.put(QueueItem(device, QueueItem.Action.DELETE))
        self._logger.log(f"Device {device} is now away")

    def set_device_home(self, device: str) -> None:
        """Add client to the 'add' queue"""
        if not self._should_handle_device(device):
            return
        self._queue.put(QueueItem(device, QueueItem.Action.ADD))
        self._logger.log(f"Device {device} is now at {self._settings.location}")

    def _get_all_online_devices(self) -> List[str]:
        """Call ubus and get all online devices"""
        devices = []
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
            response: dict = json.loads(process.stdout)
            devices.extend(response["clients"].keys())
        return devices

    def _should_handle_device(self, device: str) -> bool:
        """Check if a device should be handled by checking the allow/deny list"""
        if device in self._settings.filter:
            return not self._settings.filter_is_denylist
        return self._settings.filter_is_denylist

    def start_watchers(self) -> None:
        """Start ubus watcher threads for every interface"""
        for interface in self._settings.interfaces:
            # Start an ubus watcher for every interface
            watcher = UbusWatcher(interface, self.set_device_home, self.set_device_away)
            watcher.start()
            self._watchers.append(watcher)

    def stop_watchers(self) -> None:
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
        self._queue.put(QueueItem("quit", QueueItem.Action.QUIT))

    def _do_full_sync(self, away_only=False):
        """Perform a full sync of all currently online devices compared to last time"""
        seen_now = set(self._get_all_online_devices())
        away = self._last_seen_clients - seen_now
        self._last_seen_clients = seen_now
        if not away_only:
            for client in seen_now:
                self.set_device_home(client)
        for client in away:
            self.set_device_away(client)

    def _update_version_entity(self):
        """Create a script version entity in home assistant"""
        ap_name = (
            self._settings.ap_name.replace("-", "_").lower()
            if self._settings.ap_name
            else "openwrt_router"
        )
        entity_id = f"sensor.{ap_name}_presence_detector_version"

        response, ok = self._post(
            f"{self._settings.hass_url}/api/states/{entity_id}",
            data={"state": VERSION},
            headers={"Authorization": f"Bearer {self._settings.hass_token}"},
        )
        if not ok:
            self._logger.log(
                f"Unable to create/update version entity in HA: {response}"
            )

    def run(self) -> None:
        """Main loop for the presence detector"""
        self._do_full_sync()

        # Update the version entity in HA
        self._update_version_entity()

        # Start ubus watcher(s) for every interface
        self.start_watchers()

        ha_is_offline = False
        # Enable a queue timeout if fallback_sync interval is set
        queue_timeout = (
            self._settings.fallback_sync_interval
            if self._settings.fallback_sync_interval > 0
            else None
        )

        # The main (sync) polling loop
        while not self._killed:
            try:
                item: QueueItem = self._queue.get(timeout=queue_timeout)
            except queue.Empty:
                # Perform a periodic full sync
                self._do_full_sync()
                continue

            if item.action == QueueItem.Action.QUIT:
                self._queue.task_done()
                break

            if self._ha_seen(item.device, item.action == QueueItem.Action.ADD):
                if ha_is_offline:
                    # We're back online -> process backlog
                    ha_is_offline = False
                    self._do_full_sync()
                    # Update the version entity in HA
                    self._update_version_entity()
            else:
                self._logger.log("Home Assistant seems to be offline, sleeping...")
                # HA is offline -> Add the item back to the queue
                # and perform a full sync when it's back
                self._queue.put(item)
                ha_is_offline = True
                time.sleep(5)

            self._queue.task_done()


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
                    self._on_join(event["assoc"]["address"].lower())
                elif "disassoc" in event:
                    self._on_leave(event["disassoc"]["address"].lower())
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
