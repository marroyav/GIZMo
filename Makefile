CC ?= gcc
PYTHON ?= python3
BUILD_DIR ?= build
DESTDIR ?=
PREFIX ?= /usr

CPPFLAGS ?=
CFLAGS ?= -O2 -g
CFLAGS += -std=gnu11 -Wall -Wextra -Wpedantic
LDFLAGS ?=

EVE_ROOT := legacy/live-root/home/ubuntu/Software/EVE-main_Kria
EVE_CPPFLAGS := -DPLATFORM_RASPBERRYPI -DUSE_LINUX_SPI_DEV \
	-I$(EVE_ROOT)/lib/eve/include -I$(EVE_ROOT)/example
EVE_SOURCES := \
	$(EVE_ROOT)/main/main.c \
	$(EVE_ROOT)/example/eve_calibrate.c \
	src/display/eve_example.c \
	$(EVE_ROOT)/example/eve_fonts.c \
	$(EVE_ROOT)/example/eve_helper.c \
	$(EVE_ROOT)/example/eve_images.c \
	$(EVE_ROOT)/lib/eve/source/EVE_API.c \
	$(EVE_ROOT)/lib/eve/source/EVE_HAL_Linux.c \
	$(EVE_ROOT)/lib/eve/ports/eve_arch_rpi/EVE_Linux_RPi.c

BINARIES := $(BUILD_DIR)/gizmo-zmon $(BUILD_DIR)/gizmo-control $(BUILD_DIR)/gizmo-display

.PHONY: all clean install test deb

all: $(BINARIES)

$(BUILD_DIR):
	mkdir -p $@

$(BUILD_DIR)/gizmo-zmon: src/zmon/gizmo-zmon.c | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) -lm

$(BUILD_DIR)/gizmo-control: src/control/gizmo-control.c | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS)

$(BUILD_DIR)/gizmo-display: $(EVE_SOURCES) | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(EVE_CPPFLAGS) $(CFLAGS) $(EVE_SOURCES) -o $@ $(LDFLAGS)

install: all
	install -D -m 0755 $(BUILD_DIR)/gizmo-zmon $(DESTDIR)$(PREFIX)/bin/gizmo-zmon
	install -D -m 0755 $(BUILD_DIR)/gizmo-display $(DESTDIR)$(PREFIX)/bin/gizmo-display
	install -D -m 0755 $(BUILD_DIR)/gizmo-control $(DESTDIR)$(PREFIX)/libexec/gizmo/gizmo-control
	install -D -m 0755 scripts/gizmo-hardware-setup $(DESTDIR)$(PREFIX)/libexec/gizmo/gizmo-hardware-setup
	install -D -m 0755 scripts/gizmo-network-setup $(DESTDIR)$(PREFIX)/libexec/gizmo/gizmo-network-setup
	install -D -m 0755 scripts/gizmo-doctor $(DESTDIR)$(PREFIX)/bin/gizmo-doctor
	install -D -m 0755 scripts/gizmo-opcua-client $(DESTDIR)$(PREFIX)/bin/gizmo-opcua-client
	install -d -m 0755 $(DESTDIR)$(PREFIX)/libexec/gizmo
	install -m 0644 src/python/*.py $(DESTDIR)$(PREFIX)/libexec/gizmo/
	install -D -m 0644 VERSION $(DESTDIR)$(PREFIX)/share/gizmo/VERSION
	install -d -m 0755 $(DESTDIR)$(PREFIX)/share/gizmo/dashboard
	install -m 0644 web/dashboard/* $(DESTDIR)$(PREFIX)/share/gizmo/dashboard/
	install -d -m 0755 $(DESTDIR)/etc/gizmo
	install -m 0644 config/hardware.env config/network.env config/runtime.env $(DESTDIR)/etc/gizmo/
	install -d -m 0755 $(DESTDIR)$(PREFIX)/share/gizmo/default-state
	install -m 0644 config/default-state/* $(DESTDIR)$(PREFIX)/share/gizmo/default-state/
	install -d -m 0755 $(DESTDIR)/lib/systemd/system
	install -m 0644 packaging/systemd/* $(DESTDIR)/lib/systemd/system/
	install -D -m 0644 packaging/sysusers/gizmo.conf $(DESTDIR)$(PREFIX)/lib/sysusers.d/gizmo.conf
	install -D -m 0644 packaging/tmpfiles/gizmo.conf $(DESTDIR)$(PREFIX)/lib/tmpfiles.d/gizmo.conf
	install -D -m 0644 packaging/udev/99-gizmo.rules $(DESTDIR)$(PREFIX)/lib/udev/rules.d/99-gizmo.rules
	install -d -m 0755 $(DESTDIR)/lib/firmware/xilinx/GIZMo_Kria_3_7_25
	install -m 0644 legacy/live-root/lib/firmware/xilinx/GIZMo_Kria_3_7_25/* $(DESTDIR)/lib/firmware/xilinx/GIZMo_Kria_3_7_25/
	install -D -m 0644 legacy/live-root/lib/firmware/GIZMo-Kria-3-7-25.dtbo $(DESTDIR)/lib/firmware/GIZMo-Kria-3-7-25.dtbo
	install -d -m 0755 $(DESTDIR)$(PREFIX)/share/doc/gizmo-runtime
	install -m 0644 README.md $(DESTDIR)$(PREFIX)/share/doc/gizmo-runtime/README.md
	install -m 0644 docs/*.md $(DESTDIR)$(PREFIX)/share/doc/gizmo-runtime/
	install -d -m 0755 $(DESTDIR)$(PREFIX)/share/doc/gizmo-runtime/reference
	install -m 0644 docs/reference/* $(DESTDIR)$(PREFIX)/share/doc/gizmo-runtime/reference/
	install -d -m 0755 $(DESTDIR)$(PREFIX)/share/doc/gizmo-runtime/licenses
	install -m 0644 LICENSES/README.md $(DESTDIR)$(PREFIX)/share/doc/gizmo-runtime/licenses/README.md
	install -m 0644 $(EVE_ROOT)/lib/eve/LICENSE $(DESTDIR)$(PREFIX)/share/doc/gizmo-runtime/licenses/BRIDGETEK-EVE-LICENSE
	install -m 0644 $(EVE_ROOT)/example/LICENSE $(DESTDIR)$(PREFIX)/share/doc/gizmo-runtime/licenses/BRIDGETEK-EXAMPLE-LICENSE

test: all
	PYTHON="$(PYTHON)" ./tests/run-tests.sh

deb:
	./packaging/build-deb.sh

clean:
	rm -rf -- $(BUILD_DIR)
