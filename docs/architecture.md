# Runtime architecture

## Ownership boundary

`gizmo-runtime` is the only package-level owner. `gizmo.target` is the only
operator-level lifecycle control. Internally, systemd supervises processes
individually so a failed OPC-UA bridge does not kill measurements, and changing
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
└── gizmo-opcua.service ──────── TCP 4840
```

OPC-UA consumes ZeroMQ, temperature, and SDR. The display and ZeroMQ consume
ZMon's TCP feed.

## Privilege separation

ZMon and SDR still require `/dev/mem`; the recovered display writes legacy
GPIO sysfs nodes. Those processes retain narrowly bounded root execution until
their device access can be converted to UIO, VFIO, GPIO character devices, or
dedicated udev permissions.

ZeroMQ, temperature, and OPC-UA run as the locked `gizmo` system user.
The temperature I2C node is group-owned by `gizmo`.

The legacy ZeroMQ server invoked arbitrary `sudo` commands and reran
the complete startup script. The maintained server instead calls a
group-restricted Unix socket. The C control helper accepts only:

- `restart-zmon`
- `set-time <Unix-seconds>` in the years 2000–2100
- `ping`

No shell evaluates client data.

## Filesystem ownership

| Path | Purpose | Mutation policy |
|---|---|---|
| `/usr/bin`, `/usr/libexec/gizmo` | executables and service code | package only |
| `/usr/lib/gizmo/python` | pinned Python runtime dependencies | package only |
| `/lib/firmware/xilinx/GIZMo_Kria_3_7_25` | compiled overlay | package only |
| `/etc/gizmo` | administrator configuration | dpkg conffiles |
| `/usr/share/gizmo/default-state` | factory/recovered defaults | package only |
| `/var/lib/gizmo` | calibration, latch, ADC, runtime arguments | services/operator |
| `/run/gizmo` | privileged control socket | transient |

`systemd-tmpfiles` copies defaults only when state is absent. Package upgrades
therefore do not overwrite device calibration or operator values.

## Ordering and failure behavior

1. Network setup and overlay loading are required by `gizmo.target`.
2. Hardware consumers require successful overlay setup.
3. ZeroMQ requires ZMon and the privileged control socket.
4. OPC-UA requires ZeroMQ, temperature, and SDR.
5. Long-running services use bounded restart delays.
6. Stopping `gizmo.target` stops every component; hardware teardown unloads the
   overlay last through dependency ordering.

All stdout/stderr goes to the journal. The package does not create unbounded
multi-megabyte logs in `/dev/shm`.

## Compatibility

The external ports, OPC-UA namespace/object/variable names, and documented
ZeroMQ commands are retained. Persistent command values use the recovered
environment-file format so rollback remains possible.
