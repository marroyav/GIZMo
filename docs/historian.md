# Persistent historian

> **Status:** Implemented in runtime 0.4.0 on 27 July 2026.

## Purpose

`gizmo-historian.service` records a bounded, status-aware history of the
canonical `urn:fnal:gizmo` OPC UA model. It allows the dashboard to plot data
captured before the browser was opened and preserves history across dashboard
reloads and service restarts.

OPC UA remains the public monitoring and control contract. The historian is a
read-only local record, not a command channel, and does not replace a
site-level controls historian.

## Data path and failure boundary

```text
                                      live values
gizmo-opcua.service ───────────────► gizmo-dashboard.service ──► browser
          │                                      ▲
          │ one fixed local subscription         │ bounded history queries
          ▼                                      │ over a private Unix socket
gizmo-historian.service ───────── SQLite ─────────┘
          │
          └── alarm, quality, clock, boot, network, and service events
```

The live and historical paths use independent OPC UA sessions. A historian or
database failure therefore does not stop ZMon, OPC UA, the front-panel
display, or the live dashboard. Browser count does not increase historian
sessions or database writers.

## Capture profile

The recorder uses a fixed allow-list derived from the canonical model:

- one compressed fast snapshot per second for measurements, the
  authoritative composite relay/beacon alarm, lock-in values, phase, thermal
  values, uptime, and the SDR frame sequence;
- one compressed platform snapshot every ten seconds for chartable OS,
  storage, process, filesystem, and network counters;
- one-minute numeric rollups with first, last, minimum, maximum, mean, count,
  and worst OPC UA status;
- transition events for alarms, latches, measurement range/quality, service
  state, restart counts, time synchronization, boot identity, IP/MAC/carrier
  state, and historian connection or disk warnings.

Every stored scalar carries its OPC UA value, status code, and source
timestamp. The row also carries historian receive time and, for fast samples,
the measurement sequence and historian stream epoch.

`Alarm.Active` is stored directly from the Boolean emitted by ZMon at the same
decision branch that drives the physical relay/beacon. It is not inferred
again from resistance or phase. Raw queries retain `false`/`true`; minute
rollups represent the active fraction and retain first, last, minimum, and
maximum so an assertion within a bucket remains discoverable.

Raw SDR frames and ADC waveforms are deliberately excluded. At 2048 samples
per frame they would dominate storage and eMMC writes without helping normal
slow-controls trending.

### HIGH Z semantics

An out-of-range resistance is stored as:

```text
value = NULL
status = Good
range = OutOfRange
```

`HIGH Z` is a valid good-quality range state, not a source or measurement
failure. The database never stores 500 Ω as a measurement. The browser derives its
chartreuse line at the validated 500 Ω boundary from the separate range state.
CSV exports retain the empty resistance, status code, range, and range status.

## SQLite store

The database is:

```text
/var/lib/gizmo/history/gizmo-history.sqlite3
```

It is owned by `gizmo:gizmo`; the containing directory is mode `0750`.
Package upgrades and removal retain it with the other device-specific state.

Schema version 2 contains:

| Table | Contents |
|---|---|
| `stream` | boot ID, sequence epoch, start/end, and rollover reason |
| `fast_sample` | receive time, stream, sequence, and compressed fast payload |
| `platform_sample` | receive time and compressed platform payload |
| `minute_rollup` | minute bucket, sample count, and compressed aggregates |
| `event` | transition time, severity, stable key, summary, and bounded JSON |
| `historian_meta` | schema and creation metadata |

Payloads are compact zlib-compressed JSON arrays whose ordering is fixed by
the installed model catalog. SQLite runs in WAL mode with
`synchronous=FULL`, foreign keys, a ten-second busy timeout, and incremental
auto-vacuum. `PRAGMA quick_check` must pass before acquisition starts.

The historian assigns a new stream when it starts, the board boot ID changes,
or the upstream measurement sequence moves backward. UTC is not used as a
unique key, so a clock correction cannot overwrite an earlier sample.

## Retention and disk guard

Runtime 0.4.0 defaults are:

| Record class | Retention |
|---|---:|
| one-second fast samples | 14 days |
| ten-second platform samples | 30 days |
| one-minute rollups | 365 days |
| transition events | 1825 days |

Retention runs hourly and returns free pages incrementally. The recorder stops
new writes if available filesystem space falls below the larger of 2 GiB or
15% of filesystem capacity. It continues checking for recovery and reports
the limited state and dropped-sample count through `/status`.

`GIZMO_HISTORIAN_RETENTION_ENABLED=0` explicitly disables age-based pruning
for a permanent off-board replica. The configured day values remain visible
as policy metadata, but no table is pruned and queries are not restricted to
the event-retention interval. The Kria configuration keeps retention enabled;
unlimited retention is not appropriate for its eMMC.

