# Live web dashboard

## Purpose

`gizmo-dashboard.service` provides a clear, read-only instrument view at:

```text
http://<gizmo-address>:8080/
```

For the current controls interface, use
`http://<redacted-private-ip>:8080/`. The dashboard is hosted by the Kria so it
continues to work on the private instrument network without a cloud service or
an Internet route.

The top of the page intentionally emphasizes only the operator-critical state:

- equivalent resistance, including the explicit `HIGH Z` out-of-range state;
- configured alarm threshold, capacitance, phase, and stimulus frequency;
- current alarm condition, board-local time, and latched time;
- overall health and essential OS/firmware indicators.

Lower sections provide selectable live trends, subsystem quality, both PS
network interfaces, owned systemd services, and a searchable set of typed OPC
UA monitoring variables. Selecting a chartable variable in the explorer opens
it as a dedicated trend.

## Data path

```text
GIZMo producers
      │
      ▼
gizmo-opcua.service ── one OPC UA subscription ── gizmo-dashboard.service
                                                       │
                                           cached JSON + server-sent events
                                                       │
                                           one or more local web browsers
```

The server resolves `urn:fnal:gizmo` at connection time and subscribes to the
stable string NodeIds. Each browser receives the same cached state; browsers
do not create their own OPC UA sessions or poll the producers. If a new web
package references a node that an older OPC UA model does not yet contain, only
that variable receives `BadNodeIdUnknown`; the remaining live view stays
available.

The subscription is supplemented by one batched local read every five seconds.
This reconciles status-only transitions that some OPC UA SDKs do not emit when
the underlying scalar value is unchanged; it does not create another session
or any browser-driven polling.

OPC UA values retain their value, `StatusCode`, source/server timestamps, and
dashboard receive time. The browser does not reinterpret a non-good numeric
value as zero. `Measurement.ResistanceOhm` is shown only as a number when it is
available; `Measurement.ResistanceRange = OutOfRange` is rendered as
`HIGH Z`.

## Plot behavior

The browser retains at most one hour of samples in memory. This is a live
session buffer, not a persistent historian:

- closing or reloading the tab clears its history;
- the plot can show 1 minute, 5 minutes, 15 minutes, or 1 hour;
- pausing freezes history collection in that tab while status tiles remain
  live;
- **Export CSV** downloads the active view with UTC timestamps and an OPC UA
  status-code column for every series.

The standard views cover impedance, temperatures, lock-in components, phase,
and system utilization. Other chartable scalars can be selected from the
variable explorer.

When `Measurement.ResistanceRange = OutOfRange`, the impedance plot adds an
electric-blue `HIGH Z (>500 Ω)` trace at the 500 Ω validated-range boundary.
This is a clipped visual state, not a fabricated 500 Ω measurement:
`ResistanceOhm` remains unavailable with `BadOutOfRange`, and the impedance
CSV exports the canonical range and its status code rather than replacing the
missing resistance value with 500.

## Read-only HTTP interface

The same-origin browser API is deliberately small:

| Path | Meaning |
|---|---|
| `/` | self-contained dashboard |
| `/api/catalog` | monitored variable metadata and chart presets |
| `/api/state` | latest cached values and connection state |
| `/api/stream` | one-second server-sent-event stream |
| `/healthz` | process and upstream OPC UA connection health |

There are no HTTP write, command, login, upload, or proxy endpoints. POST
requests return `405 Method Not Allowed`. Configuration and explicit
instrument operations remain in the canonical OPC UA contract and its
operator client.

## Service configuration

Defaults in `/etc/gizmo/runtime.env` are:

```sh
GIZMO_DASHBOARD_OPCUA_ENDPOINT=opc.tcp://127.0.0.1:4840
GIZMO_DASHBOARD_BIND=0.0.0.0
GIZMO_DASHBOARD_PORT=8080
GIZMO_DASHBOARD_SUBSCRIPTION_MS=500
GIZMO_DASHBOARD_PUBLISH_INTERVAL_SECONDS=1
```

After changing them:

```sh
sudo systemctl restart gizmo-dashboard.service
systemctl status gizmo-dashboard.service
curl http://127.0.0.1:8080/healthz
```

The health response is `status: degraded` while the web service is alive but
its OPC UA session is disconnected. The service reconnects with bounded
backoff and browsers reconnect their event stream automatically.

## Security boundary

The page contains no third-party scripts, fonts, analytics, cookies, or
external network requests. Content-Security-Policy limits scripts, styles, and
connections to the Kria origin. The systemd service runs as the locked
`gizmo` user with a read-only filesystem and no Linux capabilities.

HTTP itself is not authenticated or encrypted. Port 8080 exposes operational
and inventory data to anyone who can reach it, so it must remain on the
restricted controls VLAN or behind a site-managed authenticated TLS reverse
proxy. The dashboard's read-only property does not make an untrusted network
safe, and it does not change the separate security requirements of OPC UA and
the legacy compatibility ports.
