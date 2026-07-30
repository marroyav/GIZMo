# Off-board monitoring replicas

## Deployed topology

The first off-board replica runs on `<redacted-site-host>` under:

```text
/srv/gizmo-monitor
├── app/       dashboard and historian sources/assets
├── bin/       idempotent start, restart loop, and status commands
├── config/    runtime configuration and the installed crontab
├── data/      SQLite history
├── logs/      process logs
├── run/       private historian query socket
└── venv/      pinned Python environment
```

The board remains on its isolated `<redacted-private-ip>` maintenance link. A
workstation user service creates loopback-only reverse forwards on
`<redacted-site-host>`:

| Server listener | Destination |
|---|---|
| `127.0.0.1:22053` | board SSH 22 |
| `127.0.0.1:48453` | board OPC UA 4840 |
| `127.0.0.1:18080` | board dashboard 8080 |

The board-local historian is the authoritative edge buffer. The off-board
historian incrementally mirrors its raw rows through the fixed, read-only
`http://127.0.0.1:18080/api/history/replication` route. The server-side
dashboard keeps an independent OPC UA session for its live display, binds only
`127.0.0.1:18081`, and reads historical plots from the mirrored SQLite
database. The workstation relay forwards that dashboard address back to the
operator at:

```text
http://127.0.0.1:18081/
```

None of these ports is exposed on a public server interface.

## Operator checks

Check the deployed processes, upstream connection, sample counts, retention,
and disk budget:

```sh
ssh site-operator@<redacted-site-host> \
  /srv/gizmo-monitor/bin/status
```

The SQLite database is:

```text
/srv/gizmo-monitor/data/gizmo-history.sqlite3
```

Do not copy that file while it is live. Use the SQLite online backup API or a
checkpointed immutable export for replication and tape archival.

Every retained scalar includes its OPC UA source timestamp and status. Fast
rows also retain historian receive time, measurement sequence, and stream
identity. A board or OPC UA restart that moves the sequence backward creates a
new stream instead of overwriting earlier samples.

## Outage recovery

`gizmo-historian.service` must remain enabled on the Kria. It records one-second
measurement/alarm rows, ten-second platform rows, minute rollups, and transition
events even when the workstation relay or `<redacted-site-host>` is unreachable.

The off-board historian stores an atomic cursor for each raw table and pins the
Kria machine identity on first contact. After connectivity returns it requests
bounded base64-encoded batches and imports the original compressed rows,
timestamps, status codes, stream epochs, rollups, and events in SQLite
transactions. A crash before commit leaves the cursor unchanged; replaying an
acknowledged batch is idempotent.

The raw edge buffer is 14 days. An outage longer than that can still recover
retained minute rollups and events, but one-second rows already expired on the
Kria cannot be reconstructed. Monitor replica lag and keep it comfortably
below the edge retention window.

## Persistence

`<redacted-site-host>` does not grant the operator passwordless administration and
the user does not have systemd lingering enabled. The current deployment
therefore uses a detached `tmux` supervisor with an idempotent five-second
component restart loop. User cron starts it at boot and checks every five
minutes that the session exists.

This is adequate for the initial monitor but is not the final service
ownership model. For long-term operation, the server administrators should
install equivalent system-level services with:

- a dedicated locked service identity;
- `Restart=always`;
- write access restricted to the replica directory;
- a read-only application directory;
- journal or bounded file logging;
- health monitoring for connection state, dropped samples, database
  integrity, and free-space guard activation.

## Replicating to two additional servers

Run an independent replica historian on each server rather than synchronizing
live SQLite WAL files. Every replica advances its own atomic cursor against the
same board-local edge history, so a server outage does not prevent another
replica from recording or later catching up.

The measured planning rate is 1.48 GB per continuously operating month per
replica without retention. The current retention policy reached about 1.26 GB
steady-state in replay. Provision at least 10 GB per bounded replica, or
25 GB per replica for one year of unpruned full-resolution history.

The replica kit is in `deploy/offboard/`. Update the endpoint, bind address,
port, and storage root in `runtime.env.example` for each target. Do not expose
the anonymous OPC UA or HTTP endpoints beyond the restricted monitoring path.

## Current dependency

Until a registered Fermilab-facing board link is physically connected and
configured, live viewing and replication depend on the workstation SSH relay.
The Kria continues recording locally if that relay stops. The relay reconnects
automatically while a valid Fermilab Kerberos ticket is available, after which
the server replica catches up without operator intervention. A direct routed
board address can later replace the relay without changing the historian
schema or dashboard.
