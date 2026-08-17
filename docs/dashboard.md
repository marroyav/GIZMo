# Live and historical web dashboard

## Purpose

`gizmo-dashboard.service` provides a clear, read-only instrument view at:

```text
http://<gizmo-address>:8080/
```

For the current controls interface, use
`http://<gizmo-address>:8080/`. The dashboard is hosted by the Kria so it
continues to work on the private instrument network without a cloud service or
an Internet route.

The top of the page intentionally emphasizes only the operator-critical state:

- equivalent resistance, including the explicit `HIGH Z` out-of-range state;
- configured alarm threshold, capacitance, phase, and stimulus frequency;
- current alarm condition, board-local time, and latched time;
- overall health and essential OS/firmware indicators.

Lower sections provide simultaneous live or persistent trends, subsystem
quality, both PS network interfaces, owned systemd services, and a searchable
set of typed OPC UA monitoring variables. The six standard plots share the
same time range, synchronized cursor, and zoom/pan viewport so impedance,
thermal, lock-in, phase, alarm, and system behavior can be correlated without
switching tabs. A separate installed-calibration plot reads the live
resistance table from the board and displays lock-in magnitude, a sinusoidal
RMS estimate, and phase as functions of impedance.
Selecting a chartable variable in the explorer adds it as another trend. The
advanced variable inventory is collapsed by default so the operational page
stays compact.

## Visual status language

The interface is a single dark industrial theme using JetBrains Mono when it
is installed on the operator workstation, with packaged system monospace
fallbacks. Status meaning is kept consistent across cards, chips, services,
and variables:

| Color | Meaning |
|---|---|
| chartreuse | healthy, connected, active, in-range, or intended `HIGH Z` |
| orange | currently asserted ground alarm only |
| steel gray | uncertain, unavailable, idle, or neutral status |
| graphite and off-white | structure, charts, and text |

A historical latch remains visible with its timestamp, but it does not turn
the console orange after the active alarm condition has cleared.

Color is never the only indicator: every state also has text, an OPC UA status
code, an icon/dot, or a line label. The page uses semantic HTML5 landmarks,
native date/time controls, a native disclosure for advanced telemetry, and
keyboard-visible focus states.

## Data path

```text
GIZMo producers
      │
      ▼
gizmo-opcua.service ── one live subscription ── gizmo-dashboard.service
          │                                      │             ▲
          │ one historian subscription           │ SSE         │ bounded GET
          ▼                                      ▼             │
gizmo-historian.service ─── SQLite ─── private Unix socket ─────┘
                                             one or more browsers
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

The explicit **Live / History** selector keeps the two data sources clear.
Live mode retains at most one hour of samples in browser memory:

- closing or reloading the tab clears its history;
- the plot can show 1 minute, 5 minutes, 15 minutes, or 1 hour;
- pausing freezes history collection in that tab while status tiles remain
  live;
- **Export all CSV** downloads all plotted series with UTC timestamps and an
  OPC UA status-code column for every series.

The **Zoom in**, **Zoom out**, and **Reset zoom** controls apply one viewport
to every telemetry panel. A mouse wheel zooms around the pointer, dragging a
zoomed plot pans every panel, and double-clicking resets the viewport. These
interactions change only the browser view; they do not change the selected
history query or CSV export interval. The control bar reports the visible
span. In Live mode, button zoom stays attached to the newest sample until the
operator pans away from the live edge.

History mode queries the package-owned SQLite historian and supports 1 hour,
6 hour, 24 hour, 7 day, and custom local date/time windows. It indicates
whether points are raw or rollups and exports the selected database interval.
The server caps normal plots at 5,000 points and automatically selects or
coarsens minute rollups for larger intervals.

The standard views cover the authoritative composite alarm, impedance,
temperatures, lock-in components, phase, and system utilization
simultaneously. The alarm trace is `0/NORMAL` in chartreuse and `1/ALARM` in
orange. The live and historical paths both consume `Alarm.Active`; neither the
browser nor dashboard server recomputes the resistance and phase rules.
Other chartable scalars can be selected from the variable explorer.

When `Measurement.ResistanceRange = OutOfRange`, the impedance plot adds a
chartreuse `HIGH Z (>500 Ω)` trace at the 500 Ω validated-range boundary.
This is a clipped visual state, not a fabricated 500 Ω measurement:
`ResistanceOhm` remains non-numeric with `Good` status because `HIGH Z` is a
valid range state, and the impedance CSV exports the canonical range and its
status code rather than replacing the
missing resistance value with 500.

## Installed calibration plot

`/api/calibration/resistance` reads the live legacy `RCalData` node through
the dashboard's existing OPC UA session and validates the flattened
four-column `Rcalibration_ph.csv` payload. The plot uses:

- known impedance `z` in ohms;
- lock-in vector magnitude `sqrt(I² + Q²)` in ADC-count units;
- a sinusoidal RMS estimate, defined explicitly as `magnitude / sqrt(2)`;
- the `atan2` phase in degrees.

The RMS curve is an amplitude-derived estimate, not a statistical RMS over
the raw ADC waveform or repeated calibration reads. Computing that quantity
would require retaining the underlying ADC samples or one I/Q result per
read, neither of which is present in the calibration CSV. The 1 MΩ
open-circuit anchor is summarized separately so it does not compress the
validated 0–500 Ω response. The calibration plot has independent wheel,
button, drag, and double-click zoom controls.

## Read-only HTTP interface

The same-origin browser API is deliberately small:

| Path | Meaning |
|---|---|
| `/` | self-contained dashboard |
| `/api/catalog` | monitored variable metadata and chart presets |
| `/api/state` | latest cached values and connection state |
| `/api/stream` | one-second server-sent-event stream |
| `/api/calibration/resistance` | validated live resistance calibration rows, RMS estimate, and phase |
| `/api/history/status` | historian connection, storage, and time bounds |
| `/api/history/series` | retained-series metadata |
| `/api/history/query` | bounded raw or rollup query |
| `/api/history/events` | bounded transition query |
| `/api/history/export.csv` | bounded persistent CSV export |
| `/healthz` | process and upstream OPC UA connection health |

The history routes are a fixed read-only proxy to
`/run/gizmo/historian.sock`; they cannot name a database file or submit SQL.
There are no HTTP write, command, login, or upload endpoints. POST requests
return `405 Method Not Allowed`. Configuration and explicit instrument
operations remain in the canonical OPC UA contract and its operator client.

## Service configuration

Defaults in `/etc/gizmo/runtime.env` are:

```sh
GIZMO_DASHBOARD_OPCUA_ENDPOINT=opc.tcp://127.0.0.1:4840
GIZMO_DASHBOARD_BIND=0.0.0.0
GIZMO_DASHBOARD_PORT=8080
GIZMO_DASHBOARD_SUBSCRIPTION_MS=500
GIZMO_DASHBOARD_PUBLISH_INTERVAL_SECONDS=1
GIZMO_DASHBOARD_HISTORIAN_SOCKET=/run/gizmo/historian.sock
```

After changing them:

```sh
sudo systemctl restart gizmo-dashboard.service
systemctl status gizmo-dashboard.service
curl http://127.0.0.1:8080/healthz
```

The health response is `status: degraded` while the web service is alive but
its OPC UA session is disconnected. `historian_available` separately reports
whether the private query socket exists. History can be unavailable while
live telemetry stays healthy. The service reconnects with bounded backoff and
browsers reconnect their event stream automatically.

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
