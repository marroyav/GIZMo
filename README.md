# GIZMo Kria runtime

This source-only public edition turns the maintained GIZMo Kria slow-controls
runtime into one package, `gizmo-runtime`, with one operational entry point:
`gizmo.target`. Device images, recovered filesystem content, calibration/state
bundles, site configuration, supplied manuals, and reviewed third-party
dependencies are deliberately excluded.

The package deliberately uses separate systemd services for the overlay,
network, impedance monitor, display, ZeroMQ, temperature, SDR, OPC UA,
persistent historian, and web-dashboard components. Systemd owns startup
ordering, privileges, restarts, logs, and shutdown. Operators still start and
stop the product as one unit.

> Status: runtime 0.4.4 is the maintained package. It keeps the Kria historian
> as a 14-day edge buffer, adds cursor-based recovery to independent off-board
> replicas, and package-owns boot-time synchronization through site-managed
> NTP sources. Runtime 0.4.3 added the ZMon measurement engine's authoritative
> composite relay/beacon alarm to OPC UA, the historian, and the full-width
> operations dashboard. Runtime 0.4.0 was historian-restart tested, and
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

Only the two Processing System Ethernet ports are supported. The public
configuration defaults to `GIZMO_NETWORK_MODE=none`; it contains no deployment
addresses. Copy `config/network.env.example` into the controlled site
configuration, assign approved addresses, and select `static` only after
review. The setup script refuses RFC 5737 example addresses.

## Build and test

Host-side compilation and protocol/unit-file checks do not require controlled
device assets:

```sh
make
make test
```

A full device build requires a controlled asset directory containing the
reviewed EVE dependency, FPGA/device-tree overlays, device state, calibration
tables, and approved network/time configuration:

```text
controlled-assets/
├── eve/
├── default-state/
├── firmware/
│   ├── GIZMo-Kria-3-7-25.dtbo
│   └── xilinx/GIZMo_Kria_3_7_25/
└── site-config/
    ├── network.env
    └── 60-gizmo-timesyncd.conf
```

Build it only from the controlled workflow:

```sh
make full CONTROLLED_ASSET_ROOT=/approved/path/to/controlled-assets
```

The package builder installs the pinned Python runtime and its compiled wheels
inside `/usr/lib/gizmo/python`; it does not use Ubuntu's personal
`~/.local` packages. A wheelhouse can be supplied for offline builds:

```sh
CONTROLLED_ASSET_ROOT=/approved/path/to/controlled-assets \
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

The generic off-board replica design is described in the
[off-board monitoring guide](docs/offboard-monitoring.md). The reusable
user-mode files are in `deploy/offboard/`; they contain only loopback endpoints
and example paths. Hostnames, accounts, and storage locations remain site
configuration.

The board can also serve a read-only console at the site-assigned endpoint on
TCP 8080. It uses one shared live OPC UA subscription,
preserves value status codes, plots up to one hour in the browser, and queries
package-owned persistent history for longer intervals and CSV export. See
[the dashboard guide](docs/dashboard.md) and
[historian design](docs/historian.md).

The Kria clock is synchronized at boot with `systemd-timesyncd`; see the
[time-synchronization guide](docs/time-synchronization.md).

Local historian inspection is intentionally restricted to privileged
operators:

```sh
sudo gizmo-historian-client status
sudo gizmo-historian-client series
```

On a non-Python-3.10 development host, use `make test`. A target package still
requires the controlled assets and a native Ubuntu 22.04 ARM64/Python 3.10
build environment.

## Install

The public repository by itself cannot produce an installable device package.
That is intentional: packaging fails closed unless the controlled inputs above
are present.

Read [the migration procedure](docs/migration.md) before touching a running
legacy image. The safe high-level sequence is:

```sh
sudo dpkg -i build/gizmo-runtime_0.4.4_arm64.deb
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
- `config/`: publication-safe examples and non-site runtime policy
- `packaging/`: systemd, udev, sysusers, tmpfiles, and `.deb` construction
- `deploy/`: parameterized off-board monitoring and time-relay examples
- `docs/`: source-level architecture, operation, and security guidance

The public history does not contain the recovered live-root snapshot, compiled
overlay, device-tree blob, device-state bundle, calibration tables, supplied
manuals, live database, credentials, or site topology. See the
[recovered-system inventory](docs/live-system-inventory.md),
[security notes](docs/security.md), and
[licensing/provenance notes](LICENSES/README.md).
