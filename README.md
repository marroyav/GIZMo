# GIZMo Kria runtime

This repository turns the recovered GIZMo Kria slow-controls release into one
owned package, `gizmo-runtime`, with one operational entry point:
`gizmo.target`.

The package deliberately uses separate systemd services for the overlay,
network, impedance monitor, display, ZeroMQ, temperature, SDR, OPC UA,
persistent historian, and web-dashboard components. Systemd owns startup
ordering, privileges, restarts, logs, and shutdown. Operators still start and
stop the product as one unit.

> Status: runtime 0.4.3 is the maintained package. It adds the ZMon
> measurement engine's authoritative composite relay/beacon alarm to OPC UA,
> the historian, and the full-width dark operations dashboard without
> reimplementing the resistance/phase decision in monitoring code. Runtime
> 0.4.0 was installed and historian-restart tested, and
> runtime 0.2.9 was cold-boot tested, on the borrowed instrument.
> Installation itself does not enable or start `gizmo.target`.

## Runtime map

| Component | Unit | Port | Runtime identity |
|---|---|---:|---|
| FPGA overlay | `gizmo-hardware.service` | — | root |
| PS-port addressing | `gizmo-network.service` | — | root, `CAP_NET_ADMIN` |
| privileged control | `gizmo-control.socket` | Unix socket | root, allow-listed commands |
| impedance monitor | `gizmo-zmon.service` | TCP 5055 | root, `CAP_SYS_RAWIO` |
| EVE display | `gizmo-display.service` | — | root |
| ZeroMQ command API | `gizmo-zmq.service` | TCP 5555 | `gizmo` |
| temperature stream | `gizmo-temperature.service` | TCP 5005 | `gizmo` |
| SDR stream | `gizmo-sdr.service` | TCP 5556 | root, `CAP_SYS_RAWIO` |
| canonical OPC UA server | `gizmo-opcua.service` | TCP 4840 | `gizmo` |
| persistent historian | `gizmo-historian.service` | private Unix socket | `gizmo` |
| live and historical dashboard | `gizmo-dashboard.service` | HTTP 8080 | `gizmo` |

Only the two Processing System Ethernet ports are configured. Defaults match
the recovered board:

- `eth0`: `<redacted-private-ip>/24`, default gateway `<redacted-private-ip>`
- `eth1`: `<redacted-private-ip>/24`

Edit `/etc/gizmo/network.env` to change them. Set
`GIZMO_NETWORK_MODE=networkmanager` or `none` when another component owns the
interfaces.

## Build and test

Host-side compilation and protocol/unit-file checks:

```sh
make
make test
```

Build the target package natively on Ubuntu 22.04 ARM64:

```sh
make deb
```

The package builder installs the pinned Python runtime and its compiled wheels
inside `/usr/lib/gizmo/python`; it does not use Ubuntu's personal
`~/.local` packages. A wheelhouse can be supplied for offline builds:

```sh
WHEELHOUSE=/path/to/wheels make deb
```

ARM64 wheel hashes are pinned in
`packaging/wheelhouse-arm64.sha256` and checked before an offline package
build.

The supported monitoring and control contract is the typed OPC UA namespace
`urn:fnal:gizmo` on TCP 4840. It exposes measurement quality and source
timestamps along with OS, network, firmware, time, service, calibration, and
SDR status:

```sh
gizmo-opcua-client health
gizmo-opcua-client measurement
gizmo-opcua-client snapshot
gizmo-opcua-client schema
```

See [the OPC UA contract](docs/opcua.md). The recovered
`SimpleOPCUAServer/CommandObject` namespace and text ZeroMQ API remain as
compatibility interfaces during migration.

The board also serves a read-only console at
`http://<gizmo-address>:8080/`. It uses one shared live OPC UA subscription,
preserves value status codes, plots up to one hour in the browser, and queries
package-owned persistent history for longer intervals and CSV export. See
[the dashboard guide](docs/dashboard.md) and
[historian design](docs/historian.md).

Local historian inspection is intentionally restricted to privileged
operators:

```sh
sudo gizmo-historian-client status
sudo gizmo-historian-client series
```

On a non-Python-3.10 development host, a structure-only package can be checked
with `BUNDLE_PYTHON_DEPS=0 make deb`. That artifact is not suitable for the
Kria.

## Install

Read [the migration procedure](docs/migration.md) before touching a running
legacy image. The safe high-level sequence is:

```sh
sudo dpkg -i build/gizmo-runtime_0.4.3_arm64.deb
sudo gizmo-doctor
```

Then stop and mask both legacy startup units before enabling the new target.
Do not run the legacy scripts and `gizmo.target` together; both access the same
FPGA MMIO, ports, files, and relays.

After migration:

```sh
sudo systemctl enable --now gizmo.target
systemctl status gizmo.target
journalctl -u 'gizmo-*' -f
```

Configuration lives in `/etc/gizmo`, device-specific mutable state and
calibration in `/var/lib/gizmo`, and transient sockets in `/run/gizmo`.
Calibration state is retained even when the package is purged.

## Repository layout

- `src/`: maintained ZMon, privilege helper, and service sources
- `config/`: package configuration and recovered device-state defaults
- `packaging/`: systemd, udev, sysusers, tmpfiles, and `.deb` construction
- `legacy/live-root/`: immutable audit snapshot from the working instrument
- `docs/reference/`: supplied manuals
- `tools/legacy/`: supplied legacy client

The compiled overlay is present, but the Vivado/HDL project is not. See
[the recovered-system inventory](docs/live-system-inventory.md) and
[security notes](docs/security.md). Review the
[licensing/provenance notes](LICENSES/README.md) before redistribution.