Replication transfers inserts and updated rollups, never deletions. A row
already committed to a replica therefore remains until that replica's own
policy or an operator removes it. A replica disconnected beyond the Kria raw
retention window cannot reconstruct one-second rows that expired before they
were copied.

### Measured storage budget

A local replay used the actual compressed payloads received from the live Kria
and built a checkpointed SQLite database containing:

- 86,400 fast rows;
- 8,640 platform rows;
- 1,440 minute rollups;
- a conservative 100 transition events.

The resulting one-day database was about 48.5 MB (46.2 MiB). Scaled to the
mean calendar month of 30.436875 days, ingestion is about:

```text
1.48 GB/month decimal
1.38 GiB/month binary
```

Use **1.5 GB of SQLite writes per continuously operating month** as the
planning value. This is write volume, not unbounded retained capacity. A
second full-retention replay of 14 days raw, 30 days platform, one year of
rollups, and five years at 100 events/day occupied about 1.26 GB
(1.17 GiB). Real event volume is expected to be much lower.

The live `/status` response also estimates monthly bytes from the current
average payload sizes. Payload size varies slightly with strings and status
details, so a long board soak should replace the short replay for final eMMC
wear planning.

## Read-only query interface

The historian listens only on:

```text
/run/gizmo/historian.sock
```

The socket supports:

| Route | Purpose |
|---|---|
| `/status` | connection, drops, database size, bounds, and retention |
| `/series` | allow-listed retained series |
| `/query` | bounded raw or rollup time-series query |
| `/events` | bounded transition query |
| `/export.csv` | bounded CSV export |
| `/replication` | bounded raw batches for off-board replication |
| `/healthz` | status alias for service checks |

The dashboard exposes the corresponding same-origin
`/api/history/{status,series,query,events,export.csv,replication}` routes. It forwards only
the fixed GET routes, while the historian validates every parameter; neither
the Unix socket nor SQLite file is exposed.

When `GIZMO_HISTORIAN_REPLICA_URL` is set, the same process runs in replica
mode instead of opening its own OPC UA recording session. It pins the edge
machine identity, imports raw batches transactionally, and keeps per-table
cursors in the destination database. This mode is intended for independent
off-board replicas; the Kria itself must run normal recording mode.

Query rules:

- `from` and `to` require timestamps with an explicit UTC offset;
- series names must match the allow-list exactly;
- normal queries are capped at 5,000 returned points;
- CSV exports are capped at 100,000 rows;
- raw data is returned when it fits, otherwise one-minute rollups are
  automatically selected and may be coarsened further;
- non-good values and gaps remain explicit;
- mutation methods return `405 Method Not Allowed`.

Examples:

```sh
sudo gizmo-historian-client status
sudo gizmo-historian-client series
sudo gizmo-historian-client query \
  --from 2026-07-27T12:00:00-06:00 \
  --to 2026-07-27T13:00:00-06:00 \
  Measurement.ResistanceOhm Measurement.ThresholdOhm
sudo gizmo-historian-client export \
  --from 2026-07-27T12:00:00-06:00 \
  --to 2026-07-27T13:00:00-06:00 \
  --output gizmo-history.csv \
  Measurement.ResistanceOhm
```

## Dashboard behavior

The dashboard has an explicit **Live / History** selector. History mode
supports 1 hour, 6 hour, 24 hour, 7 day, and custom local date/time windows,
labels the returned raw or rollup resolution, preserves plotted gaps and
status-aware tooltips, renders all five primary views on a shared time range,
and exports the requested database interval to CSV.

If the historian is unavailable, the History view says so while the live
tiles and live trend remain operational.

## Service ownership and security

`gizmo-historian.service` is a `PartOf=gizmo.target` member and starts after
OPC UA. It runs as the locked `gizmo` user with no capabilities, private
devices, a read-only system filesystem, a 256 MiB memory ceiling, and write
access limited to `/var/lib/gizmo/history` and `/run/gizmo`.

Historical data includes detector state, host identity, network state, and
maintenance transitions. TCP 8080 and OPC UA 4840 must remain on the
restricted controls network or behind site-managed authenticated TLS. Event
details are bounded and must never include credentials, keys, arbitrary
journal output, or environment secrets.

## Current limitations

- The server does not implement OPC UA Historical Access (`HistoryRead`);
  history is available through the read-only dashboard/Unix HTTP API.
- Cursor-based off-board replication is implemented, but deployment,
  monitoring, immutable backup, and restore testing remain site-owned.
- Event markers are retained and queryable but are not yet overlaid on the
  trend canvas.
- The short storage replay verifies layout and integrity, but a 24-hour board
  soak and power-interruption test are still required for final wear and
  recovery acceptance.
