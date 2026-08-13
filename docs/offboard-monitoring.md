# Off-board monitoring replicas

This document describes the generic replica design. Hostnames, user accounts,
network routes, storage locations, and source identities are controlled site
configuration and are not committed.

## Suggested layout

The publication-safe examples use `/srv/gizmo-monitor`:

```text
/srv/gizmo-monitor
├── app/       dashboard and historian sources/assets
├── bin/       idempotent start, restart loop, and status commands
├── config/    runtime configuration and service definition
├── data/      SQLite history
├── logs/      bounded process logs
├── run/       private historian query socket
└── venv/      pinned Python environment
```

The transport into this host is site-owned. If an SSH relay is approved, keep
its server listeners on loopback and use site-assigned ports. A representative
mapping is:

| Server listener | Destination |
|---|---|
| `127.0.0.1:22000` | board SSH 22 |
| `127.0.0.1:48400` | board OPC UA 4840 |
| `127.0.0.1:18080` | board dashboard 8080 |

These are examples, not deployed endpoints. Do not expose anonymous OPC UA or
HTTP listeners on a public interface.

## Replication behavior

The board-local historian is the authoritative edge buffer. Each off-board
historian advances its own atomic cursor against the fixed read-only history
replication route. A server outage therefore does not prevent another replica
from recording or later catching up.

The replica pins the source identity on first contact, requests bounded
base64-encoded batches, and imports rows, timestamps, status codes, stream
epochs, rollups, and events in SQLite transactions. A crash before commit
leaves the cursor unchanged; replaying an acknowledged batch is idempotent.

The raw edge buffer is 14 days. An outage longer than that can still recover
retained minute rollups and events, but expired one-second rows cannot be
reconstructed. Monitor replica lag and storage headroom.

## Operations

Use a dedicated locked service identity, automatic restart, a read-only
application directory, restricted write access to data/log/run directories,
and bounded logging. Monitor upstream connectivity, cursor lag, dropped
samples, database integrity, and the free-space guard.

Never copy a live SQLite file while its WAL writer is active. Use the SQLite
online-backup API or a checkpointed immutable export. The example configuration
under `deploy/offboard/` uses loopback-only endpoints and contains no live
database or source identity.
