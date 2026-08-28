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
	SOURCE_TYPE=$(get_setting '@.source_type' 'router')
	DEBUG=$(get_setting '@.debug' 'false')
	[ "$DEBUG" = "true" ] && DEBUG=1 || DEBUG=0

	# Filter list, lowercased
	FILTER=$(get_array '@.filter' | tr 'A-Z' 'a-z')

	# Interfaces: auto-detect if not specified
	INTERFACES=$(get_array '@.interfaces')
	if [ -z "$INTERFACES" ]; then
		INTERFACES=$(ubus list 'hostapd.*' 2>/dev/null)
	fi

	if [ -z "$INTERFACES" ]; then
		log "No wifi interfaces found or configured!"
		exit 1
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

# ---------------------------------------------------------------------------
# JSON escaping for params passthrough
# ---------------------------------------------------------------------------

# Publish the discovery config + state for a device.
# ha_seen <device-mac> <seen: 1|0>
ha_seen() {
	local device="$1"
	local seen="$2"

	# device_slug: mac with ':' -> '_', optionally prefixed by ap_name
	local device_name
	device_name=$(echo "$device" | tr ':' '_')
	local device_slug="$device_name"
	if [ -n "$AP_NAME" ]; then
		device_slug="${AP_NAME}_${device_name}"
	fi

	local ok=0

	if ! is_registered "$device_slug"; then
		mark_registered "$device_slug"

		# Look up per-device overrides for name / icon from params.
		local override_name
		override_name=$(jsonfilter -i "$CONFIG" -e "@.params['$device'].name" 2>/dev/null)
		local override_icon
		override_icon=$(jsonfilter -i "$CONFIG" -e "@.params['$device'].icon" 2>/dev/null)

		local name="$device_name"
		[ -n "$override_name" ] && name="$override_name"

		local device_block="\"connections\":[[\"mac\",\"$device\"]]"
		if [ -n "$override_name" ]; then
			device_block="$device_block,\"name\":\"$override_name\""
		fi

		local icon_field=""
		[ -n "$override_icon" ] && icon_field="\"icon\":\"$override_icon\","

		local config_topic="homeassistant/device_tracker/${device_slug}/config"
		local state_topic="homeassistant/device_tracker/${device_slug}/state"
		local body
		body="{\"state_topic\":\"$state_topic\",\"json_attributes_topic\":\"$state_topic\",\"value_template\":\"{{ value_json['state'] }}\",\"name\":\"$name\",${icon_field}\"platform\":\"device_tracker\",\"payload_home\":\"$LOCATION\",\"payload_not_home\":\"$AWAY\",\"source_type\":\"$SOURCE_TYPE\",\"device\":{$device_block},\"unique_id\":\"$device_slug\"}"

		mqtt_pub "$config_topic" "$body"
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
	# Debounce: the retained homeassistant/status "online" message is
	# delivered immediately after startup (right after main's own sync) and on
	# every mosquitto_sub reconnect. Without this guard those would each
	# trigger a redundant full re-sync. Skip if we synced < 30s ago.
	local now
	now=$(date +%s 2>/dev/null || echo 0)
	if [ -f "$RUNDIR/.last_sync" ]; then
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
	fi

	cp "$seen_file" "$LAST_SEEN"
}

# ---------------------------------------------------------------------------
# Event watchers
# ---------------------------------------------------------------------------

# Watch a single interface's ubus events and act on assoc/disassoc.
watch_interface() {
	local interface="$1"
	while [ ! -f "$RUNDIR/.stop" ]; do
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
					log "Device $mac on $interface is now away" 1
					ha_seen "$mac" 0
				fi
				;;
			esac
		done
		# If ubus subscribe exits (interface gone), wait and retry.
		[ -f "$RUNDIR/.stop" ] && break
		sleep 5
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
	sleep 1
	kill -9 $CHILD_PIDS 2>/dev/null
	for interface in $INTERFACES; do
		gp=$(pgrep -f "ubus subscribe $interface" 2>/dev/null)
		[ -n "$gp" ] && kill -9 $gp 2>/dev/null
	done
	gp=$(pgrep -f "mosquitto_sub.*homeassistant/status" 2>/dev/null)
	[ -n "$gp" ] && kill -9 $gp 2>/dev/null
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
	done

	cleanup
}

main "$@"
