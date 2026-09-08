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
            "mqtt_username": "ha",
            "mqtt_password": "",
            "mqtt_retain_state": True,
            "interfaces": [],
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
        if not self._settings["interfaces"]:
            self._settings["interfaces"] = self.list_wifi_interfaces()
            self._auto_detect_interfaces = True
        else:
            self._auto_detect_interfaces = False

    @property
    def auto_detect_interfaces(self) -> bool:
        """Whether interfaces were auto-detected (vs an explicit user-supplied list)"""
        return self._auto_detect_interfaces

    def __getattr__(self, item: str) -> Any:
        return self._settings.get(item)

    def list_wifi_interfaces(self) -> list[str]:
        """List all wifi interfaces. Returns [] (rather than raising) on
        failure, so a caller doing periodic re-detection at runtime doesn't
        crash the whole process over a transient ubus hiccup."""
        try:
            output = subprocess.run(
                ["ubus", "list", "hostapd.*"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        except (subprocess.CalledProcessError, OSError):
            return []
        interfaces = output.stdout.decode("utf-8").strip().split("\n")
        return [i for i in interfaces if i]
        output = subprocess.run(
            ["ubus", "list", "hostapd.*"], stdout=subprocess.PIPE, check=True
        )
        return output.stdout.decode("utf-8").strip().split("\n")

    @staticmethod
    def deep_merge(dict1: dict, dict2: dict):
        """Deep merge two dictionaries"""
        result = dict1.copy()
        for key, value in dict2.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = Settings.deep_merge(result[key], value)
            else:
                result[key] = value
        return result


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
        self._queue: Queue = Queue()
        self._watchers: list[UbusWatcher] = []
        self._killed = False
        self._last_seen_clients: set[tuple[str, str]] | None = None
        self._online_clients: dict[str, set[str]] = {}
        self._registered_clients: set[str] = set()
        for interface in self._settings.interfaces:
            self._online_clients[interface] = set()
        self._connect_to_mqtt()

    def _connect_to_mqtt(self):
        if hasattr(mqtt, "CallbackAPIVersion"):
            self._mqtt = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2
            )
        else:
            # Version 1 is deprecated but still supported
            self._mqtt = mqtt.Client()
        self._mqtt.on_connect = self._on_mqtt_connect
        self._mqtt.on_disconnect = self._on_mqtt_disconnect
        if hasattr(self._mqtt, "on_connect_fail"):
            self._mqtt.on_connect_fail = self._on_mqtt_connect_fail
        self._mqtt.username_pw_set(
            self._settings.mqtt_username, self._settings.mqtt_password
        )
        self._mqtt.reconnect_delay_set(min_delay=1, max_delay=60)
        self._mqtt.message_callback_add(
            "homeassistant/status", self._on_ha_status_message
        )
        self._mqtt.connect_async(
            self._settings.mqtt_host, self._settings.mqtt_port, keepalive=60
        )
        self._mqtt.loop_start()

    def _on_mqtt_connect(
        self, _client, _userdata, _flags, reason_code, _properties=None
    ):
        """Callback for MQTT connection (supports both v1 and v2 API)"""
        is_failure = (
            reason_code.is_failure
            if hasattr(reason_code, "is_failure")
            else reason_code != 0
        )
        if is_failure:
            self._logger.log(f"MQTT broker connection failed (rc: {reason_code})")
            return
        self._logger.log("MQTT broker connected")
        self._mqtt.subscribe("homeassistant/status")

    def _on_mqtt_connect_fail(self, _client, _userdata):
        """Callback for MQTT connection failures"""
        self._logger.log("MQTT broker connection failed, retrying...")

    def _on_mqtt_disconnect(self, *args, **_kwargs):
        """Callback for MQTT disconnections (supports both v1 and v2 API)"""
        reason_code = args[3] if len(args) >= 4 else (args[2] if len(args) >= 3 else 0)
        self._logger.log(f"MQTT broker disconnected (rc: {reason_code})")
        self._registered_clients.clear()

    def _on_ha_status_message(self, _client, _userdata, message):
        """Callback for HA status messages"""
        if message.payload == b"offline":
            self._logger.log("Home Assistant is offline!")
            self._registered_clients.clear()
        elif message.payload == b"online":
            self._logger.log("Home Assistant is back online")
            self._do_full_sync()

    def _publish(self, topic: str, data: str, retain=False) -> bool:
        self._logger.log(f"Publishing to {topic}: {data}", True)
        if not self._mqtt.is_connected():
            return False
        result = self._mqtt.publish(topic, data, qos=1, retain=retain)
        try:
            result.wait_for_publish(timeout=5)
        except (RuntimeError, ValueError) as ex:
            self._logger.log(f"Error publishing to {topic}: {ex}", False)
            return False
        return result.is_published()

    def _ha_seen(self, device: str, seen: bool = True) -> bool:
        """Publish MQTT messages register the device and update home/away status"""
        device_slug = device_name = device.replace(":", "_")
        if self._settings.ap_name:
            device_slug = f"{self._settings.ap_name}_{device_slug}"

        ok = True
        if device_slug not in self._registered_clients:
            self._registered_clients.add(device_slug)
            body = {
                "state_topic": f"homeassistant/device_tracker/{device_slug}/state",
                "json_attributes_topic": f"homeassistant/device_tracker/{device_slug}/state",
                "value_template": "{{ value_json['state'] }}",
                "name": device_name,
                "platform": "device_tracker",
                "payload_home": self._settings.location,
                "payload_not_home": self._settings.away,
                "source_type": self._settings.source_type,
                "device": {"connections": [["mac", device]]},
                "unique_id": device_slug,
            }
            if device in self._settings.params:
                body = Settings.deep_merge(body, self._settings.params[device])
                if "name" not in body["device"]:
                    body["device"]["name"] = body["name"]
            # Register the device in HA
            ok &= self._publish(
                f"homeassistant/device_tracker/{device_slug}/config", json.dumps(body)
            )
        # Set the state of the device
        state = {
            "in_zones": [f"zone.{self._settings.location}"] if seen else [],
            "state": self._settings.location if seen else self._settings.away,
        }
        ok &= self._publish(
            f"homeassistant/device_tracker/{device_slug}/state",
            json.dumps(state),
            retain=self._settings.mqtt_retain_state,
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
        self._logger.log(
            f"Starting ubus watchers on interfaces {self._settings.interfaces}"
        )
        for interface in self._settings.interfaces:
            # Start an ubus watcher for every interface
            watcher = UbusWatcher(interface, self.set_device_home, self.set_device_away)
            watcher.start()
            self._watchers.append(watcher)

    def stop_watchers(self) -> None:
        """Signal all ubus watchers to stop"""
        for watcher in self._watchers:
            watcher.stop()

    def _recheck_interfaces(self) -> None:
        """Detect radios that appeared or disappeared after startup and
        start/stop watchers accordingly (see #90). Interface auto-detection
        (self._settings.interfaces resolved from `ubus list 'hostapd.*'`)
        only happens once, in Settings.__init__ - a radio still doing a DFS
        CAC scan, slow to come up on boot, recreated by a wifi reload, or
        renamed/renumbered by a channel switch or firmware upgrade is
        otherwise never watched for the life of the process, and
        set_device_away's "still connected on another watched interface"
        check can't see a device on an interface nothing ever added to
        self._settings.interfaces either - producing a false away exactly
        as reported. Only applies when auto-detecting; an explicit
        interfaces list is a deliberate choice we don't second-guess."""
        if not self._settings.auto_detect_interfaces:
            return

        # Prune watchers that gave up (see UbusWatcher.gave_up) - their own
        # thread has already exited, so nothing to stop here, just drop the
        # bookkeeping so the same name is treated as fresh if it reappears.
        gone = [w for w in self._watchers if w.gave_up]
        for watcher in gone:
            self._logger.log(
                f"Interface {watcher.interface} confirmed gone; no longer tracking it"
            )
            self._watchers.remove(watcher)
            if watcher.interface in self._settings.interfaces:
                self._settings.interfaces.remove(watcher.interface)
            self._online_clients.pop(watcher.interface, None)

        current = set(self._settings.list_wifi_interfaces())
        watched = {w.interface for w in self._watchers}
        for interface in current - watched:
            self._logger.log(
                f"New wifi interface detected: {interface}; starting a watcher for it"
            )
            self._settings.interfaces.append(interface)
            self._online_clients[interface] = set()
            watcher = UbusWatcher(interface, self.set_device_home, self.set_device_away)
            watcher.start()
            self._watchers.append(watcher)
            # A newly-appeared interface may already have clients associated
            # from before we started watching it - the next full sync picks
            # them up on its own via _get_all_online_devices, nothing else
            # to do here.

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
        self._mqtt.disconnect()
        self._mqtt.loop_stop()

    def _do_full_sync(self, away_only=False):
        """Perform a full sync of all current online devices compared to last time"""
        self._registered_clients = set()
        seen_now = set(self._get_all_online_devices())
        is_first_sync = self._last_seen_clients is None
        away = (self._last_seen_clients or set()) - seen_now
        self._last_seen_clients = seen_now
        for interface, client in seen_now:
            if not away_only:
                self.set_device_home(interface, client)
        for interface, client in away:
            self.set_device_away(interface, client)

        if is_first_sync:
            # First-sync fix for #81: without this, a params-listed device
            # that's currently offline but was previously marked home via a
            # retained MQTT message stays stuck home forever.
            seen_macs = {client for _interface, client in seen_now}
            for device in self._settings.params:
                if device in seen_macs or not self._should_handle_device(device):
                    continue
                self._logger.log(
                    f"Device {device} is away (first sync, no prior state)", True
                )
                # Call _ha_seen directly rather than set_device_away: the
                # latter's per-interface bookkeeping (self._online_clients)
                # is keyed by real interface names and is only meaningful
                # for devices that were actually observed on one, which by
                # definition isn't the case here.
                self._ha_seen(device, seen=False)

    # How often to re-check for newly-appeared or gone wifi interfaces when
    # auto-detecting (see _recheck_interfaces). Independent of
    # fallback_sync_interval, which can be 0/disabled and is semantically
    # about "full state sync", not "interface discovery".
    INTERFACE_RECHECK_INTERVAL = 30

    def run(self) -> None:
        """Main loop for the presence detector"""
        self._do_full_sync()

        # Start ubus watcher(s) for every interface
        self.start_watchers()

        mq_is_offline = False
        fallback_sync_interval = self._settings.fallback_sync_interval
        auto_detect = self._settings.auto_detect_interfaces

        # The queue timeout drives both periodic full syncs and, when
        # auto-detecting, the interface recheck - use whichever period is
        # shorter (or don't time out at all if neither applies) as the
        # actual wait, and track each cadence's own elapsed time against
        # ticks of that duration so a short interface-recheck period
        # doesn't silently make full syncs happen more often than
        # configured, or vice versa.
        if auto_detect and fallback_sync_interval > 0:
            queue_timeout = min(
                fallback_sync_interval, self.INTERFACE_RECHECK_INTERVAL
            )
        elif auto_detect:
            queue_timeout = self.INTERFACE_RECHECK_INTERVAL
        elif fallback_sync_interval > 0:
            queue_timeout = fallback_sync_interval
        else:
            queue_timeout = None
        elapsed_since_full_sync = 0.0
        elapsed_since_iface_recheck = 0.0

        # The main (sync) polling loop
        while not self._killed:
            try:
                item: QueueItem = self._queue.get(timeout=queue_timeout)
            except queue.Empty:
                # queue_timeout is None whenever neither cadence applies, so
                # this branch (and the elapsed-time bookkeeping below) is
                # only ever reached when at least one of them is active.
                elapsed_since_full_sync += queue_timeout
                elapsed_since_iface_recheck += queue_timeout
                if (
                    fallback_sync_interval > 0
                    and elapsed_since_full_sync >= fallback_sync_interval
                ):
                    elapsed_since_full_sync = 0.0
                    self._do_full_sync()
                if (
                    auto_detect
                    and elapsed_since_iface_recheck >= self.INTERFACE_RECHECK_INTERVAL
                ):
                    elapsed_since_iface_recheck = 0.0
                    self._recheck_interfaces()
                continue

            if item.action == QueueItem.Action.QUIT:
                self._queue.task_done()
                break

            if self._ha_seen(item.device, item.action == QueueItem.Action.ADD):
                if mq_is_offline:
                    # We're back online -> process backlog
                    mq_is_offline = False
                    self._do_full_sync()
            else:
                self._logger.log("MQTT broker seems to be offline, sleeping...")
                # MQTT is offline -> Add the item back to the queue
                # and perform a full sync when it's back
                self._queue.put(item)
                mq_is_offline = True
                time.sleep(5)

            self._queue.task_done()


class UbusWatcher(Thread):
    """Watches live ubus events and signals presence detector of leave/join events"""

    # Consecutive failed-to-start attempts (~1s apart, see the sleep below)
    # before giving up on an interface that never manages to subscribe -
    # long enough to rule out "still booting" / "hasn't finished a DFS CAC
    # scan yet", short enough not to sit blind for too long on a genuinely
    # renamed/removed interface. Mirrors the shell rewrite's equivalent
    # threshold (used there for the same purpose).
    GIVE_UP_AFTER_ATTEMPTS = 300

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
        self._gave_up = False

    def stop(self):
        """Stops this watcher thread"""
        self._killed = True

    @property
    def interface(self) -> str:
        """The interface this watcher is (or was) watching"""
        return self._interface

    @property
    def gave_up(self) -> bool:
        """True once this watcher has given up on ever subscribing to its
        interface (see GIVE_UP_AFTER_ATTEMPTS) and stopped its own loop -
        the caller should stop tracking this interface, and treat the same
        name reappearing later as brand new rather than permanently ignored."""
        return self._gave_up

    def run(self) -> None:
        """Main loop for the ubus event watcher thread"""
        consecutive_failures = 0
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
                consecutive_failures += 1
                if consecutive_failures >= self.GIVE_UP_AFTER_ATTEMPTS:
                    self._gave_up = True
                    return
                continue
            # A subscription that actually starts proves the interface
            # exists - reset the counter so a later, unrelated drop doesn't
            # inherit an already-high failure count.
            consecutive_failures = 0
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
