# SPDX-License-Identifier: GPL-3.0-or-later
#
# OpenWrt package definition for openwrt-ha-device-tracker.
#
# Builds two packages from one source checkout:
#   presence-detector     - the Python implementation (feature-complete)
#   presence-detector-sh   - the pure-shell/busybox implementation (low-flash)
#
# Both are architecture-independent (PKGARCH:=all) since they ship only
# scripts - one build works on every target, ipk or apk. The two packages
# install to the same init.d / config paths (you run one implementation or
# the other), so they CONFLICT and cannot be co-installed.

include $(TOPDIR)/rules.mk

PKG_NAME:=presence-detector
PKG_VERSION:=3.1.1
PKG_RELEASE:=1

PKG_SOURCE_PROTO:=git
PKG_SOURCE_URL:=https://github.com/rmoesbergen/openwrt-ha-device-tracker.git
PKG_SOURCE_VERSION:=v$(PKG_VERSION)
PKG_MIRROR_HASH:=skip

PKG_MAINTAINER:=Ronald Moesbergen <info@isnoop.net>
PKG_LICENSE:=GPL-3.0-or-later
PKG_LICENSE_FILES:=LICENSE

include $(INCLUDE_DIR)/package.mk

define Package/presence-detector/Default
  SECTION:=net
  CATEGORY:=Network
  SUBMENU:=Home Assistant
  TITLE:=WiFi device presence detector for Home Assistant
  URL:=https://github.com/rmoesbergen/openwrt-ha-device-tracker
  PKGARCH:=all
endef

define Package/presence-detector
$(call Package/presence-detector/Default)
  TITLE+= (Python)
  # Dependency set matches the project's documented install for the Python
  # version (README.md).
  DEPENDS:=+python3-light +python3-paho-mqtt +python3-codecs +python3-urllib +python3-logging
  CONFLICTS:=presence-detector-sh
endef

define Package/presence-detector-sh
$(call Package/presence-detector/Default)
  TITLE+= (shell)
  # Pure shell: only needs the MQTT client; jsonfilter and ubus are in base.
  DEPENDS:=+mosquitto-client-nossl
  CONFLICTS:=presence-detector
endef

define Package/presence-detector/description
 Reports WiFi client presence to Home Assistant over MQTT, using hostapd
 association events via ubus. This is the Python implementation.
endef

define Package/presence-detector-sh/description
 Reports WiFi client presence to Home Assistant over MQTT, using hostapd
 association events via ubus. This is the pure-shell (busybox/ash)
 implementation for low-flash devices - no Python required.
endef

# Scripts only: nothing to compile.
define Build/Compile
endef

define Package/presence-detector/conffiles
/etc/config/presence-detector.settings.json
endef

Package/presence-detector-sh/conffiles = $(Package/presence-detector/conffiles)

define Package/presence-detector/install
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/presence-detector.py $(1)/usr/bin/presence-detector.py
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/init.d/presence-detector $(1)/etc/init.d/presence-detector
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) $(PKG_BUILD_DIR)/presence-detector.settings.json.example \
		$(1)/etc/config/presence-detector.settings.json
endef

define Package/presence-detector-sh/install
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/presence-detector.sh $(1)/usr/bin/presence-detector.sh
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/init.d/presence-detector-sh $(1)/etc/init.d/presence-detector
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) $(PKG_BUILD_DIR)/presence-detector.settings.json.example \
		$(1)/etc/config/presence-detector.settings.json
endef

$(eval $(call BuildPackage,presence-detector))
$(eval $(call BuildPackage,presence-detector-sh))
