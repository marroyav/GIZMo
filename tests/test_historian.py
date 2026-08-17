#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "python"))

import gizmo_historian as historian
from gizmo_dashboard import VARIABLES


def stamp(second: int) -> str:
    return dt.datetime(
        2026,
        7,
        27,
        12,
        0,
        second,
        tzinfo=dt.timezone.utc,
    ).isoformat()


def fake_snapshot(second: int, *, high_z: bool = False) -> dict[str, object]:
    source = stamp(second)
    values = {}
    for index, spec in enumerate(VARIABLES):
        if spec.data_type in {"Double", "UInt32", "UInt64"}:
            value: object = float(index + second)
        elif spec.data_type == "Boolean":
            value = False
        elif spec.data_type == "DateTime":
            value = source
        else:
            value = f"value-{index}"
        values[spec.path] = {
            "value": value,
            "status": "Good",
            "source_timestamp": source,
            "server_timestamp": None,
            "received_at": source,
        }
    values["Identity.BootId"]["value"] = "boot-test"
    values["Measurement.Sequence"]["value"] = second
    values["Measurement.ResistanceOhm"]["value"] = (
        None if high_z else 200.0 + second
    )
    values["Measurement.ResistanceOhm"]["status"] = (
        "Good"
    )
    values["Measurement.ResistanceRange"]["value"] = (
        "OutOfRange" if high_z else "InRange"
    )
    values["Measurement.ResistanceRange"]["status"] = "Good"
    return {
        "sequence": second,
        "generated_at": source,
        "connection": {
            "connected": True,
            "endpoint": "opc.tcp://127.0.0.1:4840",
        },
        "values": values,
    }


class HistorianTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "history.sqlite3"
        self.store = historian.HistorianStore(
            self.database,
            raw_retention_days=14,
            platform_retention_days=30,
            rollup_retention_days=365,
            event_retention_days=1825,
            min_free_bytes=1,
        )
        self.store.initialize()
        self.stream = self.store.begin_stream("boot-test", 1, "test")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_new_database_uses_incremental_auto_vacuum(self) -> None:
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute("PRAGMA auto_vacuum").fetchone()[0],
                2,
            )

    def test_storage_status_uses_bounded_covering_index_scans(self) -> None:
        with sqlite3.connect(self.database) as connection:
            for table, index in (
                ("fast_sample", "fast_sample_status_scan"),
                ("platform_sample", "platform_sample_status_scan"),
                ("minute_rollup", "minute_rollup_status_scan"),
            ):
                indexes = {
                    row[1]
                    for row in connection.execute(f"PRAGMA index_list({table})")
                }
                self.assertIn(index, indexes)

        original_limit = historian.STATUS_AVERAGE_SAMPLE_ROWS
        historian.STATUS_AVERAGE_SAMPLE_ROWS = 2
        try:
            self.store.writer.executemany(
                """
                INSERT INTO fast_sample(
                    receive_time_us, stream_id, sequence, payload
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    (1, self.stream, 1, b"x"),
                    (2, self.stream, 2, b"yy"),
                    (3, self.stream, 3, b"zzzz"),
                ),
            )
            self.store.writer.commit()
            status = self.store.storage_status()
        finally:
            historian.STATUS_AVERAGE_SAMPLE_ROWS = original_limit

        self.assertEqual(status["fast_samples"]["count"], 3)
        self.assertEqual(status["fast_samples"]["average_payload_bytes"], 3)
        self.assertEqual(
            status["fast_samples"]["average_payload_sample_rows"],
            2,
        )

    def test_model14_command_and_calibration_state_are_event_paths(self) -> None:
        self.assertIn("Operations.LastCommandState", historian.EVENT_PATHS)
        self.assertIn("Operations.LastCommandResult", historian.EVENT_PATHS)
        self.assertIn("Calibration.OperationState", historian.EVENT_PATHS)
        self.assertIn("Calibration.RestorationState", historian.EVENT_PATHS)
        self.assertNotIn(
            "Measurement.StimulusCurrentRmsAmpere",
            historian.FAST_CAPTURE_PATHS,
        )

    def test_payload_round_trip_preserves_status_and_source_time(self) -> None:
        snapshot = fake_snapshot(1, high_z=True)
        encoded = historian.encode_payload(
            historian.FAST_CAPTURE_PATHS,
            snapshot["values"],
        )
        decoded = historian.decode_payload(
            historian.FAST_CAPTURE_PATHS,
            encoded,
        )

        self.assertIsNone(
            decoded["Measurement.ResistanceOhm"]["value"]
        )
        self.assertEqual(
            decoded["Measurement.ResistanceOhm"]["status"],
            "Good",
        )
        self.assertEqual(
            historian.iso_from_us(
                decoded["Measurement.ResistanceOhm"]["source_time_us"]
            ),
            stamp(1),
        )

    def test_raw_and_rollup_queries_keep_high_z_non_numeric(self) -> None:
        first = fake_snapshot(1, high_z=True)
        second = fake_snapshot(2, high_z=True)
        for snapshot in (first, second):
            receive_time_us, _ = self.store.record_fast(snapshot, self.stream)
            accumulator = getattr(self, "accumulator", None)
            if accumulator is None:
                bucket = receive_time_us // 60_000_000 * 60_000_000
                accumulator = historian.MinuteAccumulator(bucket)
                self.accumulator = accumulator
            accumulator.update(snapshot)
        self.store.write_rollup(self.accumulator)
        start = historian.parse_time_us(stamp(0))
        end = historian.parse_time_us(stamp(3))
        assert start is not None and end is not None

        raw = self.store.query(
            ("Measurement.ResistanceOhm", "Measurement.ThresholdOhm"),
            start,
            end,
            max_points=10,
        )
        rolled = self.store.query(
            ("Measurement.ResistanceOhm", "Measurement.ThresholdOhm"),
            start,
            end,
            max_points=1,
        )

        self.assertEqual(raw["resolution"], "raw")
        self.assertEqual(len(raw["points"]), 2)
        self.assertIsNone(
            raw["points"][0]["values"]["Measurement.ResistanceOhm"]["value"]
        )
        self.assertEqual(raw["points"][0]["resistanceRange"], "OutOfRange")
        self.assertIn("rollup", rolled["resolution"])
        self.assertEqual(len(rolled["points"]), 1)
        self.assertIsNone(
            rolled["points"][0]["values"]["Measurement.ResistanceOhm"]["value"]
        )
        self.assertEqual(rolled["points"][0]["resistanceRange"], "OutOfRange")

    def test_composite_alarm_boolean_is_retained_and_rolls_up(self) -> None:
        first = fake_snapshot(1)
        second = fake_snapshot(2)
        second["values"]["Alarm.Active"]["value"] = True
        accumulator = None
        for snapshot in (first, second):
            receive_time_us, _ = self.store.record_fast(snapshot, self.stream)
            if accumulator is None:
                bucket = receive_time_us // 60_000_000 * 60_000_000
                accumulator = historian.MinuteAccumulator(bucket)
            accumulator.update(snapshot)
        assert accumulator is not None
        self.store.write_rollup(accumulator)
        start = historian.parse_time_us(stamp(0))
        end = historian.parse_time_us(stamp(3))
        assert start is not None and end is not None

        raw = self.store.query(("Alarm.Active",), start, end, max_points=10)
        rolled = self.store.query(("Alarm.Active",), start, end, max_points=1)

        self.assertEqual(
            [point["values"]["Alarm.Active"]["value"] for point in raw["points"]],
            [False, True],
        )
        alarm_rollup = rolled["points"][0]["values"]["Alarm.Active"]
        self.assertEqual(alarm_rollup["value"], 0.5)
        self.assertEqual(alarm_rollup["aggregate"]["maximum"], 1.0)
        self.assertEqual(historian.ROLLUP_PATHS[-1], "Alarm.Active")

    def test_platform_query_events_csv_and_storage_status(self) -> None:
        snapshot = fake_snapshot(1)
        self.store.record_fast(snapshot, self.stream)
        self.store.record_platform(snapshot)
        self.store.record_event(
            "Alarm.Active",
            "Alarm became active",
            severity="alarm",
            details={"reason": "test"},
            time_us=historian.parse_time_us(stamp(1)),
        )
        start = historian.parse_time_us(stamp(0))
        end = historian.parse_time_us(stamp(3))
        assert start is not None and end is not None

        query = self.store.query(
            ("OperatingSystem.CpuUtilizationPercent",),
            start,
            end,
        )
        events = self.store.query_events(start, end)
        exported = self.store.export_csv(
            ("Measurement.ResistanceOhm",),
            start,
            end,
        ).decode()
        status = self.store.storage_status()

        self.assertEqual(query["point_count"], 1)
        self.assertEqual(events["events"][0]["severity"], "alarm")
        self.assertIn("Measurement.ResistanceRange", exported)
        self.assertEqual(status["fast_samples"]["count"], 1)
        self.assertEqual(status["platform_samples"]["count"], 1)
        self.assertEqual(
            status["fast_samples"]["average_payload_sample_rows"],
            1,
        )
        self.assertGreater(
            status["projected_month_bytes_from_payloads"],
            0,
        )

    def test_retention_removes_only_expired_rows(self) -> None:
        snapshot = fake_snapshot(1)
        receive_time_us, _ = self.store.record_fast(snapshot, self.stream)
        self.store.record_platform(snapshot)
        removed = self.store.apply_retention(
            receive_time_us + 40 * 86400 * 1_000_000
        )

        self.assertEqual(removed["fast_sample"], 1)
        self.assertEqual(removed["platform_sample"], 1)
        self.assertEqual(self.store.storage_status()["fast_samples"]["count"], 0)

    def test_disabled_retention_preserves_full_replica_history(self) -> None:
        database = Path(self.temporary.name) / "unlimited-history.sqlite3"
        store = historian.HistorianStore(
            database,
            raw_retention_days=14,
            platform_retention_days=30,
            rollup_retention_days=365,
            event_retention_days=1825,
            retention_enabled=False,
            min_free_bytes=1,
        )
        store.initialize()
        try:
            stream = store.begin_stream("boot-unlimited", 1, "test")
            receive_time_us, _ = store.record_fast(fake_snapshot(1), stream)
            store.record_platform(fake_snapshot(1))
            removed = store.apply_retention(
                receive_time_us + 10_000 * 86400 * 1_000_000
            )

            self.assertEqual(
                removed,
                {
                    "fast_sample": 0,
                    "platform_sample": 0,
                    "minute_rollup": 0,
                    "event": 0,
                },
            )
            status = store.storage_status()
            self.assertFalse(status["retention_enabled"])
            self.assertEqual(status["fast_samples"]["count"], 1)
            self.assertEqual(status["platform_samples"]["count"], 1)

            query = store.query(
                ("Measurement.ResistanceOhm",),
                receive_time_us - 1,
                receive_time_us + 2_000 * 86400 * 1_000_000,
                max_points=10,
            )
            self.assertEqual(query["point_count"], 1)
        finally:
            store.close()

    def test_cursor_replication_is_raw_atomic_and_idempotent(self) -> None:
        first = fake_snapshot(1)
        second = fake_snapshot(2, high_z=True)
        accumulator = None
        for snapshot in (first, second):
            receive_time_us, _ = self.store.record_fast(snapshot, self.stream)
            if accumulator is None:
                bucket = receive_time_us // 60_000_000 * 60_000_000
                accumulator = historian.MinuteAccumulator(bucket)
            accumulator.update(snapshot)
        assert accumulator is not None
        self.store.record_platform(second)
        self.store.write_rollup(accumulator)
        self.store.record_event(
            "Alarm.Active",
            "replication test event",
            severity="alarm",
            details={"active": True},
            time_us=historian.parse_time_us(stamp(2)),
        )

        replica = historian.HistorianStore(
            Path(self.temporary.name) / "replica.sqlite3",
            min_free_bytes=1,
        )
        replica.initialize()
        try:
            fast_first = self.store.replication_batch(
                "fast_sample",
                0,
                0,
                limit=1,
            )
            self.assertTrue(fast_first["has_more"])
            source_id = fast_first["source"]["id"]
            imported_first = replica.import_replication_batch(
                fast_first,
                expected_source_id=source_id,
            )
            fast_cursor = imported_first["cursor"]
            fast_second = self.store.replication_batch(
                "fast_sample",
                fast_cursor["key"],
                fast_cursor["tie"],
                limit=10,
            )
            self.assertFalse(fast_second["has_more"])
            replica.import_replication_batch(
                fast_second,
                expected_source_id=source_id,
            )

            # Replaying an acknowledged batch must neither duplicate nor fail.
            replay = replica.import_replication_batch(
                fast_first,
                expected_source_id=source_id,
            )
            self.assertEqual(replay["imported"], 0)
            self.assertEqual(replay["skipped"], 1)

            for table in (
                "platform_sample",
                "minute_rollup",
                "event",
            ):
                batch = self.store.replication_batch(table, 0, 0, limit=10)
                replica.import_replication_batch(
                    batch,
                    expected_source_id=source_id,
                )

            status = replica.storage_status()
            self.assertEqual(status["fast_samples"]["count"], 2)
            self.assertEqual(status["platform_samples"]["count"], 1)
            self.assertEqual(status["minute_rollups"]["count"], 1)
            self.assertEqual(status["events"]["count"], 1)
            self.assertEqual(
                {
                    row["table"]
                    for row in status["replication"]
                },
                set(historian.REPLICATION_TABLES),
            )
            start = historian.parse_time_us(stamp(0))
            end = historian.parse_time_us(stamp(3))
            assert start is not None and end is not None
            query = replica.query(
                ("Measurement.ResistanceOhm", "Alarm.Active"),
                start,
                end,
                max_points=10,
            )
            self.assertEqual(query["point_count"], 2)
            self.assertEqual(query["points"][1]["resistanceRange"], "OutOfRange")

            with self.assertRaisesRegex(ValueError, "configured identity"):
                replica.import_replication_batch(
                    fast_second,
                    expected_source_id="unexpected-board",
                )
        finally:
            replica.close()

    def test_replica_recorder_catches_up_over_bounded_http_batches(self) -> None:
        self.store.record_fast(fake_snapshot(1), self.stream)

        source_store = self.store

        class ReplicationHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                split = urlsplit(self.path)
                query = parse_qs(split.query)
                if split.path != "/replication":
                    self.send_error(404)
                    return
                body = json.dumps(
                    source_store.replication_batch(
                        query["table"][0],
                        int(query.get("after", ["0"])[0]),
                        int(query.get("after_tie", ["0"])[0]),
                        limit=int(query.get("limit", ["1000"])[0]),
                    ),
                    separators=(",", ":"),
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

        http_server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            ReplicationHandler,
        )
        http_thread = threading.Thread(
            target=http_server.serve_forever,
            daemon=True,
        )
        http_thread.start()
        replica = historian.HistorianStore(
            Path(self.temporary.name) / "network-replica.sqlite3",
            retention_enabled=False,
            min_free_bytes=1,
        )
        replica.initialize()
        recorder = historian.ReplicaRecorder(
            replica,
            f"http://127.0.0.1:{http_server.server_port}/replication",
            poll_seconds=0.05,
            batch_size=1,
        )
        recorder.start()
        try:
            deadline = time.monotonic() + 3
            while (
                replica.storage_status()["fast_samples"]["count"] < 1
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            status = recorder.status()
            self.assertEqual(
                replica.storage_status()["fast_samples"]["count"],
                1,
            )
            self.assertTrue(status["replica_connected"])
            self.assertEqual(status["source_hostname"], socket.gethostname())
            self.assertIsNotNone(status["last_sample_at"])
            self.assertEqual(status["dropped_samples"], 0)
            self.assertIn("sync_errors", status)
        finally:
            recorder.stop()
            http_server.shutdown()
            http_server.server_close()
            http_thread.join(timeout=2)
            replica.close()

    def test_unix_http_status_and_read_only_boundary(self) -> None:
        socket_path = Path(self.temporary.name) / "historian.sock"
        recorder = SimpleNamespace(
            status=lambda: {
                "opcua_connected": True,
                "last_error": "",
            }
        )
        server = historian.ThreadingUnixHTTPServer(
            str(socket_path),
            historian.HistorianHandler,
            self.store,
            recorder,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = self._unix_request(socket_path, "GET", "/status")
            replication_status, replication_body = self._unix_request(
                socket_path,
                "GET",
                "/replication?table=fast_sample&after=0&limit=10",
            )
            post_status, post_body = self._unix_request(
                socket_path,
                "POST",
                "/status",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")
        self.assertEqual(replication_status, 200)
        self.assertEqual(
            json.loads(replication_body)["table"],
            "fast_sample",
        )
        self.assertEqual(post_status, 405)
        self.assertEqual(
            json.loads(post_body),
            {"error": "historian API is read-only"},
        )

    @staticmethod
    def _unix_request(
        socket_path: Path,
        method: str,
        path: str,
    ) -> tuple[int, bytes]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            client.sendall(
                (
                    f"{method} {path} HTTP/1.1\r\n"
                    "Host: localhost\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
            )
            response = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
        headers, body = response.split(b"\r\n\r\n", 1)
        status = int(headers.splitlines()[0].split()[1])
        return status, body


if __name__ == "__main__":
    unittest.main()
