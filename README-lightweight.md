# Lightweight (shell) presence detector

> **About this fork.** This is a fork of
> [rmoesbergen/openwrt-ha-device-tracker](https://github.com/rmoesbergen/openwrt-ha-device-tracker).
> All original credit for the design, the MQTT/discovery protocol, and the
> Python implementation goes to the upstream author. This fork **adds one
> thing**: `presence-detector.sh`, a pure-shell reimplementation for routers
> too small to run Python (see below). The upstream `presence-detector.py`,
> its README, and the settings format are kept intact and unchanged so the two
> implementations stay interchangeable. Fixes to shared behavior are offered
> back upstream where they apply.

`presence-detector.sh` is a pure POSIX shell (ash/busybox) rewrite of
`presence-detector.py`. It provides the **same functionality** and is a
**drop-in replacement on the Home Assistant side** (identical MQTT topics,
discovery config and state payloads), but uses a tiny fraction of the flash
storage.

Use this version if your router has limited free space and you cannot install
Python and its dependencies (e.g. small 8/16 MB devices).

## Why it's smaller

The Python version needs `python3-light` plus several Python packages
(`python3-paho-mqtt`, `python3-codecs`, `python3-urllib`, `python3-logging`),
which together take roughly **5–10 MB** of flash.

The shell version needs only:

| Dependency         | Notes                                                        |
|--------------------|--------------------------------------------------------------|
| `mosquitto-client` | Provides `mosquitto_pub` / `mosquitto_sub` (~50–150 KB)      |
| `jsonfilter`       | Part of base OpenWRT — used to parse the settings file       |
| `ubus`             | Part of base OpenWRT                                         |
| `logger`, `grep`, `tr`, `awk`, `sort` | All provided by busybox (already present) |

In practice this is around **100–200 KB** instead of several megabytes, and
`jsonfilter`/`ubus`/busybox are already installed on every OpenWRT system.

## How it works

The behavior mirrors the Python version:

* On start it performs a **full sync** via `ubus call <iface> get_clients` and
  publishes `home` for every connected client.
* It runs one **`ubus subscribe <iface>`** watcher per Wi-Fi interface and
  reacts to `assoc` (join) and `disassoc` (leave) events in realtime.
* It subscribes to **`homeassistant/status`** and performs a full re-sync when
  Home Assistant comes back online, and clears its registration cache when HA
  goes offline (so devices get re-announced via MQTT discovery).
* An optional **`fallback_sync_interval`** triggers a periodic full sync.

Registration state (which devices have already had their discovery config
published) is tracked with marker files under `/var/run/presence-detector/`,
so the asynchronous watchers and the main loop share state without threads.

## Installation

On your OpenWRT device:

1. Install the only extra dependency:
   ```bash
   opkg update && opkg install mosquitto-client
   # or, on apk-based OpenWRT (snapshot):
   # apk update && apk add mosquitto-client
   ```
   (`jsonfilter` and `ubus` are already part of the base system.)

2. Copy `presence-detector.sh` and your settings file to a persistent location
   (e.g. `/etc/config`). Start from the provided example and fill in your own
   MQTT credentials:
   ```bash
   cp presence-detector.sh /etc/config/presence-detector.sh
   cp presence-detector.settings.json.example /etc/config/presence-detector.settings.json
   # then edit /etc/config/presence-detector.settings.json (set mqtt_password, etc.)
   chmod +x /etc/config/presence-detector.sh
   ```

3. Install the init script (renamed to the standard service name):
   ```bash
   cp init.d/presence-detector-sh /etc/init.d/presence-detector
   chmod +x /etc/init.d/presence-detector
   ```

4. Adjust `/etc/config/presence-detector.settings.json` to your needs — the
   format is **identical** to the Python version. See the
   [main README](README.md#openwrt-device-configuration) for a full
   description of every setting.

5. Enable and start the service:
   ```bash
   service presence-detector enable
   service presence-detector start
   ```
   (or simply reboot).

The Home Assistant side is unchanged — see
[Steps to perform in Home Assistant](README.md#steps-to-perform-in-home-assistant).

## Settings

The settings file is the same `presence-detector.settings.json` used by the
Python version. A sanitized template ships as
[`presence-detector.settings.json.example`](presence-detector.settings.json.example) —
copy it and fill in your own values. **Never commit a settings file
containing your real `mqtt_password`** (the repo's `.gitignore` already
excludes `presence-detector.settings.json` and
`presence-detector.settings.*.json`). All settings are supported:

`mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password`,
`mqtt_retain_state`, `interfaces`, `filter_is_denylist`, `filter`, `params`,
`ap_name`, `location`, `away`, `fallback_sync_interval`, `source_type`,
`debug`.

Notes / minor differences:

* **`params`**: The shell version reads the per-device `name` and `icon`
  overrides (the two commonly used keys). If you need to pass through other
  arbitrary discovery keys, use the Python version — the shell version keeps
  the params handling deliberately simple to stay small.
* **`interfaces`**: As with the Python version, leave empty (`[]`) to
  auto-detect all `hostapd.*` interfaces.
* **`ap_name`**: Prefixes the entity — both its `unique_id`/MQTT topic **and**
  its name — with the AP name (e.g. `device_tracker.cocina_aa_bb_...`). Leave
  as `""` for a single access point. Set a **distinct** value per AP when
  running on multiple access points (see
  [Multiple access points & roaming](#multiple-access-points--roaming)).
* **`debug`**: When `true`, debug lines are written to syslog (`logread`).

## Multiple access points & roaming

If you run this on several APs (e.g. one per floor/room) and the same phones
roam through the house, follow this pattern so a device that moves between APs
stays reliably "home".

### Give each AP a distinct `ap_name`

Set a **different** `ap_name` on every router:

```jsonc
// cocina router
{ "ap_name": "cocina",     "...": "..." }
// living-room router
{ "ap_name": "livingroom", "...": "..." }
// bedroom router
{ "ap_name": "bedroom",    "...": "..." }
```

The same phone MAC then produces **one entity per AP**, all distinct:

```
device_tracker.cocina_54_32_04_3d_b4_c8
device_tracker.livingroom_54_32_04_3d_b4_c8
device_tracker.bedroom_54_32_04_3d_b4_c8
```

Because every tracker carries the same `device.connections` MAC, Home Assistant
groups all three under a **single device** per phone — while keeping them as
separate tracker entities (distinct `unique_id`s).

> **Do not use `params` name overrides in a multi-AP setup.** If all APs give
> the same MAC the same friendly name, HA cannot derive distinct entity IDs and
> will append numeric suffixes (`..._2`, `..._3`). Leave the auto
> `<ap>_<mac>` naming in place and give devices a human-friendly identity via a
> Person (below).

### Why not one shared entity across APs?

It is tempting to leave `ap_name` empty on all APs so they publish to the *same*
topic (one entity per MAC). **Don't** — it creates a race on roaming: when a
phone moves from `cocina` to `livingroom`, `livingroom` publishes `home` while
`cocina` publishes `not_home` for the same entity. Depending on ordering, the
stale `not_home` can win and the device flips away even though it is connected.
Each router only knows its own clients, so they cannot resolve this between
themselves.

### Combine per person with a Person entity (recommended)

Home Assistant's **Person** entity is the built-in, roaming-safe combiner: a
Person is `home` if **any** of its assigned device_trackers is home. Assign all
of a person's per-AP trackers to their Person:

* Settings → People → *(person)* → **"Select the devices that belong to this
  person"** → add `device_tracker.cocina_<mac>`,
  `device_tracker.livingroom_<mac>`, `device_tracker.bedroom_<mac>`.

As the phone roams, whichever AP currently sees it keeps the Person `home`,
absorbing the per-AP `assoc`/`disassoc` race. Base your automations on
`person.<name>` instead of the individual trackers.

> A `device_tracker` **group helper** is *not* available in Home Assistant
> (the group helper only supports `binary_sensor`, `light`, `switch`, etc.), so
> the Person entity — or a template `binary_sensor` that is `on` when any AP
> tracker is `home` — is the correct way to OR the per-AP trackers.

### Adding a new AP later

1. Install `mosquitto-client` and the service on the new router (same steps as
   [Installation](#installation)).
2. Use a settings file identical to your other APs but with a new `ap_name`.
3. Start the service; the new `device_tracker.<newap>_<mac>` entities appear.
4. In HA, add each new tracker to the relevant Person.

## Running manually / debugging

You can run it in the foreground to watch what it does:

```bash
/etc/config/presence-detector.sh -c /etc/config/presence-detector.settings.json
```

Log output goes to syslog; read it with:

```bash
logread -e presence-detector
```

With `"debug": true` you will see lines like:

```text
daemon.debug presence-detector[1234]: Publishing to homeassistant/device_tracker/xx_xx_xx_xx_xx_xx/state: {"in_zones":["zone.home"],"state":"home"}
```

## Choosing between the shell and Python versions

| | `presence-detector.sh` (shell) | `presence-detector.py` (Python) |
|---|---|---|
| Flash usage | ~100–200 KB (+ base tools) | ~5–10 MB (Python + deps) |
| Dependencies | `mosquitto-client` | `python3-light`, `python3-paho-mqtt`, … |
| Realtime events | ✅ `ubus subscribe` | ✅ `ubus subscribe` |
| MQTT auto-discovery | ✅ | ✅ |
| HA online/offline recovery | ✅ | ✅ |
| Fallback periodic sync | ✅ | ✅ |
| Arbitrary `params` passthrough | `name` + `icon` only | Any discovery key |

For most single/dual-radio access points the shell version is fully
equivalent. Pick the Python version only if you rely on advanced `params`
passthrough.
