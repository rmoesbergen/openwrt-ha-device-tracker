#!/bin/sh
#
# A lightweight Wi-Fi device presence detector for Home Assistant that runs on
# OpenWRT. This is a pure POSIX/ash + busybox rewrite of presence-detector.py,
# designed to minimize flash storage usage.
#
# Dependencies (OpenWRT packages):
#   - mosquitto-client (provides mosquitto_pub / mosquitto_sub)
#   - jsonfilter       (part of base OpenWRT, used to parse JSON)
#   - ubus / ubusd     (part of base OpenWRT)
#
# It uses the exact same settings file and MQTT topic/payload format as the
# Python version, so it is a drop-in replacement on the Home Assistant side.

CONFIG="/etc/config/presence-detector.settings.json"

log() {
	# log <message> [is_debug]
	# Only log debug messages when debugging is enabled.
	if [ "$2" = "1" ] && [ "$DEBUG" != "1" ]; then
		return
	fi
	local level="daemon.info"
	[ "$2" = "1" ] && level="daemon.debug"
	logger -t "presence-detector" -p "$level" "$1"
}

# ---------------------------------------------------------------------------
# Settings handling
# ---------------------------------------------------------------------------

# Read a single scalar setting from the config file with a default fallback.
# Usage: get_setting <json-path> <default>
get_setting() {
	local value
	value=$(jsonfilter -i "$CONFIG" -e "$1" 2>/dev/null)
	if [ -z "$value" ]; then
		echo "$2"
	else
		echo "$value"
	fi
}

# Read an array setting as newline-separated values.
# Usage: get_array <json-path>
get_array() {
	jsonfilter -i "$CONFIG" -e "$1[*]" 2>/dev/null
}

load_settings() {
	if [ ! -f "$CONFIG" ]; then
		log "Config file $CONFIG not found!"
		exit 1
	fi

	MQTT_HOST=$(get_setting '@.mqtt_host' '192.168.1.50')
	MQTT_PORT=$(get_setting '@.mqtt_port' '1883')
	MQTT_USERNAME=$(get_setting '@.mqtt_username' 'ha')
	MQTT_PASSWORD=$(get_setting '@.mqtt_password' '')
	MQTT_RETAIN_STATE=$(get_setting '@.mqtt_retain_state' 'true')
	FILTER_IS_DENYLIST=$(get_setting '@.filter_is_denylist' 'true')
	AP_NAME=$(get_setting '@.ap_name' '')
	LOCATION=$(get_setting '@.location' 'home')
	AWAY=$(get_setting '@.away' 'not_home')
	FALLBACK_SYNC_INTERVAL=$(get_setting '@.fallback_sync_interval' '0')
	AWAY_DEBOUNCE_SECONDS=$(get_setting '@.away_debounce_seconds' '0')
	SOURCE_TYPE=$(get_setting '@.source_type' 'router')
	DEBUG=$(get_setting '@.debug' 'false')
	[ "$DEBUG" = "true" ] && DEBUG=1 || DEBUG=0

	# Filter list, lowercased
	FILTER=$(get_array '@.filter' | tr 'A-Z' 'a-z')

	# Interfaces: auto-detect if not specified. AUTO_DETECT_INTERFACES gates
	# the periodic re-detection in main() below - only relevant when the
	# user didn't pin an explicit list themselves.
	INTERFACES=$(get_array '@.interfaces')
	if [ -z "$INTERFACES" ]; then
		AUTO_DETECT_INTERFACES=1
		INTERFACES=$(ubus list 'hostapd.*' 2>/dev/null)
	else
		AUTO_DETECT_INTERFACES=0
	fi

	if [ -z "$INTERFACES" ]; then
		log "No wifi interfaces found or configured!"
		exit 1
	fi

	# Detect ucode (present on OpenWRT 22.03+). When available we build the
	# MQTT discovery config with a real JSON deep-merge, so ANY key in a
	# device's params block is honoured (including nested "device" keys such
	# as manufacturer / model / sw_version). Without ucode we fall back to a
	# minimal string-built config that supports only name + icon.
	if command -v ucode >/dev/null 2>&1; then
		HAS_UCODE=1
	else
		HAS_UCODE=0
		log "ucode not found: params limited to name+icon (install ucode / OpenWRT >= 22.03 for full params)"
	fi
}

# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

mqtt_pub() {
	# mqtt_pub <topic> <payload> [retain]
	local topic="$1"
	local payload="$2"
	local retain_flag=""
	[ "$3" = "1" ] && retain_flag="-r"

	local auth=""
	[ -n "$MQTT_USERNAME" ] && auth="-u $MQTT_USERNAME -P $MQTT_PASSWORD"

	log "Publishing to $topic: $payload" 1
	# shellcheck disable=SC2086
	mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" $auth \
		-q 1 $retain_flag -t "$topic" -m "$payload"
	return $?
}

# ---------------------------------------------------------------------------
# Registration state
# ---------------------------------------------------------------------------
# We track which device slugs have already been registered (config published)
# with marker files in a tmpfs runtime dir, so the async ubus watchers and the
# main process share state without needing threads.

RUNDIR="/var/run/presence-detector"
LAST_SEEN="/var/run/presence-detector.last_seen"

reset_registrations() {
	# Only clears the registered-slug markers (forces re-publish of discovery
	# config), NOT the last_seen tracking file or the .stop flag.
	mkdir -p "$RUNDIR"
	rm -f "$RUNDIR"/*.reg 2>/dev/null
}

is_registered() {
	[ -f "$RUNDIR/$1.reg" ]
}

mark_registered() {
	touch "$RUNDIR/$1.reg"
}

# ---------------------------------------------------------------------------
# Away-debounce state
# ---------------------------------------------------------------------------
# A device roaming between interfaces/bands on the same router (or briefly
# dropping due to power-save/signal) produces a disassoc immediately followed
# by a fresh assoc, often within well under a second. Without debouncing that
# publishes a home->away->home flap to HA for every such hop (see upstream
# issues #67 and #50). When AWAY_DEBOUNCE_SECONDS > 0, a disassoc doesn't
# publish "away" right away - it records a pending marker (named after the
# device slug, same identity ha_seen already uses) and a background check
# fires after the debounce window to publish "away" only if the marker is
# still there. Any assoc for that device in the meantime removes the marker
# first, so the delayed check finds it gone and does nothing - no flap.

mark_pending_away() {
	# mark_pending_away <device_slug>
	echo "$1" > "$RUNDIR/$1.pending_away" 2>/dev/null
}

clear_pending_away() {
	rm -f "$RUNDIR/$1.pending_away" 2>/dev/null
}

is_pending_away() {
	[ -f "$RUNDIR/$1.pending_away" ]
}

# ---------------------------------------------------------------------------
# Interface lifecycle state (auto-detect only)
# ---------------------------------------------------------------------------
# A watched interface that stops existing (radio disabled, genuinely renamed/
# renumbered rather than replaced) would otherwise retry `ubus subscribe`
# forever at a quiet 15s backoff - harmless on its own, but once interface
# auto-detection can ADD interfaces at runtime (see the periodic re-detect in
# main()), leaving the removal half unhandled means $INTERFACES only ever
# grows. watch_interface() marks itself gone here after enough consecutive
# instant-exit attempts to be confident the interface isn't coming back
# shortly, then exits its own loop; main()'s periodic re-detect prunes
# $INTERFACES using this marker (auto-detect only - an explicit interfaces
# list is never pruned, same as it's never auto-extended).

mark_interface_gone() {
	touch "$RUNDIR/iface_gone.$1" 2>/dev/null
}

is_interface_gone() {
	[ -f "$RUNDIR/iface_gone.$1" ]
}

clear_interface_gone() {
	rm -f "$RUNDIR/iface_gone.$1" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Device filtering
# ---------------------------------------------------------------------------

should_handle_device() {
	# Returns 0 (true) if the device should be handled.
	local device="$1"
	local in_list=0
	local mac
	for mac in $FILTER; do
		if [ "$mac" = "$device" ]; then
			in_list=1
			break
		fi
	done

	if [ "$in_list" = "1" ]; then
		# In list: handle only if it is an allowlist (denylist=false)
		[ "$FILTER_IS_DENYLIST" = "true" ] && return 1 || return 0
	fi
	# Not in list: handle only if it is a denylist
	[ "$FILTER_IS_DENYLIST" = "true" ] && return 0 || return 1
}

# device_slug: mac with ':' -> '_', optionally prefixed by ap_name. Shared by
# ha_seen and the away-debounce markers so both key off the same identity.
device_slug() {
	local device_name
	device_name=$(echo "$1" | tr ':' '_')
	if [ -n "$AP_NAME" ]; then
		echo "${AP_NAME}_${device_name}"
	else
		echo "$device_name"
	fi
}

# ---------------------------------------------------------------------------
# JSON escaping for params passthrough
# ---------------------------------------------------------------------------

# Publish the discovery config + state for a device.
# ha_seen <device-mac> <seen: 1|0>
ha_seen() {
	local device="$1"
	local seen="$2"

	local device_name
	local device_slug
	device_name=$(echo "$device" | tr ':' '_')
	device_slug=$(device_slug "$device")

	local ok=0

	if ! is_registered "$device_slug"; then
		mark_registered "$device_slug"

		local config_topic="homeassistant/device_tracker/${device_slug}/config"
		local state_topic="homeassistant/device_tracker/${device_slug}/state"
		local body

		if [ "$HAS_UCODE" = "1" ]; then
			# Full path: deep-merge the device's entire params block into the
			# discovery config via ucode, so arbitrary keys work (including
			# nested device keys: manufacturer, model, sw_version, ...).
			# Doubling fix: if the device ends up named, the entity name is
			# set to null so HA shows the device name once.
			body=$(ucode -l fs \
				-D CFG="$CONFIG" -D DEV="$device" -D SLUG="$device_slug" \
				-D LOC="$LOCATION" -D AWAY="$AWAY" -D STYPE="$SOURCE_TYPE" \
				-e '
					let fp = fs.open(CFG, "r");
					let settings = json(fp.read("all")); fp.close();
					let params = (settings.params && settings.params[DEV]) ? settings.params[DEV] : {};
					let st = sprintf("homeassistant/device_tracker/%s/state", SLUG);
					let body = {
						state_topic: st, json_attributes_topic: st,
						value_template: "{{ value_json[\x27state\x27] }}",
						name: replace(DEV, ":", "_"),
						platform: "device_tracker",
						payload_home: LOC, payload_not_home: AWAY,
						source_type: STYPE,
						device: { connections: [ ["mac", DEV] ] },
						unique_id: SLUG
					};
					function dm(a, b) {
						for (let k in b) {
							if (type(a[k]) == "object" && type(b[k]) == "object")
								dm(a[k], b[k]);
							else
								a[k] = b[k];
						}
						return a;
					}
					dm(body, params);
					if (!body.device.name && params.name) body.device.name = params.name;
					if (body.device.name) body.name = null;
					print(body);
				' 2>/dev/null)
		fi

		if [ -z "$body" ]; then
			# Fallback path (no ucode, or ucode failed): string-built config
			# supporting only name + icon overrides. Same doubling fix.
			local override_name
			override_name=$(jsonfilter -i "$CONFIG" -e "@.params['$device'].name" 2>/dev/null)
			local override_icon
			override_icon=$(jsonfilter -i "$CONFIG" -e "@.params['$device'].icon" 2>/dev/null)

			local name_field
			local device_block="\"connections\":[[\"mac\",\"$device\"]]"
			if [ -n "$override_name" ]; then
				name_field="\"name\":null,"
				device_block="$device_block,\"name\":\"$override_name\""
			else
				name_field="\"name\":\"$device_name\","
			fi
			local icon_field=""
			[ -n "$override_icon" ] && icon_field="\"icon\":\"$override_icon\","

			body="{\"state_topic\":\"$state_topic\",\"json_attributes_topic\":\"$state_topic\",\"value_template\":\"{{ value_json['state'] }}\",${name_field}${icon_field}\"platform\":\"device_tracker\",\"payload_home\":\"$LOCATION\",\"payload_not_home\":\"$AWAY\",\"source_type\":\"$SOURCE_TYPE\",\"device\":{$device_block},\"unique_id\":\"$device_slug\"}"
		fi

		# Retain the discovery config, not just state. Without this, if HA
		# is offline/restarting at the exact moment this one-shot config
		# message is published, HA has no way to learn about the entity's
		# MQTT subscription again until this script itself restarts (which
		# forces a fresh republish via reset_registrations). A retained
		# config lets HA's MQTT integration pick the entity back up on its
		# own reconnect.
		mqtt_pub "$config_topic" "$body" 1
		ok=$?
	fi

	# Publish state
	local state_topic="homeassistant/device_tracker/${device_slug}/state"
	local state_payload
	if [ "$seen" = "1" ]; then
		state_payload="{\"in_zones\":[\"zone.$LOCATION\"],\"state\":\"$LOCATION\"}"
	else
		state_payload="{\"in_zones\":[],\"state\":\"$AWAY\"}"
	fi

	local retain=0
	[ "$MQTT_RETAIN_STATE" = "true" ] && retain=1
	mqtt_pub "$state_topic" "$state_payload" "$retain"
	ok=$((ok + $?))

	return $ok
}

# ---------------------------------------------------------------------------
# Full sync
# ---------------------------------------------------------------------------

get_all_online_devices() {
	# Prints "interface mac" pairs for every currently connected client.
	local interface
	for interface in $INTERFACES; do
		ubus call "$interface" get_clients 2>/dev/null | \
			grep -o '"[0-9a-fA-F:]\{17\}"' | tr -d '"' | tr 'A-Z' 'a-z' | \
			sort -u | \
			while read -r mac; do
				[ -n "$mac" ] && echo "$interface $mac"
			done
	done
}

do_full_sync() {
	# Re-register everything and publish current state for all online devices.
	#
	# force=1 bypasses the debounce below. Used when we know we may have
	# missed events (e.g. a watcher just reconnected after an unexpected
	# drop) and a "we synced recently" skip would be wrong here specifically.
	local force="${1:-0}"

	# Debounce: the retained homeassistant/status "online" message is
	# delivered immediately after startup (right after main's own sync) and on
	# every mosquitto_sub reconnect. Without this guard those would each
	# trigger a redundant full re-sync. Skip if we synced < 30s ago.
	local now
	now=$(date +%s 2>/dev/null || echo 0)
	if [ "$force" != "1" ] && [ -f "$RUNDIR/.last_sync" ]; then
		local last
		last=$(cat "$RUNDIR/.last_sync" 2>/dev/null || echo 0)
		if [ "$now" -gt 0 ] && [ "$last" -gt 0 ] && [ $((now - last)) -lt 30 ]; then
			log "Skipping redundant full sync (last was $((now - last))s ago)" 1
			return 0
		fi
	fi
	echo "$now" > "$RUNDIR/.last_sync" 2>/dev/null

	reset_registrations
	local seen_file="$RUNDIR/.seen_now"
	get_all_online_devices | sort -u > "$seen_file"

	# Publish 'home' for everything currently online.
	while read -r interface mac; do
		[ -z "$mac" ] && continue
		if should_handle_device "$mac"; then
			ha_seen "$mac" 1
			log "Device $mac on $interface is now at $LOCATION" 1
		fi
	done < "$seen_file"

	# Any MAC in last_seen but no longer online is now away.
	if [ -f "$LAST_SEEN" ]; then
		# Compare on the MAC column only (a device may roam interfaces).
		local now_macs
		now_macs=$(awk '{print $2}' "$seen_file" | sort -u)
		awk '{print $2}' "$LAST_SEEN" | sort -u | while read -r mac; do
			[ -z "$mac" ] && continue
			if ! echo "$now_macs" | grep -qx "$mac"; then
				if should_handle_device "$mac"; then
					ha_seen "$mac" 0
					log "Device $mac is now away" 1
				fi
			fi
		done
	else
		# First-ever sync (fresh install, reinstall, or firmware upgrade): we
		# have no history to diff against, so the block above can't detect
		# "was home, now gone". Without this, a device that was marked home
		# via a RETAINED MQTT message from a previous install stays stuck at
		# home forever, since nothing ever tells HA otherwise.
		#
		# We can't discover arbitrary previously-tracked MACs from the router
		# alone, but every MAC listed in params is a known, named device we
		# can proactively check right now: if it's not in today's online
		# list, publish it away immediately instead of waiting for a
		# disassoc event (or the retire that never comes because it never
		# reassociates while we're watching).
		local now_macs
		now_macs=$(awk '{print $2}' "$seen_file" | sort -u)
		jsonfilter -i "$CONFIG" -e '@.params' 2>/dev/null | \
			grep -oE '"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}"' | tr -d '"' | tr 'A-Z' 'a-z' | sort -u | \
			while read -r mac; do
				[ -z "$mac" ] && continue
				if ! echo "$now_macs" | grep -qx "$mac"; then
					if should_handle_device "$mac"; then
						ha_seen "$mac" 0
						log "Device $mac is away (first sync, no prior state)" 1
					fi
				fi
			done
	fi

	cp "$seen_file" "$LAST_SEEN"
}

# ---------------------------------------------------------------------------
# Event watchers
# ---------------------------------------------------------------------------

# Watch a single interface's ubus events and act on assoc/disassoc.
watch_interface() {
	local interface="$1"
	# Consecutive instant-exit attempts (case 1 below). Reset whenever a
	# subscription actually stays up for a while (case 2) - that proves the
	# interface exists and is just having connectivity issues, which is not
	# the "gone for good" signal this counter looks for.
	local consecutive_quick_fails=0
	while [ ! -f "$RUNDIR/.stop" ]; do
		local start_ts
		start_ts=$(date +%s 2>/dev/null || echo 0)
		# ubus subscribe streams JSON events, one per line-ish.
		ubus subscribe "$interface" 2>/dev/null | while read -r line; do
			[ -f "$RUNDIR/.stop" ] && break
			case "$line" in
			*'"assoc"'*)
				local mac
				mac=$(echo "$line" | grep -o '"address":"[^"]*"' | \
					head -n1 | cut -d'"' -f4 | tr 'A-Z' 'a-z')
				[ -z "$mac" ] && continue
				if should_handle_device "$mac"; then
					# Cancel any pending debounced "away" for this device -
					# it just reappeared (same interface, another interface
					# on this router, or reconnected after a brief drop), so
					# the delayed check below must find nothing and no-op.
					clear_pending_away "$(device_slug "$mac")"
					log "Device $mac on $interface is now at $LOCATION" 1
					ha_seen "$mac" 1
				fi
				;;
			*'"disassoc"'*)
				local mac
				mac=$(echo "$line" | grep -o '"address":"[^"]*"' | \
					head -n1 | cut -d'"' -f4 | tr 'A-Z' 'a-z')
				[ -z "$mac" ] && continue
				if should_handle_device "$mac"; then
					if [ "$AWAY_DEBOUNCE_SECONDS" -gt 0 ] 2>/dev/null; then
						local slug
						slug=$(device_slug "$mac")
						mark_pending_away "$slug"
						log "Device $mac on $interface disassociated; debouncing ${AWAY_DEBOUNCE_SECONDS}s before marking away" 1
						(
							sleep "$AWAY_DEBOUNCE_SECONDS"
							# Don't act past shutdown, and only publish away
							# if nothing re-marked this device home (via
							# clear_pending_away) while we were sleeping.
							if [ ! -f "$RUNDIR/.stop" ] && is_pending_away "$slug"; then
								clear_pending_away "$slug"
								log "Device $mac is now away (debounced)" 1
								ha_seen "$mac" 0
							fi
						) &
						# Record the PID (append-only, never pruned at
						# runtime - a few bytes per roam event is negligible
						# on tmpfs) so cleanup() can reap it precisely by
						# PID. A plain `sleep` has nothing in its own
						# command line to distinguish it, so a pgrep -f name
						# match here would be exactly the kind of overbroad
						# match that self-matches unrelated ash processes;
						# killing a PID that already exited is a silent
						# no-op, so a stale entry is harmless.
						echo "$!" >> "$RUNDIR/.debounce_pids" 2>/dev/null
					else
						log "Device $mac on $interface is now away" 1
						ha_seen "$mac" 0
					fi
				fi
				;;
			esac
		done
		[ -f "$RUNDIR/.stop" ] && break

		# Distinguish two very different cases that both end up here:
		#
		#  1. The interface never existed / doesn't exist yet (typo, not-yet-
		#     configured radio, or genuinely renamed e.g. after a firmware
		#     upgrade renumbers phy0->phy1). ubus subscribe exits almost
		#     instantly every time. Retrying here is expected steady-state
		#     behavior while we wait for it to show up - it must stay quiet
		#     and NOT force a full sync every few seconds, or a permanently
		#     wrong interface name turns into a full-sync spam loop.
		#
		#  2. We WERE subscribed and connected for a while, then lost it -
		#     e.g. hostapd tearing down and recreating the interface after a
		#     network reconfig, ACS channel reselection, or a radio reset.
		#     This is the case that can silently miss real assoc/disassoc
		#     events, so it deserves a loud log line and an immediate full
		#     sync to close the gap rather than waiting for the fallback
		#     interval.
		local end_ts elapsed
		end_ts=$(date +%s 2>/dev/null || echo 0)
		elapsed=$((end_ts - start_ts))
		if [ "$elapsed" -ge 10 ]; then
			consecutive_quick_fails=0
			log "ubus subscribe $interface lost connection after ${elapsed}s; reconnecting and forcing a full sync to close any gap" 0
			# force=1 bypasses the redundant-sync debounce, which would
			# otherwise silently swallow exactly the sync we need if this
			# reconnect happens soon after any other sync.
			do_full_sync 1
			sleep 5
		else
			consecutive_quick_fails=$((consecutive_quick_fails + 1))
			# ~20 quick fails at a 15s backoff is ~5 minutes of never once
			# managing to subscribe - long enough to rule out "still booting"
			# or "hasn't finished a DFS CAC scan yet" and treat this as a
			# genuinely gone interface (auto-detect only: an explicit
			# interfaces list is the user's deliberate choice, so we keep
			# retrying it forever exactly as before - only main()'s periodic
			# re-detect logic acts on this marker, and it only prunes names
			# that were auto-detected in the first place).
			if [ "$AUTO_DETECT_INTERFACES" = "1" ] && [ "$consecutive_quick_fails" -ge 20 ]; then
				log "ubus subscribe $interface has never connected after $consecutive_quick_fails attempts; giving up on this interface"
				mark_interface_gone "$interface"
				return 0
			fi
			log "ubus subscribe $interface exited immediately (interface not present?); retrying" 1
			sleep 15
		fi
	done
}

# Watch Home Assistant status topic to re-sync when HA comes back online.
watch_ha_status() {
	local auth=""
	[ -n "$MQTT_USERNAME" ] && auth="-u $MQTT_USERNAME -P $MQTT_PASSWORD"
	while [ ! -f "$RUNDIR/.stop" ]; do
		# shellcheck disable=SC2086
		mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" $auth \
			-t "homeassistant/status" 2>/dev/null | while read -r payload; do
			[ -f "$RUNDIR/.stop" ] && break
			case "$payload" in
			offline)
				log "Home Assistant is offline!"
				reset_registrations
				;;
			online)
				log "Home Assistant is back online"
				do_full_sync
				;;
			esac
		done
		[ -f "$RUNDIR/.stop" ] && break
		sleep 5
	done
}

# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------

cleanup() {
	log "Stopping..."
	touch "$RUNDIR/.stop" 2>/dev/null
	trap '' TERM INT
	# Kill the direct background jobs (the watcher subshells)...
	kill $CHILD_PIDS 2>/dev/null
	# ...and the streaming grandchildren (ubus subscribe / mosquitto_sub)
	# spawned inside the watcher pipes, which may have reparented to init.
	# busybox has `pgrep` but NOT `pkill`, so match PIDs and kill them.
	# (The init.d stop_service performs the same reap independently, so
	# teardown does not rely on this trap firing.)
	local gp
	for interface in $INTERFACES; do
		gp=$(pgrep -f "ubus subscribe $interface" 2>/dev/null)
		[ -n "$gp" ] && kill $gp 2>/dev/null
	done
	gp=$(pgrep -f "mosquitto_sub.*homeassistant/status" 2>/dev/null)
	[ -n "$gp" ] && kill $gp 2>/dev/null
	# Any in-flight away-debounce sleeps (see watch_interface). Each already
	# re-checks $RUNDIR/.stop before acting, so this is belt-and-suspenders
	# against a message being published after we've stopped. Killed by PID
	# (not name match - see the comment where these PIDs are recorded);
	# killing an already-exited PID is a silent no-op.
	if [ -f "$RUNDIR/.debounce_pids" ]; then
		kill $(cat "$RUNDIR/.debounce_pids" 2>/dev/null) 2>/dev/null
	fi
	sleep 1
	kill -9 $CHILD_PIDS 2>/dev/null
	for interface in $INTERFACES; do
		gp=$(pgrep -f "ubus subscribe $interface" 2>/dev/null)
		[ -n "$gp" ] && kill -9 $gp 2>/dev/null
	done
	gp=$(pgrep -f "mosquitto_sub.*homeassistant/status" 2>/dev/null)
	[ -n "$gp" ] && kill -9 $gp 2>/dev/null
	if [ -f "$RUNDIR/.debounce_pids" ]; then
		kill -9 $(cat "$RUNDIR/.debounce_pids" 2>/dev/null) 2>/dev/null
		rm -f "$RUNDIR/.debounce_pids" 2>/dev/null
	fi
	exit 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
	# Parse -c/--config argument
	while [ $# -gt 0 ]; do
		case "$1" in
		-c|--config)
			CONFIG="$2"
			shift 2
			;;
		*)
			shift
			;;
		esac
	done

	load_settings
	# Clear any stale stop-flag left behind by a previous instance's cleanup.
	# Without this, a single procd restart becomes an infinite crash loop:
	# the new instance's watcher loops and main loop all test for .stop and
	# would exit immediately if the previous cleanup's flag were still present.
	mkdir -p "$RUNDIR"
	rm -f "$RUNDIR/.stop" 2>/dev/null
	# Same idea for the away-debounce PID list and any pending markers: a
	# hard kill (kill -9 on the main process itself) bypasses cleanup(), so
	# don't inherit a previous instance's now-meaningless PIDs/markers.
	rm -f "$RUNDIR/.debounce_pids" "$RUNDIR"/*.pending_away 2>/dev/null
	rm -f "$RUNDIR"/iface_gone.* 2>/dev/null
	reset_registrations
	trap cleanup TERM INT

	log "Starting presence-detector on interfaces: $INTERFACES"

	# Initial full sync
	do_full_sync

	CHILD_PIDS=""

	# Start HA status watcher
	watch_ha_status &
	CHILD_PIDS="$CHILD_PIDS $!"

	# Start a watcher per interface
	local interface
	for interface in $INTERFACES; do
		watch_interface "$interface" &
		CHILD_PIDS="$CHILD_PIDS $!"
	done

	# Main loop. With a fallback interval, periodically re-sync. Without one,
	# just block so the main process stays in the foreground for procd.
	#
	# NOTE: we deliberately do NOT use "sleep N & wait $!" here. When the
	# background watcher children exit/respawn they deliver SIGCHLD, which
	# interrupts `wait` and would spin this loop (and destabilise the procd
	# instance). A plain foreground `sleep` is not affected by SIGCHLD.
	#
	# We poll in short (2s) increments rather than one long sleep so that a
	# stop is noticed almost immediately: init.d stop_service writes the
	# .stop flag, and we exit on the next tick and run cleanup — without
	# depending on a SIGTERM trap interrupting a long `sleep` (unreliable on
	# busybox ash), and well within procd's term_timeout.
	local elapsed=0
	local iface_recheck_elapsed=0
	while [ ! -f "$RUNDIR/.stop" ]; do
		sleep 2
		if [ "$FALLBACK_SYNC_INTERVAL" -gt 0 ] 2>/dev/null; then
			elapsed=$((elapsed + 2))
			if [ "$elapsed" -ge "$FALLBACK_SYNC_INTERVAL" ]; then
				elapsed=0
				[ -f "$RUNDIR/.stop" ] && break
				do_full_sync
			fi
		fi

		# Interface auto-detection only runs once at startup (load_settings),
		# so a radio that appears afterward - still doing a DFS CAC scan,
		# slow to come up on boot, recreated by a wifi reload, or renamed by
		# a channel switch/firmware upgrade - would otherwise never get a
		# watcher for the life of this process (see upstream issue #90).
		# Only applies when auto-detecting; an explicit `interfaces` list is
		# a deliberate choice we don't second-guess.
		if [ "$AUTO_DETECT_INTERFACES" = "1" ]; then
			iface_recheck_elapsed=$((iface_recheck_elapsed + 2))
			if [ "$iface_recheck_elapsed" -ge 30 ]; then
				iface_recheck_elapsed=0
				[ -f "$RUNDIR/.stop" ] && break

				# Prune interfaces watch_interface() has given up on (see
				# mark_interface_gone - after enough consecutive instant-
				# exit ubus subscribe attempts to be confident it's not
				# just still starting up). Its own watcher loop has already
				# exited by the time this marker exists, so nothing to kill
				# here - just stop tracking the name and clear the marker,
				# so if it ever comes back under the same name it's treated
				# as freshly new rather than permanently ignored.
				local remaining="" existing
				for existing in $INTERFACES; do
					if is_interface_gone "$existing"; then
						log "Interface $existing confirmed gone; no longer tracking it"
						clear_interface_gone "$existing"
					else
						remaining="$remaining $existing"
					fi
				done
				INTERFACES="$remaining"

				local current_ifaces new_iface already_watched
				current_ifaces=$(ubus list 'hostapd.*' 2>/dev/null)
				for new_iface in $current_ifaces; do
					already_watched=0
					for existing in $INTERFACES; do
						[ "$existing" = "$new_iface" ] && already_watched=1 && break
					done
					if [ "$already_watched" = "0" ]; then
						log "New wifi interface detected: $new_iface; starting a watcher for it"
						INTERFACES="$INTERFACES $new_iface"
						watch_interface "$new_iface" &
						CHILD_PIDS="$CHILD_PIDS $!"
						# A newly-appeared interface may already have
						# clients associated from before we started
						# watching it (e.g. it came up moments ago and a
						# phone already joined) - do_full_sync will pick
						# them up on its own next run via
						# get_all_online_devices, so nothing else to do
						# here.
					fi
				done
			fi
		fi
	done

	cleanup
}

main "$@"
