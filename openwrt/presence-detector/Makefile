# SPDX-License-Identifier: MIT
#
# OpenWrt package definition for openwrt-ha-device-tracker.
#
# Builds two packages:
#   presence-detector     - the Python implementation (feature-complete)
#   presence-detector-sh  - the pure-shell/busybox implementation (low-flash)
#
# Both are architecture-independent (PKGARCH:=all) since they ship only
# scripts - one build works on every target, ipk or apk.
#
# Source is bundled: the scripts are copied straight from this repository's
# checkout (two directories up from this Makefile) rather than downloaded,
# so there is no source tarball and no PKG_HASH to keep in sync - the build
# always packages exactly the tree it was run from. PKG_VERSION is stamped
# from the release tag by the CI workflow.

include $(TOPDIR)/rules.mk

PKG_NAME:=presence-detector
PKG_VERSION:=3.2.0
PKG_RELEASE:=1

PKG_MAINTAINER:=Ronald Moesbergen <rmoesbergen@users.noreply.github.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

# Repository root (this Makefile lives in <repo>/openwrt/presence-detector/).
PKG_SRC_DIR:=$(CURDIR)/../..

include $(INCLUDE_DIR)/package.mk

define Package/presence-detector/Default
  SECTION:=net
  CATEGORY:=Network
  SUBMENU:=Home Assistant
  TITLE:=WiFi device presence detector for Home Assistant
  URL:=https://github.com/rmoesbergen/openwrt-ha-device-tracker
  PKGARCH:=all
  # Both packages install /etc/init.d/presence-detector and the same
  # settings file, so only one may be installed. apk enforces that via this
  # shared (automatically versioned) provide - the apk-era SDK does not
  # carry CONFLICTS into apk metadata. opkg uses CONFLICTS below instead.
  PROVIDES:=presence-detector-service
endef

define Package/presence-detector
$(call Package/presence-detector/Default)
  TITLE+= (Python)
  # Runtime deps only (EXTRA_DEPENDS): they come from the official OpenWrt
  # feeds, so they're written into the package metadata but NOT built here.
  # (DEPENDS:=+... would make the SDK compile python3 & co. from source.)
  # Set matches the project's documented install for the Python version.
  # Comma-separated, each with a version constraint ("name (>=x)", space
  # before the paren) - the apk-era SDK (25.12+) rejects unversioned extra
  # deps. Loose minimums that hold on every supported release; paho >=1
  # because the code supports both the v1 and v2 paho callback APIs.
  EXTRA_DEPENDS:=python3-light (>=3), python3-paho-mqtt (>=1), python3-codecs (>=3), python3-urllib (>=3), python3-logging (>=3)
endef

define Package/presence-detector-sh
$(call Package/presence-detector/Default)
  TITLE+= (shell)
  # Pure shell: only needs the MQTT client; jsonfilter and ubus are in base.
  # Runtime dep only - see the note on the Python package above.
  EXTRA_DEPENDS:=mosquitto-client-nossl (>=1)
  # opkg conflict (ipk Conflicts: field). Declared on one side only:
  # declaring it on both creates a circular Kconfig dependency. apk relies
  # on the shared PROVIDES above.
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

define Build/Prepare
	mkdir -p $(PKG_BUILD_DIR)/init.d
	$(CP) \
		$(PKG_SRC_DIR)/presence-detector.py \
		$(PKG_SRC_DIR)/presence-detector.sh \
		$(PKG_SRC_DIR)/presence-detector.settings.json.example \
		$(PKG_SRC_DIR)/LICENSE \
		$(PKG_BUILD_DIR)/
	$(CP) \
		$(PKG_SRC_DIR)/init.d/presence-detector \
		$(PKG_SRC_DIR)/init.d/presence-detector-sh \
		$(PKG_BUILD_DIR)/init.d/
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
