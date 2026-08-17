#!/usr/bin/env python3
"""Persistent, status-aware historian for the canonical GIZMo OPC UA model."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import io
import json
import math
import os
import signal
import socket
import socketserver
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import zlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urlsplit

from gizmo_dashboard import (
    CATALOG,
    NAMESPACE_URI,
    VARIABLES,
    OpcUaMonitor,
)


SCHEMA_VERSION = 2
DEFAULT_ENDPOINT = "opc.tcp://127.0.0.1:4840"
DEFAULT_DATABASE = "/var/lib/gizmo/history/gizmo-history.sqlite3"
DEFAULT_SOCKET = "/run/gizmo/historian.sock"
DEFAULT_CAPTURE_INTERVAL = 1.0
DEFAULT_PLATFORM_INTERVAL = 10.0
DEFAULT_RAW_RETENTION_DAYS = 14
DEFAULT_PLATFORM_RETENTION_DAYS = 30
DEFAULT_ROLLUP_RETENTION_DAYS = 365
DEFAULT_EVENT_RETENTION_DAYS = 1825
DEFAULT_MAX_QUERY_POINTS = 5000
DEFAULT_MAX_EXPORT_ROWS = 100_000
DEFAULT_MIN_FREE_BYTES = 2 * 1024**3
DEFAULT_REPLICA_POLL_SECONDS = 2.0
DEFAULT_REPLICA_BATCH_SIZE = 1000
MAX_REPLICA_BATCH_SIZE = 2000
MAX_REPLICA_RESPONSE_BYTES = 8 * 1024**2
STATUS_AVERAGE_SAMPLE_ROWS = 4096
REPLICATION_TABLES = (
    "fast_sample",
    "platform_sample",
    "minute_rollup",
    "event",
)
MONTH_SECONDS = 365.2425 / 12 * 86400

RESISTANCE_RANGE_PATH = "Measurement.ResistanceRange"
_LEGACY_FAST_SERIES_PATHS = tuple(
    spec.path
    for spec in VARIABLES
    if spec.chartable
    and (
        spec.path.startswith("Measurement.")
        or spec.path.startswith("Thermal.")
        or spec.path in {"Time.UptimeSeconds", "SDR.FrameSequence"}
    )
)
FAST_QUERY_PATHS = (*_LEGACY_FAST_SERIES_PATHS, "Alarm.Active")
PLATFORM_QUERY_PATHS = tuple(
    spec.path
    for spec in VARIABLES
    if spec.chartable and spec.path not in FAST_QUERY_PATHS
)
# Preserve every pre-0.4.3 rollup index and append the new Boolean series.
ROLLUP_PATHS = (
    *tuple(
        spec.path
        for spec in VARIABLES
        if spec.chartable and spec.path != "Alarm.Active"
    ),
    "Alarm.Active",
)
FAST_CAPTURE_PATHS = tuple(
    dict.fromkeys(
        (
            "Identity.ModelVersion",
            "Identity.RuntimeVersion",
            "Identity.BootId",
            "Measurement.Sequence",
            "Measurement.SampleTime",
            # Keep the binary payload layout compatible with existing
            # historian rows: Alarm.Active was already retained below.
            *_LEGACY_FAST_SERIES_PATHS,
            RESISTANCE_RANGE_PATH,
            "Measurement.Quality",
            "Measurement.Diagnostic",
            "Alarm.Active",
            "Alarm.Latched",
            "Alarm.Reason",
            "Alarm.LatchTime",
        )
    )
)
PLATFORM_CAPTURE_PATHS = PLATFORM_QUERY_PATHS
EVENT_PATHS = (
    "Identity.RuntimeVersion",
    "Identity.BootId",
    "Measurement.StimulusCurrentRmsAmpere",
    "Measurement.ResistanceRange",
    "Measurement.CapacitanceRange",
    "Measurement.Quality",
    "Alarm.Active",
    "Alarm.Latched",
    "Alarm.Reason",
    "Alarm.LatchTime",
    "Operations.CommandGateState",
    "Operations.LastCommandId",
    "Operations.LastCommandName",
    "Operations.LastCommandParameters",
    "Operations.LastCommandRequester",
    "Operations.LastCommandState",
    "Operations.LastCommandRequestTime",
    "Operations.LastCommandCompletionTime",
    "Operations.LastCommandResult",
    "Calibration.OperationState",
    "Calibration.ProgressPercent",
    "Calibration.LastOperationTime",
    "Calibration.LastOperationResult",
    "Calibration.RestorationState",
    "Health.Overall",
    "Health.Measurement",
    "Health.Thermal",
    "Health.Time",
    "Health.OperatingSystem",
    "Health.Network",
    "Health.Storage",
    "Health.Firmware",
    "Health.Services",
    "Health.Calibration",
    "Health.SDR",
    "Time.NtpSynchronized",
    "Network.Interfaces.eth0.Carrier",
    "Network.Interfaces.eth0.Addresses",
    "Network.Interfaces.eth0.MacAddress",
    "Network.Interfaces.eth1.Carrier",
    "Network.Interfaces.eth1.Addresses",
    "Network.Interfaces.eth1.MacAddress",
)
for _service_path in (
    spec.path
    for spec in VARIABLES
    if spec.path.startswith("Services.Units.")
    and (
        spec.path.endswith(".ActiveState")
        or spec.path.endswith(".Result")
        or spec.path.endswith(".RestartCount")
    )
):
    EVENT_PATHS += (_service_path,)

QUERY_PATHS = frozenset((*FAST_QUERY_PATHS, *PLATFORM_QUERY_PATHS))
PATH_INDEX = {path: index for index, path in enumerate(ROLLUP_PATHS)}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def now_us() -> int:
    return time.time_ns() // 1000


def local_source_identity() -> dict[str, str]:
    machine_id = ""
    try:
        machine_id = Path("/etc/machine-id").read_text().strip()
    except OSError:
        pass
    hostname = socket.gethostname()
    return {
        "id": machine_id or f"hostname:{hostname}",
        "hostname": hostname,
    }


def parse_time_us(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        # Public history APIs use epoch milliseconds; internal callers may
        # already provide microseconds.
        return int(numeric if abs(numeric) >= 10**14 else numeric * 1000)
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires an explicit UTC offset")
    return int(parsed.timestamp() * 1_000_000)


def iso_from_us(value: int | None) -> str | None:
    if value is None:
        return None
    return dt.datetime.fromtimestamp(value / 1_000_000, dt.timezone.utc).isoformat()


def status_rank(status: str) -> int:
    lowered = status.lower()
    if lowered.startswith("bad"):
        return 3
    if lowered.startswith("uncertain"):
        return 2
    if lowered == "good":
        return 0
    return 1


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def payload_entry(payload: dict[str, Any] | None) -> list[Any]:
    payload = payload or {}
    value = payload.get("value")
    if isinstance(value, float) and not math.isfinite(value):
        value = None
    return [
        value,
        str(payload.get("status") or "BadWaitingForInitialData"),
        parse_time_us(payload.get("source_timestamp")),
    ]


def encode_payload(
    paths: tuple[str, ...],
    values: dict[str, dict[str, Any]],
) -> bytes:
    body = json.dumps(
        [payload_entry(values.get(path)) for path in paths],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return zlib.compress(body, level=1)


def decode_payload(paths: tuple[str, ...], payload: bytes) -> dict[str, dict[str, Any]]:
    entries = json.loads(zlib.decompress(payload))
    result: dict[str, dict[str, Any]] = {}
    if len(paths) != len(entries):
        raise ValueError(
            "historian payload entry count does not match the configured paths"
        )
    for path, entry in zip(paths, entries):
        value, status, source_time_us = entry
        result[path] = {
            "value": value,
            "status": status,
            "source_time_us": source_time_us,
        }
    return result


class MinuteAccumulator:
    """Compact status-aware aggregation for one UTC minute."""

    def __init__(self, bucket_us: int) -> None:
        self.bucket_us = bucket_us
        self.sample_count = 0
        self.high_z_count = 0
        self.high_z_status = "Good"
        self.series: dict[int, dict[str, Any]] = {}

    def update(self, snapshot: dict[str, Any]) -> None:
        self.sample_count += 1
        values = snapshot.get("values", {})
        range_payload = values.get(RESISTANCE_RANGE_PATH, {})
        range_status = str(
            range_payload.get("status") or "BadWaitingForInitialData"
        )
        if range_payload.get("value") == "OutOfRange":
            self.high_z_count += 1
            if status_rank(range_status) >= status_rank(self.high_z_status):
                self.high_z_status = range_status

        for index, path in enumerate(ROLLUP_PATHS):
            payload = values.get(path, {})
            status = str(payload.get("status") or "BadWaitingForInitialData")
            value = finite_number(payload.get("value"))
            current = self.series.get(index)
            if current is None:
                current = {
                    "first": value,
                    "last": value,
                    "minimum": value,
                    "maximum": value,
                    "total": value or 0.0,
                    "count": 1 if value is not None else 0,
                    "status": status,
                }
                self.series[index] = current
                continue
            if status_rank(status) >= status_rank(current["status"]):
                current["status"] = status
            if value is None:
                continue
            if current["count"] == 0:
                current["first"] = value
                current["minimum"] = value
                current["maximum"] = value
            current["last"] = value
            current["minimum"] = min(current["minimum"], value)
            current["maximum"] = max(current["maximum"], value)
            current["total"] += value
            current["count"] += 1

    def encode(self) -> bytes:
        rows = []
        for index, item in sorted(self.series.items()):
            count = item["count"]
            rows.append(
                [
                    index,
                    item["first"],
                    item["last"],
                    item["minimum"],
                    item["maximum"],
                    item["total"] / count if count else None,
                    count,
                    item["status"],
                ]
            )
        body = {
            "sample_count": self.sample_count,
            "high_z_count": self.high_z_count,
            "high_z_status": self.high_z_status,
            "series": rows,
        }
        return zlib.compress(
            json.dumps(body, separators=(",", ":"), allow_nan=False).encode(),
            level=1,
        )

    @classmethod
    def decode(cls, bucket_us: int, payload: bytes) -> "MinuteAccumulator":
        body = json.loads(zlib.decompress(payload))
        result = cls(bucket_us)
        result.sample_count = int(body.get("sample_count", 0))
        result.high_z_count = int(body.get("high_z_count", 0))
        result.high_z_status = str(body.get("high_z_status", "Good"))
        for row in body.get("series", []):
            (
                index,
                first,
                last,
                minimum,
                maximum,
                mean,
                count,
                status,
            ) = row
            result.series[int(index)] = {
                "first": first,
                "last": last,
                "minimum": minimum,
                "maximum": maximum,
                "total": (mean or 0.0) * int(count),
                "count": int(count),
                "status": status,
            }
        return result


class HistorianStore:
    def __init__(
        self,
        database: Path,
        *,
        raw_retention_days: int = DEFAULT_RAW_RETENTION_DAYS,
        platform_retention_days: int = DEFAULT_PLATFORM_RETENTION_DAYS,
        rollup_retention_days: int = DEFAULT_ROLLUP_RETENTION_DAYS,
        event_retention_days: int = DEFAULT_EVENT_RETENTION_DAYS,
        retention_enabled: bool = True,
        max_query_points: int = DEFAULT_MAX_QUERY_POINTS,
        max_export_rows: int = DEFAULT_MAX_EXPORT_ROWS,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    ) -> None:
        self.database = database
        self.raw_retention_days = raw_retention_days
        self.platform_retention_days = platform_retention_days
        self.rollup_retention_days = rollup_retention_days
        self.event_retention_days = event_retention_days
        self.retention_enabled = retention_enabled
        self.max_query_points = max_query_points
        self.max_export_rows = max_export_rows
        self.min_free_bytes = min_free_bytes
        self._writer: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            connection = sqlite3.connect(
                f"file:{self.database}?mode=ro",
                uri=True,
                timeout=10,
            )
        else:
            connection = sqlite3.connect(
                self.database,
                timeout=10,
                check_same_thread=False,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        if not readonly:
            # auto_vacuum must be selected while a new database is still
            # empty. Set it before switching the file into WAL mode or
            # creating the schema so retention can return unused pages.
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        connection = self._connect()
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS historian_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS stream (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                boot_id TEXT NOT NULL,
                started_us INTEGER NOT NULL,
                ended_us INTEGER,
                first_sequence INTEGER,
                last_sequence INTEGER,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fast_sample (
                receive_time_us INTEGER PRIMARY KEY,
                stream_id INTEGER NOT NULL REFERENCES stream(id),
                sequence INTEGER,
                payload BLOB NOT NULL
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS fast_sample_status_scan
                ON fast_sample(receive_time_us);

            CREATE TABLE IF NOT EXISTS platform_sample (
                receive_time_us INTEGER PRIMARY KEY,
                payload BLOB NOT NULL
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS platform_sample_status_scan
                ON platform_sample(receive_time_us);

            CREATE TABLE IF NOT EXISTS minute_rollup (
                bucket_us INTEGER PRIMARY KEY,
                sample_count INTEGER NOT NULL,
                payload BLOB NOT NULL,
                updated_us INTEGER NOT NULL
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS minute_rollup_status_scan
                ON minute_rollup(bucket_us);

            CREATE TABLE IF NOT EXISTS event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time_us INTEGER NOT NULL,
                source_time_us INTEGER,
                severity TEXT NOT NULL,
                event_key TEXT NOT NULL,
                summary TEXT NOT NULL,
                details_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS event_time ON event(time_us);

            CREATE TABLE IF NOT EXISTS replication_source (
                source_id TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                first_seen_us INTEGER NOT NULL,
                last_seen_us INTEGER NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS replication_cursor (
                source_id TEXT NOT NULL
                    REFERENCES replication_source(source_id),
                table_name TEXT NOT NULL,
                remote_key INTEGER NOT NULL,
                remote_tie INTEGER NOT NULL,
                updated_us INTEGER NOT NULL,
                PRIMARY KEY(source_id, table_name)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS replication_stream (
                source_id TEXT NOT NULL
                    REFERENCES replication_source(source_id),
                remote_stream_id INTEGER NOT NULL,
                local_stream_id INTEGER NOT NULL UNIQUE
                    REFERENCES stream(id),
                PRIMARY KEY(source_id, remote_stream_id)
            ) WITHOUT ROWID;
            """
        )
        existing = connection.execute(
            "SELECT value FROM historian_meta WHERE key='schema_version'"
        ).fetchone()
        if existing is not None and int(existing["value"]) > SCHEMA_VERSION:
            connection.close()
            raise RuntimeError(
                "historian database schema is newer than this runtime"
            )
        connection.execute(
            """
            INSERT INTO historian_meta(key, value) VALUES('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(SCHEMA_VERSION),),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO historian_meta(key, value)
            VALUES('created_at', ?)
            """,
            (utc_now(),),
        )
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            connection.close()
            raise RuntimeError(f"historian SQLite quick_check failed: {quick_check}")
        connection.commit()
        with self._lock:
            self._writer = connection

    @property
    def writer(self) -> sqlite3.Connection:
        if self._writer is None:
            raise RuntimeError("historian store is not initialized")
        return self._writer

    def close(self) -> None:
        with self._lock:
            if self._writer is None:
                return
            self._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._writer.commit()
            self._writer.close()
            self._writer = None

    def begin_stream(self, boot_id: str, sequence: int | None, reason: str) -> int:
        stamp = now_us()
        with self._lock:
            cursor = self.writer.execute(
                """
                INSERT INTO stream(
                    boot_id, started_us, first_sequence, last_sequence, reason
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (boot_id or "unknown", stamp, sequence, sequence, reason),
            )
            self.writer.commit()
            return int(cursor.lastrowid)

    def end_stream(self, stream_id: int) -> None:
        with self._lock:
            self.writer.execute(
                "UPDATE stream SET ended_us=? WHERE id=? AND ended_us IS NULL",
                (now_us(), stream_id),
            )
            self.writer.commit()

    def record_fast(self, snapshot: dict[str, Any], stream_id: int) -> tuple[int, int]:
        values = snapshot["values"]
        receive_time_us = parse_time_us(snapshot.get("generated_at")) or now_us()
        sequence = values.get("Measurement.Sequence", {}).get("value")
        sequence = int(sequence) if isinstance(sequence, (int, float)) else None
        payload = encode_payload(FAST_CAPTURE_PATHS, values)
        with self._lock:
            while self.writer.execute(
                "SELECT 1 FROM fast_sample WHERE receive_time_us=?",
                (receive_time_us,),
            ).fetchone():
                receive_time_us += 1
            self.writer.execute(
                """
                INSERT INTO fast_sample(
                    receive_time_us, stream_id, sequence, payload
                ) VALUES(?, ?, ?, ?)
                """,
                (receive_time_us, stream_id, sequence, payload),
            )
            self.writer.execute(
                "UPDATE stream SET last_sequence=? WHERE id=?",
                (sequence, stream_id),
            )
            self.writer.commit()
        return receive_time_us, len(payload)

    def record_platform(self, snapshot: dict[str, Any]) -> tuple[int, int]:
        receive_time_us = parse_time_us(snapshot.get("generated_at")) or now_us()
        payload = encode_payload(PLATFORM_CAPTURE_PATHS, snapshot["values"])
        with self._lock:
            while self.writer.execute(
                "SELECT 1 FROM platform_sample WHERE receive_time_us=?",
                (receive_time_us,),
            ).fetchone():
                receive_time_us += 1
            self.writer.execute(
                """
                INSERT INTO platform_sample(receive_time_us, payload)
                VALUES(?, ?)
                """,
                (receive_time_us, payload),
            )
            self.writer.commit()
        return receive_time_us, len(payload)

    def record_event(
        self,
        event_key: str,
        summary: str,
        *,
        severity: str = "info",
        source_time_us: int | None = None,
        details: dict[str, Any] | None = None,
        time_us: int | None = None,
    ) -> None:
        detail_text = json.dumps(
            details or {},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(detail_text.encode()) > 16_384:
            raise ValueError("historian event details exceed 16 KiB")
        with self._lock:
            self.writer.execute(
                """
                INSERT INTO event(
                    time_us, source_time_us, severity, event_key, summary,
                    details_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    time_us or now_us(),
                    source_time_us,
                    severity,
                    event_key,
                    summary[:512],
                    detail_text,
                ),
            )
            self.writer.commit()

    def write_rollup(self, accumulator: MinuteAccumulator) -> None:
        payload = accumulator.encode()
        with self._lock:
            self.writer.execute(
                """
                INSERT INTO minute_rollup(
                    bucket_us, sample_count, payload, updated_us
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(bucket_us) DO UPDATE SET
                    sample_count=excluded.sample_count,
                    payload=excluded.payload,
                    updated_us=excluded.updated_us
                """,
                (
                    accumulator.bucket_us,
                    accumulator.sample_count,
                    payload,
                    now_us(),
                ),
            )
            self.writer.commit()

    def load_rollup(self, bucket_us: int) -> MinuteAccumulator | None:
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT payload FROM minute_rollup WHERE bucket_us=?",
                (bucket_us,),
            ).fetchone()
        return MinuteAccumulator.decode(bucket_us, row["payload"]) if row else None

    def _raw_points(
        self,
        paths: tuple[str, ...],
        start_us: int,
        end_us: int,
    ) -> list[dict[str, Any]]:
        fast_paths = tuple(path for path in paths if path in FAST_QUERY_PATHS)
        platform_paths = tuple(path for path in paths if path in PLATFORM_QUERY_PATHS)
        points: list[dict[str, Any]] = []
        with self._connect(readonly=True) as connection:
            if fast_paths:
                rows = connection.execute(
                    """
                    SELECT receive_time_us, sequence, payload
                    FROM fast_sample
                    WHERE receive_time_us BETWEEN ? AND ?
                    ORDER BY receive_time_us
                    """,
                    (start_us, end_us),
                )
                for row in rows:
                    decoded = decode_payload(FAST_CAPTURE_PATHS, row["payload"])
                    values = {
                        path: self._public_payload(decoded[path])
                        for path in fast_paths
                    }
                    range_payload = decoded[RESISTANCE_RANGE_PATH]
                    points.append(
                        {
                            "time": row["receive_time_us"] // 1000,
                            "sequence": row["sequence"],
                            "values": values,
                            "resistanceRange": range_payload["value"],
                            "resistanceRangeStatus": range_payload["status"],
                        }
                    )
            if platform_paths:
                rows = connection.execute(
                    """
                    SELECT receive_time_us, payload
                    FROM platform_sample
                    WHERE receive_time_us BETWEEN ? AND ?
                    ORDER BY receive_time_us
                    """,
                    (start_us, end_us),
                )
                for row in rows:
                    decoded = decode_payload(
                        PLATFORM_CAPTURE_PATHS,
                        row["payload"],
                    )
                    points.append(
                        {
                            "time": row["receive_time_us"] // 1000,
                            "sequence": None,
                            "values": {
                                path: self._public_payload(decoded[path])
                                for path in platform_paths
                            },
                            "resistanceRange": None,
                            "resistanceRangeStatus": None,
                        }
                    )
        points.sort(key=lambda item: item["time"])
        return points

    @staticmethod
    def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "value": payload["value"],
            "status": payload["status"],
            "source_timestamp": iso_from_us(payload["source_time_us"]),
        }

    def _rollup_points(
        self,
        paths: tuple[str, ...],
        start_us: int,
        end_us: int,
    ) -> list[dict[str, Any]]:
        indexes = {PATH_INDEX[path]: path for path in paths}
        points = []
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                """
                SELECT bucket_us, payload
                FROM minute_rollup
                WHERE bucket_us BETWEEN ? AND ?
                ORDER BY bucket_us
                """,
                (
                    start_us // 60_000_000 * 60_000_000,
                    end_us // 60_000_000 * 60_000_000,
                ),
            )
            for row in rows:
                accumulator = MinuteAccumulator.decode(
                    row["bucket_us"],
                    row["payload"],
                )
                values: dict[str, Any] = {}
                for index, path in indexes.items():
                    item = accumulator.series.get(index)
                    if item is None:
                        values[path] = {
                            "value": None,
                            "status": "BadNoData",
                            "source_timestamp": None,
                        }
                        continue
                    count = item["count"]
                    values[path] = {
                        "value": item["total"] / count if count else None,
                        "status": item["status"],
                        "source_timestamp": iso_from_us(
                            row["bucket_us"] + 30_000_000
                        ),
                        "aggregate": {
                            "first": item["first"],
                            "last": item["last"],
                            "minimum": item["minimum"],
                            "maximum": item["maximum"],
                            "count": count,
                        },
                    }
                points.append(
                    {
                        "time": (row["bucket_us"] + 30_000_000) // 1000,
                        "sequence": None,
                        "values": values,
                        "resistanceRange": (
                            "OutOfRange"
                            if accumulator.high_z_count
                            else "InRange"
                        ),
                        "resistanceRangeStatus": accumulator.high_z_status,
                    }
                )
        return points

    def query(
        self,
        paths: Iterable[str],
        start_us: int,
        end_us: int,
        *,
        max_points: int | None = None,
        force_raw: bool = False,
    ) -> dict[str, Any]:
        selected = tuple(dict.fromkeys(paths))
        if not selected:
            raise ValueError("at least one series is required")
        unknown = set(selected) - QUERY_PATHS
        if unknown:
            raise ValueError(f"series is not retained: {sorted(unknown)[0]}")
        if end_us <= start_us:
            raise ValueError("history end must be after start")
        if (
            self.retention_enabled
            and end_us - start_us
            > self.event_retention_days * 86400 * 1_000_000
        ):
            raise ValueError("history interval exceeds retention boundary")
        limit = max_points or self.max_query_points
        if not 1 <= limit <= self.max_export_rows:
            raise ValueError("max_points is outside the allowed range")

        tables = []
        if any(path in FAST_QUERY_PATHS for path in selected):
            tables.append("fast_sample")
        if any(path in PLATFORM_QUERY_PATHS for path in selected):
            tables.append("platform_sample")
        with self._connect(readonly=True) as connection:
            raw_count = sum(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE receive_time_us BETWEEN ? AND ?
                    """,
                    (start_us, end_us),
                ).fetchone()[0]
                for table in tables
            )
            rollup_count = connection.execute(
                """
                SELECT COUNT(*) FROM minute_rollup
                WHERE bucket_us BETWEEN ? AND ?
                """,
                (
                    start_us // 60_000_000 * 60_000_000,
                    end_us // 60_000_000 * 60_000_000,
                ),
            ).fetchone()[0]
        use_rollup = not force_raw and (
            raw_count > limit or (raw_count == 0 and rollup_count > 0)
        )
        points = (
            self._rollup_points(selected, start_us, end_us)
            if use_rollup
            else self._raw_points(selected, start_us, end_us)
        )
        if len(points) > limit:
            if not use_rollup:
                raise ValueError(
                    f"query has {len(points)} points; narrow the interval or use rollups"
                )
            points = self._coarsen_rollup_points(points, selected, limit)
        resolution = "raw"
        if use_rollup:
            minutes = max(1, math.ceil(rollup_count / max(1, len(points))))
            resolution = (
                "one-minute rollup"
                if minutes == 1
                else f"{minutes}-minute rollup"
            )
        return {
            "resolution": resolution,
            "from": iso_from_us(start_us),
            "to": iso_from_us(end_us),
            "series": list(selected),
            "point_count": len(points),
            "points": points,
        }

    @staticmethod
    def _coarsen_rollup_points(
        points: list[dict[str, Any]],
        paths: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        width = max(1, math.ceil(len(points) / limit))
        result = []
        for offset in range(0, len(points), width):
            group = points[offset : offset + width]
            values: dict[str, Any] = {}
            for path in paths:
                payloads = [
                    point["values"].get(path, {})
                    for point in group
                ]
                usable = [
                    payload
                    for payload in payloads
                    if finite_number(payload.get("value")) is not None
                ]
                worst = max(
                    (
                        str(payload.get("status") or "BadNoData")
                        for payload in payloads
                    ),
                    key=status_rank,
                    default="BadNoData",
                )
                if not usable:
                    values[path] = {
                        "value": None,
                        "status": worst,
                        "source_timestamp": None,
                    }
                    continue
                weighted_total = 0.0
                count = 0
                minimum = math.inf
                maximum = -math.inf
                first = None
                last = None
                for payload in usable:
                    aggregate = payload.get("aggregate") or {}
                    item_count = max(1, int(aggregate.get("count") or 1))
                    item_value = float(payload["value"])
                    weighted_total += item_value * item_count
                    count += item_count
                    item_min = finite_number(aggregate.get("minimum"))
                    item_max = finite_number(aggregate.get("maximum"))
                    minimum = min(minimum, item_min if item_min is not None else item_value)
                    maximum = max(maximum, item_max if item_max is not None else item_value)
                    if first is None:
                        first = aggregate.get("first", item_value)
                    last = aggregate.get("last", item_value)
                values[path] = {
                    "value": weighted_total / count,
                    "status": worst,
                    "source_timestamp": iso_from_us(
                        int(group[len(group) // 2]["time"]) * 1000
                    ),
                    "aggregate": {
                        "first": first,
                        "last": last,
                        "minimum": minimum,
                        "maximum": maximum,
                        "count": count,
                    },
                }
            high_z_points = [
                point
                for point in group
                if point.get("resistanceRange") == "OutOfRange"
            ]
            result.append(
                {
                    "time": group[len(group) // 2]["time"],
                    "sequence": None,
                    "values": values,
                    "resistanceRange": (
                        "OutOfRange" if high_z_points else "InRange"
                    ),
                    "resistanceRangeStatus": max(
                        (
                            str(
                                point.get("resistanceRangeStatus")
                                or "BadNoData"
                            )
                            for point in group
                        ),
                        key=status_rank,
                        default="BadNoData",
                    ),
                }
            )
        return result

    def query_events(
        self,
        start_us: int,
        end_us: int,
        *,
        limit: int = 1000,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 5000:
            raise ValueError("event limit is outside the allowed range")
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                """
                SELECT time_us, source_time_us, severity, event_key, summary,
                       details_json
                FROM event
                WHERE time_us BETWEEN ? AND ?
                ORDER BY time_us
                LIMIT ?
                """,
                (start_us, end_us, limit),
            ).fetchall()
        return {
            "events": [
                {
                    "time": iso_from_us(row["time_us"]),
                    "source_time": iso_from_us(row["source_time_us"]),
                    "severity": row["severity"],
                    "key": row["event_key"],
                    "summary": row["summary"],
                    "details": json.loads(row["details_json"]),
                }
                for row in rows
            ]
        }

    def export_csv(
        self,
        paths: Iterable[str],
        start_us: int,
        end_us: int,
    ) -> bytes:
        result = self.query(
            paths,
            start_us,
            end_us,
            max_points=self.max_export_rows,
        )
        selected = tuple(result["series"])
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        headers = ["timestamp_utc", "resolution"]
        for path in selected:
            headers.extend((path, f"{path}.StatusCode"))
        if "Measurement.ResistanceOhm" in selected:
            headers.extend(
                (
                    "Measurement.ResistanceRange",
                    "Measurement.ResistanceRange.StatusCode",
                )
            )
        writer.writerow(headers)
        for point in result["points"]:
            row: list[Any] = [
                iso_from_us(int(point["time"]) * 1000),
                result["resolution"],
            ]
            for path in selected:
                payload = point["values"].get(path, {})
                row.extend((payload.get("value"), payload.get("status")))
            if "Measurement.ResistanceOhm" in selected:
                row.extend(
                    (
                        point.get("resistanceRange"),
                        point.get("resistanceRangeStatus"),
                    )
                )
            writer.writerow(row)
        return output.getvalue().encode()

    def apply_retention(self, reference_us: int | None = None) -> dict[str, int]:
        tables = (
            "fast_sample",
            "platform_sample",
            "minute_rollup",
            "event",
        )
        if not self.retention_enabled:
            return {table: 0 for table in tables}
        reference = reference_us or now_us()
        cutoffs = {
            "fast_sample": reference
            - self.raw_retention_days * 86400 * 1_000_000,
            "platform_sample": reference
            - self.platform_retention_days * 86400 * 1_000_000,
            "minute_rollup": reference
            - self.rollup_retention_days * 86400 * 1_000_000,
            "event": reference
            - self.event_retention_days * 86400 * 1_000_000,
        }
        removed: dict[str, int] = {}
        with self._lock:
            for table, cutoff in cutoffs.items():
                column = "bucket_us" if table == "minute_rollup" else (
                    "time_us" if table == "event" else "receive_time_us"
                )
                cursor = self.writer.execute(
                    f"DELETE FROM {table} WHERE {column} < ?",
                    (cutoff,),
                )
                removed[table] = max(0, cursor.rowcount)
            self.writer.execute("PRAGMA incremental_vacuum(128)")
            self.writer.commit()
        return removed

    def checkpoint(self) -> None:
        with self._lock:
            self.writer.execute("PRAGMA wal_checkpoint(PASSIVE)")

    @staticmethod
    def _replication_position(
        table: str,
        row: sqlite3.Row | dict[str, Any],
    ) -> tuple[int, int]:
        if table == "minute_rollup":
            return int(row["updated_us"]), int(row["bucket_us"])
        key = "id" if table == "event" else "receive_time_us"
        return int(row[key]), 0

    def replication_batch(
        self,
        table: str,
        after_key: int,
        after_tie: int,
        *,
        limit: int,
    ) -> dict[str, Any]:
        if table not in REPLICATION_TABLES:
            raise ValueError("replication table is not allowed")
        if after_key < 0 or after_tie < 0:
            raise ValueError("replication cursor cannot be negative")
        if not 1 <= limit <= MAX_REPLICA_BATCH_SIZE:
            raise ValueError("replication batch size is outside the allowed range")

        with self._connect(readonly=True) as connection:
            if table == "fast_sample":
                rows = connection.execute(
                    """
                    SELECT receive_time_us, stream_id, sequence, payload
                    FROM fast_sample
                    WHERE receive_time_us > ?
                    ORDER BY receive_time_us
                    LIMIT ?
                    """,
                    (after_key, limit + 1),
                ).fetchall()
            elif table == "platform_sample":
                rows = connection.execute(
                    """
                    SELECT receive_time_us, payload
                    FROM platform_sample
                    WHERE receive_time_us > ?
                    ORDER BY receive_time_us
                    LIMIT ?
                    """,
                    (after_key, limit + 1),
                ).fetchall()
            elif table == "minute_rollup":
                rows = connection.execute(
                    """
                    SELECT bucket_us, sample_count, payload, updated_us
                    FROM minute_rollup
                    WHERE updated_us > ?
                       OR (updated_us = ? AND bucket_us > ?)
                    ORDER BY updated_us, bucket_us
                    LIMIT ?
                    """,
                    (after_key, after_key, after_tie, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, time_us, source_time_us, severity, event_key,
                           summary, details_json
                    FROM event
                    WHERE id > ?
                    ORDER BY id
                    LIMIT ?
                    """,
                    (after_key, limit + 1),
                ).fetchall()

            has_more = len(rows) > limit
            rows = rows[:limit]
            streams: list[dict[str, Any]] = []
            if table == "fast_sample" and rows:
                stream_ids = tuple(
                    sorted({int(row["stream_id"]) for row in rows})
                )
                placeholders = ",".join("?" for _ in stream_ids)
                stream_rows = connection.execute(
                    f"""
                    SELECT id, boot_id, started_us, ended_us, first_sequence,
                           last_sequence, reason
                    FROM stream
                    WHERE id IN ({placeholders})
                    ORDER BY id
                    """,
                    stream_ids,
                ).fetchall()
                streams = [dict(row) for row in stream_rows]

        encoded_rows = []
        for row in rows:
            item = dict(row)
            if "payload" in item:
                item["payload"] = base64.b64encode(item["payload"]).decode("ascii")
            encoded_rows.append(item)
        cursor_key, cursor_tie = (
            self._replication_position(table, rows[-1])
            if rows
            else (after_key, after_tie)
        )
        return {
            "replication_version": 1,
            "schema_version": SCHEMA_VERSION,
            "source": local_source_identity(),
            "table": table,
            "after": {"key": after_key, "tie": after_tie},
            "cursor": {"key": cursor_key, "tie": cursor_tie},
            "has_more": has_more,
            "streams": streams,
            "rows": encoded_rows,
        }

    def replication_cursor(
        self,
        source_id: str,
        table: str,
    ) -> tuple[int, int]:
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                """
                SELECT remote_key, remote_tie
                FROM replication_cursor
                WHERE source_id=? AND table_name=?
                """,
                (source_id, table),
            ).fetchone()
        return (
            (int(row["remote_key"]), int(row["remote_tie"]))
            if row is not None
            else (0, 0)
        )

    @staticmethod
    def _decode_replica_payload(value: Any) -> bytes:
        try:
            return base64.b64decode(str(value), validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("replication payload is not valid base64") from error

    def import_replication_batch(
        self,
        batch: dict[str, Any],
        *,
        expected_source_id: str = "",
    ) -> dict[str, Any]:
        if int(batch.get("replication_version", 0)) != 1:
            raise ValueError("unsupported replication protocol version")
        if int(batch.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValueError("replication historian schema does not match")
        table = str(batch.get("table") or "")
        if table not in REPLICATION_TABLES:
            raise ValueError("replication table is not allowed")
        source = batch.get("source")
        if not isinstance(source, dict):
            raise ValueError("replication source identity is missing")
        source_id = str(source.get("id") or "").strip()
        hostname = str(source.get("hostname") or "").strip()
        if not source_id or not hostname:
            raise ValueError("replication source identity is incomplete")
        if expected_source_id and source_id != expected_source_id:
            raise ValueError("replication source does not match configured identity")
        rows = batch.get("rows")
        streams = batch.get("streams", [])
        cursor = batch.get("cursor")
        if not isinstance(rows, list) or not isinstance(streams, list):
            raise ValueError("replication batch rows are invalid")
        if not isinstance(cursor, dict):
            raise ValueError("replication cursor is missing")
        cursor_position = (int(cursor.get("key", -1)), int(cursor.get("tie", -1)))
        if cursor_position[0] < 0 or cursor_position[1] < 0:
            raise ValueError("replication cursor is invalid")

        imported = 0
        skipped = 0
        stamp = now_us()
        with self._lock:
            connection = self.writer
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO replication_source(
                        source_id, hostname, schema_version, first_seen_us,
                        last_seen_us
                    ) VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        hostname=excluded.hostname,
                        schema_version=excluded.schema_version,
                        last_seen_us=excluded.last_seen_us
                    """,
                    (source_id, hostname, SCHEMA_VERSION, stamp, stamp),
                )
                current_row = connection.execute(
                    """
                    SELECT remote_key, remote_tie
                    FROM replication_cursor
                    WHERE source_id=? AND table_name=?
                    """,
                    (source_id, table),
                ).fetchone()
                current = (
                    (
                        int(current_row["remote_key"]),
                        int(current_row["remote_tie"]),
                    )
                    if current_row is not None
                    else (0, 0)
                )

                stream_map: dict[int, int] = {}
                if table == "fast_sample":
                    for stream in streams:
                        remote_id = int(stream["id"])
                        mapped = connection.execute(
                            """
                            SELECT local_stream_id
                            FROM replication_stream
                            WHERE source_id=? AND remote_stream_id=?
                            """,
                            (source_id, remote_id),
                        ).fetchone()
                        if mapped is None:
                            inserted = connection.execute(
                                """
                                INSERT INTO stream(
                                    boot_id, started_us, ended_us,
                                    first_sequence, last_sequence, reason
                                ) VALUES(?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    str(stream["boot_id"]),
                                    int(stream["started_us"]),
                                    (
                                        int(stream["ended_us"])
                                        if stream.get("ended_us") is not None
                                        else None
                                    ),
                                    stream.get("first_sequence"),
                                    stream.get("last_sequence"),
                                    f"replica:{stream['reason']}",
                                ),
                            )
                            local_id = int(inserted.lastrowid)
                            connection.execute(
                                """
                                INSERT INTO replication_stream(
                                    source_id, remote_stream_id, local_stream_id
                                ) VALUES(?, ?, ?)
                                """,
                                (source_id, remote_id, local_id),
                            )
                        else:
                            local_id = int(mapped["local_stream_id"])
                            connection.execute(
                                """
                                UPDATE stream SET
                                    boot_id=?, started_us=?, ended_us=?,
                                    first_sequence=?, last_sequence=?,
                                    reason=?
                                WHERE id=?
                                """,
                                (
                                    str(stream["boot_id"]),
                                    int(stream["started_us"]),
                                    (
                                        int(stream["ended_us"])
                                        if stream.get("ended_us") is not None
                                        else None
                                    ),
                                    stream.get("first_sequence"),
                                    stream.get("last_sequence"),
                                    f"replica:{stream['reason']}",
                                    local_id,
                                ),
                            )
                        stream_map[remote_id] = local_id

                last_position = current
                previous_position = (-1, -1)
                for item in rows:
                    if not isinstance(item, dict):
                        raise ValueError("replication row is invalid")
                    position = self._replication_position(table, item)
                    if position <= previous_position:
                        raise ValueError("replication rows are not strictly ordered")
                    previous_position = position
                    if position <= current:
                        skipped += 1
                        continue
                    last_position = position

                    if table == "fast_sample":
                        payload = self._decode_replica_payload(item.get("payload"))
                        decode_payload(FAST_CAPTURE_PATHS, payload)
                        remote_stream_id = int(item["stream_id"])
                        local_stream_id = stream_map.get(remote_stream_id)
                        if local_stream_id is None:
                            mapped = connection.execute(
                                """
                                SELECT local_stream_id
                                FROM replication_stream
                                WHERE source_id=? AND remote_stream_id=?
                                """,
                                (source_id, remote_stream_id),
                            ).fetchone()
                            if mapped is None:
                                raise ValueError(
                                    "replication batch references an unknown stream"
                                )
                            local_stream_id = int(mapped["local_stream_id"])
                        existing = connection.execute(
                            """
                            SELECT sequence, payload FROM fast_sample
                            WHERE receive_time_us=?
                            """,
                            (position[0],),
                        ).fetchone()
                        sequence = item.get("sequence")
                        if existing is None:
                            connection.execute(
                                """
                                INSERT INTO fast_sample(
                                    receive_time_us, stream_id, sequence, payload
                                ) VALUES(?, ?, ?, ?)
                                """,
                                (position[0], local_stream_id, sequence, payload),
                            )
                            imported += 1
                        elif (
                            existing["sequence"] == sequence
                            and existing["payload"] == payload
                        ):
                            skipped += 1
                        else:
                            raise ValueError(
                                "replication fast-sample timestamp collision"
                            )
                    elif table == "platform_sample":
                        payload = self._decode_replica_payload(item.get("payload"))
                        decode_payload(PLATFORM_CAPTURE_PATHS, payload)
                        existing = connection.execute(
                            """
                            SELECT payload FROM platform_sample
                            WHERE receive_time_us=?
                            """,
                            (position[0],),
                        ).fetchone()
                        if existing is None:
                            connection.execute(
                                """
                                INSERT INTO platform_sample(receive_time_us, payload)
                                VALUES(?, ?)
                                """,
                                (position[0], payload),
                            )
                            imported += 1
                        elif existing["payload"] == payload:
                            skipped += 1
                        else:
                            raise ValueError(
                                "replication platform-sample timestamp collision"
                            )
                    elif table == "minute_rollup":
                        payload = self._decode_replica_payload(item.get("payload"))
                        bucket_us = int(item["bucket_us"])
                        MinuteAccumulator.decode(bucket_us, payload)
                        connection.execute(
                            """
                            INSERT INTO minute_rollup(
                                bucket_us, sample_count, payload, updated_us
                            ) VALUES(?, ?, ?, ?)
                            ON CONFLICT(bucket_us) DO UPDATE SET
                                sample_count=excluded.sample_count,
                                payload=excluded.payload,
                                updated_us=excluded.updated_us
                            """,
                            (
                                bucket_us,
                                int(item["sample_count"]),
                                payload,
                                int(item["updated_us"]),
                            ),
                        )
                        imported += 1
                    else:
                        details_text = str(item["details_json"])
                        details = json.loads(details_text)
                        if not isinstance(details, dict):
                            raise ValueError("replication event details are invalid")
                        connection.execute(
                            """
                            INSERT INTO event(
                                time_us, source_time_us, severity, event_key,
                                summary, details_json
                            ) VALUES(?, ?, ?, ?, ?, ?)
                            """,
                            (
                                int(item["time_us"]),
                                (
                                    int(item["source_time_us"])
                                    if item.get("source_time_us") is not None
                                    else None
                                ),
                                str(item["severity"]),
                                str(item["event_key"]),
                                str(item["summary"]),
                                details_text,
                            ),
                        )
                        imported += 1

                if rows and cursor_position != previous_position:
                    raise ValueError("replication cursor does not match final row")
                new_cursor = max(current, cursor_position)
                connection.execute(
                    """
                    INSERT INTO replication_cursor(
                        source_id, table_name, remote_key, remote_tie, updated_us
                    ) VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, table_name) DO UPDATE SET
                        remote_key=excluded.remote_key,
                        remote_tie=excluded.remote_tie,
                        updated_us=excluded.updated_us
                    """,
                    (
                        source_id,
                        table,
                        new_cursor[0],
                        new_cursor[1],
                        stamp,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "source_id": source_id,
            "hostname": hostname,
            "table": table,
            "imported": imported,
            "skipped": skipped,
            "cursor": {"key": new_cursor[0], "tie": new_cursor[1]},
        }

    def storage_status(self) -> dict[str, Any]:
        with self._connect(readonly=True) as connection:
            fast = connection.execute(
                """
                SELECT COUNT(*) AS count, MIN(receive_time_us) AS oldest,
                       MAX(receive_time_us) AS newest
                FROM fast_sample INDEXED BY fast_sample_status_scan
                """
            ).fetchone()
            fast_average = connection.execute(
                """
                SELECT AVG(LENGTH(payload)) AS average, COUNT(*) AS sampled
                FROM (
                    SELECT payload FROM fast_sample
                    ORDER BY receive_time_us DESC LIMIT ?
                )
                """,
                (STATUS_AVERAGE_SAMPLE_ROWS,),
            ).fetchone()
            platform = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM platform_sample INDEXED BY platform_sample_status_scan
                """
            ).fetchone()
            platform_average = connection.execute(
                """
                SELECT AVG(LENGTH(payload)) AS average, COUNT(*) AS sampled
                FROM (
                    SELECT payload FROM platform_sample
                    ORDER BY receive_time_us DESC LIMIT ?
                )
                """,
                (STATUS_AVERAGE_SAMPLE_ROWS,),
            ).fetchone()
            rollup = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM minute_rollup INDEXED BY minute_rollup_status_scan
                """
            ).fetchone()
            rollup_average = connection.execute(
                """
                SELECT AVG(LENGTH(payload)) AS average, COUNT(*) AS sampled
                FROM (
                    SELECT payload FROM minute_rollup
                    ORDER BY bucket_us DESC LIMIT ?
                )
                """,
                (STATUS_AVERAGE_SAMPLE_ROWS,),
            ).fetchone()
            event_count = connection.execute(
                "SELECT COUNT(*) FROM event"
            ).fetchone()[0]
            replicas = connection.execute(
                """
                SELECT s.source_id, s.hostname, s.last_seen_us, c.table_name,
                       c.remote_key, c.remote_tie, c.updated_us
                FROM replication_source AS s
                LEFT JOIN replication_cursor AS c
                  ON c.source_id = s.source_id
                ORDER BY s.source_id, c.table_name
                """
            ).fetchall()
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            page_count = connection.execute("PRAGMA page_count").fetchone()[0]
            freelist = connection.execute("PRAGMA freelist_count").fetchone()[0]
        sizes = {}
        for suffix, label in (("", "database"), ("-wal", "wal"), ("-shm", "shm")):
            path = Path(f"{self.database}{suffix}")
            sizes[label] = path.stat().st_size if path.exists() else 0
        total_bytes = sum(sizes.values())
        active_bytes = max(0, (page_count - freelist) * page_size) + sizes["wal"]
        estimate = self.estimate_monthly_bytes(
            fast_payload_bytes=fast_average["average"] or 0,
            platform_payload_bytes=platform_average["average"] or 0,
            rollup_payload_bytes=rollup_average["average"] or 0,
        )
        return {
            "database": str(self.database),
            "schema_version": SCHEMA_VERSION,
            "bytes": {**sizes, "total": total_bytes, "active": active_bytes},
            "fast_samples": {
                "count": fast["count"],
                "oldest": iso_from_us(fast["oldest"]),
                "newest": iso_from_us(fast["newest"]),
                "average_payload_bytes": fast_average["average"],
                "average_payload_sample_rows": fast_average["sampled"],
            },
            "platform_samples": {
                "count": platform["count"],
                "average_payload_bytes": platform_average["average"],
                "average_payload_sample_rows": platform_average["sampled"],
            },
            "minute_rollups": {
                "count": rollup["count"],
                "average_payload_bytes": rollup_average["average"],
                "average_payload_sample_rows": rollup_average["sampled"],
            },
            "events": {"count": event_count},
            "replication": [
                {
                    "source_id": row["source_id"],
                    "hostname": row["hostname"],
                    "last_seen": iso_from_us(row["last_seen_us"]),
                    "table": row["table_name"],
                    "remote_key": row["remote_key"],
                    "remote_tie": row["remote_tie"],
                    "updated": iso_from_us(row["updated_us"]),
                }
                for row in replicas
            ],
            "retention_days": {
                "raw": self.raw_retention_days,
                "platform": self.platform_retention_days,
                "rollup": self.rollup_retention_days,
                "event": self.event_retention_days,
            },
            "retention_enabled": self.retention_enabled,
            "projected_month_bytes_from_payloads": estimate,
        }

    @staticmethod
    def estimate_monthly_bytes(
        *,
        fast_payload_bytes: float,
        platform_payload_bytes: float,
        rollup_payload_bytes: float,
    ) -> int:
        # SQLite record/index overhead is intentionally conservative. The
        # deployment report also includes a measured synthetic database result.
        fast_rows = MONTH_SECONDS / DEFAULT_CAPTURE_INTERVAL
        platform_rows = MONTH_SECONDS / DEFAULT_PLATFORM_INTERVAL
        rollup_rows = MONTH_SECONDS / 60
        return int(
            fast_rows * (fast_payload_bytes + 48)
            + platform_rows * (platform_payload_bytes + 32)
            + rollup_rows * (rollup_payload_bytes + 32)
        )


class HistorianRecorder:
    def __init__(
        self,
        store: HistorianStore,
        monitor: OpcUaMonitor,
        *,
        capture_interval: float,
        platform_interval: float,
    ) -> None:
        self.store = store
        self.monitor = monitor
        self.capture_interval = capture_interval
        self.platform_interval = platform_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._connected = False
        self._last_error = ""
        self._last_sample_at: str | None = None
        self._last_platform_at: str | None = None
        self._dropped_samples = 0
        self._stream_id: int | None = None
        self._boot_id = ""
        self._last_sequence: int | None = None
        self._last_platform_monotonic = 0.0
        self._last_retention_monotonic = 0.0
        self._last_checkpoint_monotonic = 0.0
        self._last_rollup_flush_monotonic = 0.0
        self._last_disk_check_monotonic = 0.0
        self._disk_free_bytes = 0
        self._write_limited = False
        self._rollup: MinuteAccumulator | None = None
        self._event_state: dict[str, tuple[Any, str]] = {}
        self._connection_state: bool | None = None

    def start(self) -> None:
        self.monitor.start()
        self._thread = threading.Thread(
            target=self._run,
            name="gizmo-historian-recorder",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._rollup is not None:
            self.store.write_rollup(self._rollup)
        if self._stream_id is not None:
            self.store.end_stream(self._stream_id)
        self.monitor.stop()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "opcua_connected": self._connected,
                "last_sample_at": self._last_sample_at,
                "last_platform_at": self._last_platform_at,
                "dropped_samples": self._dropped_samples,
                "last_error": self._last_error,
                "stream_id": self._stream_id,
                "disk_free_bytes": self._disk_free_bytes,
                "write_limited": self._write_limited,
            }

    def _set_error(self, error: Exception) -> None:
        with self._lock:
            self._last_error = f"{type(error).__name__}: {error}"

    def _event_for_connection(self, connected: bool, snapshot: dict[str, Any]) -> None:
        if self._connection_state is connected:
            return
        self._connection_state = connected
        connection = snapshot.get("connection", {})
        self.store.record_event(
            "opcua.connection",
            "OPC UA connection restored" if connected else "OPC UA connection lost",
            severity="info" if connected else "warning",
            details={
                "connected": connected,
                "endpoint": connection.get("endpoint"),
                "error": connection.get("error"),
            },
        )

    def _ensure_stream(self, snapshot: dict[str, Any]) -> int:
        values = snapshot["values"]
        boot_id = str(values.get("Identity.BootId", {}).get("value") or "unknown")
        sequence_value = values.get("Measurement.Sequence", {}).get("value")
        sequence = (
            int(sequence_value)
            if isinstance(sequence_value, (int, float))
            else None
        )
        reason = ""
        if self._stream_id is None:
            reason = "historian-start"
        elif boot_id != self._boot_id:
            reason = "board-boot"
        elif (
            sequence is not None
            and self._last_sequence is not None
            and sequence < self._last_sequence
        ):
            reason = "measurement-sequence-reset"
        if reason:
            if self._stream_id is not None:
                self.store.end_stream(self._stream_id)
            self._stream_id = self.store.begin_stream(boot_id, sequence, reason)
            self.store.record_event(
                "history.stream",
                f"Historian sequence epoch started: {reason}",
                details={
                    "stream_id": self._stream_id,
                    "boot_id": boot_id,
                    "sequence": sequence,
                },
            )
        self._boot_id = boot_id
        self._last_sequence = sequence
        assert self._stream_id is not None
        return self._stream_id

    def _record_transitions(self, snapshot: dict[str, Any]) -> None:
        values = snapshot["values"]
        for path in EVENT_PATHS:
            payload = values.get(path, {})
            current = (payload.get("value"), str(payload.get("status") or "Unknown"))
            previous = self._event_state.get(path)
            self._event_state[path] = current
            if previous is None or previous == current:
                continue
            severity = "warning" if status_rank(current[1]) >= 2 else "info"
            if path == "Alarm.Active" and current[0]:
                severity = "alarm"
            self.store.record_event(
                path,
                f"{path} changed from {previous[0]!r} to {current[0]!r}",
                severity=severity,
                source_time_us=parse_time_us(payload.get("source_timestamp")),
                details={
                    "previous_value": previous[0],
                    "value": current[0],
                    "previous_status": previous[1],
                    "status": current[1],
                },
            )

    def _update_rollup(self, snapshot: dict[str, Any], receive_time_us: int) -> None:
        bucket = receive_time_us // 60_000_000 * 60_000_000
        if self._rollup is None or self._rollup.bucket_us != bucket:
            if self._rollup is not None:
                self.store.write_rollup(self._rollup)
            existing = self.store.load_rollup(bucket)
            self._rollup = existing or MinuteAccumulator(bucket)
        self._rollup.update(snapshot)
        monotonic = time.monotonic()
        if monotonic - self._last_rollup_flush_monotonic >= 10:
            self.store.write_rollup(self._rollup)
            self._last_rollup_flush_monotonic = monotonic

    def _check_disk(self) -> bool:
        stat = os.statvfs(self.store.database.parent)
        free_bytes = stat.f_bavail * stat.f_frsize
        total_bytes = stat.f_blocks * stat.f_frsize
        boundary = max(self.store.min_free_bytes, int(total_bytes * 0.15))
        limited = free_bytes < boundary
        with self._lock:
            previous = self._write_limited
            self._disk_free_bytes = free_bytes
            self._write_limited = limited
        if limited != previous:
            self.store.record_event(
                "history.disk",
                (
                    "Historian writes stopped at the free-space boundary"
                    if limited
                    else "Historian writes resumed after free space recovered"
                ),
                severity="warning" if limited else "info",
                details={
                    "free_bytes": free_bytes,
                    "required_free_bytes": boundary,
                },
            )
        return not limited

    def _run(self) -> None:
        while not self._stop.wait(self.capture_interval):
            try:
                snapshot = self.monitor.snapshot()
                connected = bool(snapshot.get("connection", {}).get("connected"))
                self._event_for_connection(connected, snapshot)
                with self._lock:
                    self._connected = connected
                if not connected:
                    continue
                monotonic = time.monotonic()
                if monotonic - self._last_disk_check_monotonic >= 60:
                    self._last_disk_check_monotonic = monotonic
                    if not self._check_disk():
                        with self._lock:
                            self._dropped_samples += 1
                        continue
                elif self._write_limited:
                    with self._lock:
                        self._dropped_samples += 1
                    continue
                stream_id = self._ensure_stream(snapshot)
                receive_time_us, _ = self.store.record_fast(snapshot, stream_id)
                self._update_rollup(snapshot, receive_time_us)
                self._record_transitions(snapshot)
                stamp = iso_from_us(receive_time_us)
                with self._lock:
                    self._last_sample_at = stamp
                    self._last_error = ""

                monotonic = time.monotonic()
                if (
                    monotonic - self._last_platform_monotonic
                    >= self.platform_interval
                ):
                    platform_time_us, _ = self.store.record_platform(snapshot)
                    self._last_platform_monotonic = monotonic
                    with self._lock:
                        self._last_platform_at = iso_from_us(platform_time_us)
                if monotonic - self._last_checkpoint_monotonic >= 300:
                    self.store.checkpoint()
                    self._last_checkpoint_monotonic = monotonic
                if monotonic - self._last_retention_monotonic >= 3600:
                    removed = self.store.apply_retention()
                    if any(removed.values()):
                        self.store.record_event(
                            "history.retention",
                            "Historian retention removed expired rows",
                            details=removed,
                        )
                    self._last_retention_monotonic = monotonic
            except Exception as error:
                self._set_error(error)
                with self._lock:
                    self._dropped_samples += 1


class ReplicaRecorder:
    """Incrementally mirror an edge historian through its read-only API."""

    def __init__(
        self,
        store: HistorianStore,
        replica_url: str,
        *,
        poll_seconds: float,
        batch_size: int,
        expected_source_id: str = "",
    ) -> None:
        self.store = store
        self.replica_url = replica_url
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self.expected_source_id = expected_source_id.strip()
        self._source_id = self.expected_source_id
        self._source_hostname = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._connected = False
        self._last_error = ""
        self._last_sample_at: str | None = None
        self._last_platform_at: str | None = None
        self._sync_errors = 0
        self._rows_imported = 0
        self._disk_free_bytes = 0
        self._write_limited = False
        self._last_retention_monotonic = 0.0
        self._last_checkpoint_monotonic = 0.0
        self._last_connection_state: bool | None = None
        with self.store._connect(readonly=True) as connection:
            sources = connection.execute(
                """
                SELECT source_id, hostname
                FROM replication_source
                ORDER BY first_seen_us
                """
            ).fetchall()
        if not self._source_id and len(sources) == 1:
            self._source_id = str(sources[0]["source_id"])
            self._source_hostname = str(sources[0]["hostname"])
        elif self._source_id:
            match = next(
                (
                    row
                    for row in sources
                    if str(row["source_id"]) == self._source_id
                ),
                None,
            )
            if match is not None:
                self._source_hostname = str(match["hostname"])

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="gizmo-historian-replica",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)

    def status(self) -> dict[str, Any]:
        with self._lock:
            lag_seconds = None
            sample_us = parse_time_us(self._last_sample_at)
            if sample_us is not None:
                lag_seconds = max(0.0, (now_us() - sample_us) / 1_000_000)
            return {
                # Compatibility with the existing status/dashboard contract:
                # this means the upstream edge historian is connected.
                "opcua_connected": self._connected,
                "replica_connected": self._connected,
                "mode": "replica",
                "source_id": self._source_id,
                "source_hostname": self._source_hostname,
                "last_sample_at": self._last_sample_at,
                "last_platform_at": self._last_platform_at,
                "lag_seconds": lag_seconds,
                "dropped_samples": 0,
                "sync_errors": self._sync_errors,
                "rows_imported": self._rows_imported,
                "last_error": self._last_error,
                "stream_id": None,
                "disk_free_bytes": self._disk_free_bytes,
                "write_limited": self._write_limited,
            }

    def _set_connection(self, connected: bool, error: str = "") -> None:
        with self._lock:
            previous = self._last_connection_state
            self._last_connection_state = connected
            self._connected = connected
            self._last_error = error
        if previous is connected:
            return
        try:
            self.store.record_event(
                "replication.connection",
                (
                    "Edge historian replication restored"
                    if connected
                    else "Edge historian replication unavailable"
                ),
                severity="info" if connected else "warning",
                details={
                    "connected": connected,
                    "url": self.replica_url,
                    "error": error,
                },
            )
        except (sqlite3.Error, ValueError):
            pass

    def _check_disk(self) -> bool:
        stat = os.statvfs(self.store.database.parent)
        free_bytes = stat.f_bavail * stat.f_frsize
        total_bytes = stat.f_blocks * stat.f_frsize
        boundary = max(self.store.min_free_bytes, int(total_bytes * 0.15))
        limited = free_bytes < boundary
        with self._lock:
            self._disk_free_bytes = free_bytes
            self._write_limited = limited
        return not limited

    def _cursor(self, table: str) -> tuple[int, int]:
        if not self._source_id:
            return 0, 0
        return self.store.replication_cursor(self._source_id, table)

    def _fetch(
        self,
        table: str,
        cursor: tuple[int, int],
    ) -> dict[str, Any]:
        query = urlencode(
            {
                "table": table,
                "after": cursor[0],
                "after_tie": cursor[1],
                "limit": self.batch_size,
            }
        )
        separator = "&" if "?" in self.replica_url else "?"
        request = urllib.request.Request(
            f"{self.replica_url}{separator}{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "GIZMoHistorianReplica/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            length = response.getheader("Content-Length")
            if length is not None and int(length) > MAX_REPLICA_RESPONSE_BYTES:
                raise ValueError("replication response exceeds size limit")
            body = response.read(MAX_REPLICA_RESPONSE_BYTES + 1)
        if len(body) > MAX_REPLICA_RESPONSE_BYTES:
            raise ValueError("replication response exceeds size limit")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("replication response is not an object")
        return payload

    def _sync_table(self, table: str) -> None:
        while not self._stop.is_set():
            cursor = self._cursor(table)
            batch = self._fetch(table, cursor)
            source = batch.get("source") or {}
            source_id = str(source.get("id") or "")
            if self._source_id and source_id != self._source_id:
                raise ValueError("edge historian identity changed")
            expected = self.expected_source_id or self._source_id
            result = self.store.import_replication_batch(
                batch,
                expected_source_id=expected,
            )
            self._source_id = str(result["source_id"])
            self._source_hostname = str(result["hostname"])
            imported = int(result["imported"])
            with self._lock:
                self._rows_imported += imported
                if table == "fast_sample":
                    self._last_sample_at = iso_from_us(
                        int(result["cursor"]["key"])
                    )
                elif table == "platform_sample":
                    self._last_platform_at = iso_from_us(
                        int(result["cursor"]["key"])
                    )
            self._set_connection(True)
            if not batch.get("has_more"):
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._check_disk():
                    raise RuntimeError(
                        "replica writes stopped at the free-space boundary"
                    )
                for table in REPLICATION_TABLES:
                    self._sync_table(table)
                    if self._stop.is_set():
                        return
                monotonic = time.monotonic()
                if monotonic - self._last_checkpoint_monotonic >= 300:
                    self.store.checkpoint()
                    self._last_checkpoint_monotonic = monotonic
                if monotonic - self._last_retention_monotonic >= 3600:
                    self.store.apply_retention()
                    self._last_retention_monotonic = monotonic
            except (
                OSError,
                TimeoutError,
                urllib.error.URLError,
                ValueError,
                RuntimeError,
                sqlite3.Error,
            ) as error:
                message = f"{type(error).__name__}: {error}"
                with self._lock:
                    self._sync_errors += 1
                self._set_connection(False, message)
            self._stop.wait(self.poll_seconds)


class ThreadingUnixHTTPServer(
    socketserver.ThreadingMixIn,
    socketserver.UnixStreamServer,
):
    daemon_threads = True

    def __init__(
        self,
        address: str,
        handler: type[BaseHTTPRequestHandler],
        store: HistorianStore,
        recorder: HistorianRecorder | ReplicaRecorder,
    ) -> None:
        super().__init__(address, handler)
        self.store = store
        self.recorder = recorder


class HistorianHandler(BaseHTTPRequestHandler):
    server_version = "GIZMoHistorian/1.0"
    sys_version = ""

    @property
    def historian(self) -> ThreadingUnixHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            json.dumps(
                {
                    "time": utc_now(),
                    "client": "local-unix-socket",
                    "request": fmt % args,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )

    def _send(
        self,
        body: bytes,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        content_type: str = "application/json; charset=utf-8",
        disposition: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(
        self,
        payload: Any,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self._send(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode(),
            status=status,
        )

    @staticmethod
    def _range(query: dict[str, list[str]]) -> tuple[int, int]:
        try:
            start = parse_time_us(query["from"][0])
            end = parse_time_us(query["to"][0])
        except (KeyError, ValueError) as error:
            raise ValueError("from and to timestamps are required") from error
        if start is None or end is None:
            raise ValueError("from and to timestamps are invalid")
        return start, end

    def do_GET(self) -> None:  # noqa: N802
        split = urlsplit(self.path)
        query = parse_qs(split.query, keep_blank_values=False)
        try:
            if split.path in {"/healthz", "/status"}:
                storage = self.historian.store.storage_status()
                recorder = self.historian.recorder.status()
                self._json(
                    {
                        "status": (
                            "ok"
                            if recorder["opcua_connected"]
                            and not recorder["last_error"]
                            else "degraded"
                        ),
                        "generated_at": utc_now(),
                        "recorder": recorder,
                        "storage": storage,
                    }
                )
            elif split.path == "/series":
                self._json(
                    {
                        "namespace_uri": NAMESPACE_URI,
                        "series": [
                            {**CATALOG[path].public(), "retained": True}
                            for path in ROLLUP_PATHS
                        ],
                    }
                )
            elif split.path == "/replication":
                table = str(query.get("table", [""])[0])
                after_key = int(query.get("after", ["0"])[0])
                after_tie = int(query.get("after_tie", ["0"])[0])
                limit = int(
                    query.get(
                        "limit",
                        [str(DEFAULT_REPLICA_BATCH_SIZE)],
                    )[0]
                )
                self._json(
                    self.historian.store.replication_batch(
                        table,
                        after_key,
                        after_tie,
                        limit=limit,
                    )
                )
            elif split.path == "/query":
                start, end = self._range(query)
                paths = tuple(
                    path
                    for item in query.get("series", [])
                    for path in item.split(",")
                    if path
                )
                max_points = int(
                    query.get(
                        "max_points",
                        [str(self.historian.store.max_query_points)],
                    )[0]
                )
                self._json(
                    self.historian.store.query(
                        paths,
                        start,
                        end,
                        max_points=max_points,
                    )
                )
            elif split.path == "/events":
                start, end = self._range(query)
                limit = int(query.get("limit", ["1000"])[0])
                self._json(
                    self.historian.store.query_events(
                        start,
                        end,
                        limit=limit,
                    )
                )
            elif split.path == "/export.csv":
                start, end = self._range(query)
                paths = tuple(
                    path
                    for item in query.get("series", [])
                    for path in item.split(",")
                    if path
                )
                body = self.historian.store.export_csv(paths, start, end)
                self._send(
                    body,
                    content_type="text/csv; charset=utf-8",
                    disposition='attachment; filename="gizmo-history.csv"',
                )
            else:
                self._json(
                    {"error": "history route not found"},
                    HTTPStatus.NOT_FOUND,
                )
        except (ValueError, OverflowError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except sqlite3.Error as error:
            self._json(
                {"error": f"historian database error: {error}"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

    def do_POST(self) -> None:  # noqa: N802
        self._json(
            {"error": "historian API is read-only"},
            HTTPStatus.METHOD_NOT_ALLOWED,
        )

    do_PUT = do_POST
    do_DELETE = do_POST


def positive_int(name: str, default: int, *, minimum: int = 1) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < minimum:
        raise SystemExit(f"{name} must be at least {minimum}")
    return value


def boolean_env(name: str, default: bool) -> bool:
    value = os.environ.get(name, "1" if default else "0").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"{name} must be a Boolean value")


def positive_float(name: str, default: float) -> float:
    value = float(os.environ.get(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise SystemExit(f"{name} must be a positive finite number")
    return value


def main() -> None:
    database = Path(os.environ.get("GIZMO_HISTORIAN_DATABASE", DEFAULT_DATABASE))
    socket_path = Path(os.environ.get("GIZMO_HISTORIAN_SOCKET", DEFAULT_SOCKET))
    endpoint = os.environ.get("GIZMO_HISTORIAN_OPCUA_ENDPOINT", DEFAULT_ENDPOINT)
    replica_url = os.environ.get("GIZMO_HISTORIAN_REPLICA_URL", "").strip()
    capture_interval = positive_float(
        "GIZMO_HISTORIAN_CAPTURE_INTERVAL_SECONDS",
        DEFAULT_CAPTURE_INTERVAL,
    )
    platform_interval = positive_float(
        "GIZMO_HISTORIAN_PLATFORM_INTERVAL_SECONDS",
        DEFAULT_PLATFORM_INTERVAL,
    )
    store = HistorianStore(
        database,
        raw_retention_days=positive_int(
            "GIZMO_HISTORIAN_RAW_RETENTION_DAYS",
            DEFAULT_RAW_RETENTION_DAYS,
        ),
        platform_retention_days=positive_int(
            "GIZMO_HISTORIAN_PLATFORM_RETENTION_DAYS",
            DEFAULT_PLATFORM_RETENTION_DAYS,
        ),
        rollup_retention_days=positive_int(
            "GIZMO_HISTORIAN_ROLLUP_RETENTION_DAYS",
            DEFAULT_ROLLUP_RETENTION_DAYS,
        ),
        event_retention_days=positive_int(
            "GIZMO_HISTORIAN_EVENT_RETENTION_DAYS",
            DEFAULT_EVENT_RETENTION_DAYS,
        ),
        retention_enabled=boolean_env(
            "GIZMO_HISTORIAN_RETENTION_ENABLED",
            True,
        ),
        max_query_points=positive_int(
            "GIZMO_HISTORIAN_MAX_QUERY_POINTS",
            DEFAULT_MAX_QUERY_POINTS,
        ),
        max_export_rows=positive_int(
            "GIZMO_HISTORIAN_MAX_EXPORT_ROWS",
            DEFAULT_MAX_EXPORT_ROWS,
        ),
        min_free_bytes=positive_int(
            "GIZMO_HISTORIAN_MIN_FREE_BYTES",
            DEFAULT_MIN_FREE_BYTES,
        ),
    )
    store.initialize()
    if replica_url:
        replica_batch_size = positive_int(
            "GIZMO_HISTORIAN_REPLICA_BATCH_SIZE",
            DEFAULT_REPLICA_BATCH_SIZE,
        )
        if replica_batch_size > MAX_REPLICA_BATCH_SIZE:
            raise SystemExit(
                "GIZMO_HISTORIAN_REPLICA_BATCH_SIZE exceeds the safe maximum"
            )
        recorder: HistorianRecorder | ReplicaRecorder = ReplicaRecorder(
            store,
            replica_url,
            poll_seconds=positive_float(
                "GIZMO_HISTORIAN_REPLICA_POLL_SECONDS",
                DEFAULT_REPLICA_POLL_SECONDS,
            ),
            batch_size=replica_batch_size,
            expected_source_id=os.environ.get(
                "GIZMO_HISTORIAN_REPLICA_SOURCE_ID",
                "",
            ),
        )
        source_description = f"edge replica {replica_url}"
    else:
        monitor = OpcUaMonitor(
            endpoint,
            positive_int("GIZMO_HISTORIAN_SUBSCRIPTION_MS", 500),
        )
        recorder = HistorianRecorder(
            store,
            monitor,
            capture_interval=capture_interval,
            platform_interval=platform_interval,
        )
        source_description = f"OPC UA {endpoint}"
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o775)
    if socket_path.exists() or socket_path.is_socket():
        socket_path.unlink()
    server = ThreadingUnixHTTPServer(
        str(socket_path),
        HistorianHandler,
        store,
        recorder,
    )
    os.chmod(socket_path, 0o660)
    stopping = threading.Event()

    def request_stop(signum: int, frame: Any) -> None:
        del signum, frame
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    recorder.start()
    print(
        f"GIZMo historian storing {database}; query socket {socket_path}; "
        f"{source_description}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stopping.set()
        server.server_close()
        recorder.stop()
        store.close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
