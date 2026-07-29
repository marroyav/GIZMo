# Runtime architecture

## Ownership boundary

`gizmo-runtime` is the only package-level owner. `gizmo.target` is the only
operator-level lifecycle control. Internally, systemd supervises processes
individually so a failed OPC UA server does not kill measurements, and changing
a threshold restarts only ZMon instead of rerunning the entire boot script.

```text
gizmo.target
├── gizmo-network.service
├── gizmo-hardware.service
│   ├── gizmo-zmon.service ───── TCP 5055
│   ├── gizmo-display.service
│   ├── gizmo-temperature.service ─ TCP 5005
│   └── gizmo-sdr.service ────── TCP 5556
├── gizmo-control.socket ─────── /run/gizmo/control.sock
├── gizmo-zmq.service ────────── TCP 5555
├── gizmo-opcua.service ──────── OPC UA TCP 4840
├── gizmo-historian.service ──── SQLite + /run/gizmo/historian.sock
└── gizmo-dashboard.service ──── read-only HTTP TCP 8080
```

The OPC UA server consumes ZeroMQ, temperature, and SDR and directly observes
Linux time, OS resources, networking, storage, FPGA/board identity,
calibration, and systemd state. Target members and producer dependencies use
ordered `Wants`, not `Requires`, so a component restart does not deactivate the
package target. `PartOf=gizmo.target` still gives the operator one complete
start/stop boundary. The public server remains browsable and reports non-good
OPC UA status codes when a producer fails. The display and compatibility
ZeroMQ service consume ZMon's TCP feed.

The dashboard resolves and subscribes to the canonical OPC UA namespace once,
caches scalar monitoring values, and fans that cache out to browsers with
server-sent events. Browser count therefore does not multiply OPC UA sessions
or producer reads. Its one-hour live buffer remains browser-local.

The historian has its own fixed OPC UA subscription and records compressed
one-second and ten-second scalar snapshots, one-minute rollups, and state
transitions in SQLite. The dashboard reaches it through a private read-only
Unix socket only when History mode or historical CSV is requested. A historian
failure therefore cannot break the live path. See the
[historian design](historian.md).

The typed `urn:fnal:gizmo` OPC UA namespace is the sole supported public
machine contract. The recovered text and `SimpleOPCUAServer` interfaces are
migration adapters.

## Privilege separation

ZMon and SDR still require `/dev/mem`; the recovered display writes legacy
GPIO sysfs nodes. Those processes retain narrowly bounded root execution until
their device access can be converted to UIO, VFIO, GPIO character devices, or
dedicated udev permissions.

ZeroMQ, temperature, OPC UA, the historian, and the dashboard run as the locked
`gizmo` system user.
The temperature I2C node is group-owned by `gizmo`.

The legacy ZeroMQ server invoked arbitrary `sudo` commands and reran
the complete startup script. The maintained server instead calls a
group-restricted Unix socket. The C control helper accepts only:

- `restart-zmon`
- `set-time <Unix-seconds>` in the years 2000–2100
- `ping`

No shell evaluates client data. A successful time update sets both the Linux
wall clock and `/dev/rtc0`; the response explicitly reports the partial case
where the wall clock changed but the hardware RTC did not.

## Filesystem ownership

| Path | Purpose | Mutation policy |
|---|---|---|
| `/usr/bin`, `/usr/libexec/gizmo` | executables and service code | package only |
| `/usr/lib/gizmo/python` | pinned Python runtime dependencies | package only |
| `/usr/share/gizmo/dashboard` | self-contained browser assets | package only |
| `/lib/firmware/xilinx/GIZMo_Kria_3_7_25` | compiled overlay | package only |
| `/etc/gizmo` | administrator configuration | dpkg conffiles |
| `/usr/share/gizmo/default-state` | factory/recovered defaults | package only |
| `/var/lib/gizmo` | calibration, latch, ADC, runtime arguments | services/operator |
| `/var/lib/gizmo/history` | retained SQLite telemetry | historian only |
| `/run/gizmo` | control and historian sockets | transient |

`systemd-tmpfiles` copies defaults only when state is absent. Package upgrades
therefore do not overwrite device calibration or operator values.

## Ordering and failure behavior

1. Starting `gizmo.target` orders and starts network setup, overlay loading,
   ZMon, the control socket, canonical OPC UA, historian, and dashboard.
2. Hardware consumers require successful overlay setup.
3. ZeroMQ requires ZMon and the privileged control socket.
4. OPC UA starts after its producers but remains available when an optional
   producer fails.
5. Long-running services use bounded restart delays; OPC UA additionally uses
   systemd readiness and watchdog notifications.
6. The historian starts after OPC UA, retains bounded data independently, and
   exposes only its private read-only query socket.
7. The dashboard starts after OPC UA and the historian, remains read-only, and
   keeps its live view available if history fails.
8. Stopping `gizmo.target` stops every component; hardware teardown unloads the
   overlay last through dependency ordering.

An intentional ZMon restart does not stop `gizmo.target`, ZeroMQ, OPC UA, the
display, or the sensor services. Consumers retain their sessions and expose a
brief non-good source status until ZMon returns.

All stdout/stderr goes to the journal. The package does not create unbounded
multi-megabyte logs in `/dev/shm`.

## Compatibility

The external legacy ports, OPC UA namespace/object/variable names, and
documented text ZeroMQ commands are retained. Persistent command values use
the recovered environment-file format so rollback remains possible. New
clients resolve `urn:fnal:gizmo` by URI and use its stable string NodeIds.
Breaking changes require a new major-version namespace URI rather than
reinterpretation of an existing node.
