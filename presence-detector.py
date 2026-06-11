#!/usr/bin/env python3
# pylint: disable=too-few-public-methods,invalid-name,too-many-instance-attributes

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
from typing import Any, Callable

from paho.mqtt import client as mqtt

VERSION = "3.0.0"


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
            "mqtt_host": "192.168.1.50",
            "mqtt_port": 1883,
            "mqtt_user": "ha",
            "mqtt_password": "",
            "interfaces": ["hostapd.wlan0"],
            "filter_is_denylist": True,
            "filter": [],
            "params": {},
            "location": "home",
            "away": "not_home",
            "fallback_sync_interval": 0,
            "source_type": "router",
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
    interface: str
    action: Action


class PresenceDetector(Thread):
    """Presence detector that uses ubus polling to detect online devices"""

    def __init__(self, config_file: str) -> None:
        super().__init__()
        self._settings = Settings(config_file)
        self._logger = Logger(self._settings.debug)
        self._connect_to_mqtt()
        self._queue: Queue = Queue()
        self._watchers: list[UbusWatcher] = []
        self._killed = False
        self._last_seen_clients: set[tuple[str, str]] = set()
        self._online_clients: dict[str, set[str]] = {}
        self._registered_clients: set[str] = set()
        for interface in self._settings.interfaces:
            self._online_clients[interface] = set()

    def _connect_to_mqtt(self):
        self._mqtt = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._mqtt.username_pw_set(
            self._settings.mqtt_user, self._settings.mqtt_password
        )
        self._mqtt.connect(
            self._settings.mqtt_host, self._settings.mqtt_port, keepalive=60
        )
        self._mqtt.reconnect_delay_set(min_delay=1, max_delay=60)
        self._mqtt.loop_start()

    def _publish(self, topic: str, data: str, retain=False) -> bool:
        self._logger.log(f"Publishing to {topic}: {data}", True)
        if not self._mqtt.is_connected():
            return False
        result = self._mqtt.publish(topic, data, qos=1, retain=retain)
        try:
            result.wait_for_publish(timeout=5)
        except RuntimeError as ex:
            self._logger.log(f"Error publishing to {topic}: {ex}", False)
            return False
        return True

    def _ha_seen(self, device: str, seen: bool = True) -> bool:
        """Call the HA device tracker 'see' service to update home/away status"""
        location = self._settings.location if seen else self._settings.away
        device_slug = device.replace(":", "_")
        if self._settings.ap_name:
            device_slug = f"{self._settings.ap_name}_{device_slug}"

        ok = True
        if device_slug not in self._registered_clients:
            self._registered_clients.add(device_slug)
            body = {
                "state_topic": f"homeassistant/device_tracker/{device_slug}/state",
                "name": device_slug,
                "payload_home": self._settings.location,
                "payload_not_home": self._settings.away,
                "source_type": self._settings.source_type,
                "device": {"connections": [["mac", device]]},
                "unique_id": device_slug,
            }
            if device in self._settings.params:
                body.update(self._settings.params[device])
            body["device"]["name"] = body["name"]
            # Register the device in HA
            ok &= self._publish(
                f"homeassistant/device_tracker/{device_slug}/config", json.dumps(body)
            )
        # Set the location
        ok &= self._publish(
            f"homeassistant/device_tracker/{device_slug}/state", location, retain=True
        )
        return ok

    def set_device_away(self, interface: str, device: str) -> None:
        """Mark a client as away in HA"""
        if not self._should_handle_device(device):
            return
        if device in self._online_clients[interface]:
            self._online_clients[interface].remove(device)
        for intf in set(self._settings.interfaces) - {interface}:
            if device in self._online_clients[intf]:
                # Device is still connected to another interface -> ignore
                self._logger.log(
                    f"Device {device} still connected to {intf}, ignoring away event.",
                    True,
                )
                return
        self._queue.put(QueueItem(device, interface, QueueItem.Action.DELETE))
        self._logger.log(f"Device {device} on {interface} is now away")

    def set_device_home(self, interface: str, device: str) -> None:
        """Add client to the 'add' queue"""
        if not self._should_handle_device(device):
            return
        self._queue.put(QueueItem(device, interface, QueueItem.Action.ADD))
        self._online_clients[interface].add(device)
        self._logger.log(
            f"Device {device} on {interface} is now at {self._settings.location}"
        )

    def _get_all_online_devices(self) -> list[tuple[str, str]]:
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
            devices.extend([(interface, key) for key in response["clients"].keys()])
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

    def stop(self, _signum: int | None = None, _frame: int | None = None):
        """Stop this thread as soon as possible"""
        self._logger.log("Stopping...")
        self.stop_watchers()
        self._killed = True
        self._queue.put(QueueItem("quit", "", QueueItem.Action.QUIT))
        self._mqtt.loop_stop()

    def _do_full_sync(self, away_only=False):
        """Perform a full sync of all current online devices compared to last time"""
        seen_now = set(self._get_all_online_devices())
        away = self._last_seen_clients - seen_now
        self._last_seen_clients = seen_now
        for interface, client in seen_now:
            if not away_only:
                self.set_device_home(interface, client)
        for interface, client in away:
            self.set_device_away(interface, client)

    def run(self) -> None:
        """Main loop for the presence detector"""
        self._do_full_sync()

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
        on_join: Callable[[str, str], None],
        on_leave: Callable[[str, str], None],
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
                    self._on_join(self._interface, event["assoc"]["address"].lower())
                elif "disassoc" in event:
                    self._on_leave(
                        self._interface, event["disassoc"]["address"].lower()
                    )
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
