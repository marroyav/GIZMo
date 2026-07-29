#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import inspect
import json
import math
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "python"))

import gizmo_dashboard as dashboard
from gizmo_model import REQUIRED_UNITS, SYSTEMD_UNITS


class DashboardContractTests(unittest.TestCase):
    def test_catalog_paths_are_unique_and_every_view_is_chartable(self) -> None:
        paths = [spec.path for spec in dashboard.VARIABLES]

        self.assertEqual(len(paths), len(set(paths)))
        for view in dashboard.CHART_VIEWS:
            self.assertTrue(view["paths"])
            for path in view["paths"]:
                self.assertIn(path, dashboard.CATALOG)
                self.assertTrue(dashboard.CATALOG[path].chartable)

    def test_service_paths_use_the_canonical_safe_node_identifier(self) -> None:
        self.assertEqual(
            dashboard.service_node_key("gizmo-opcua.service"),
            "gizmo_opcua_service",
        )
        dashboard_path = (
            "Services.Units.gizmo_dashboard_service.ActiveState"
        )
        self.assertIn(dashboard_path, dashboard.CATALOG)
        self.assertIn("gizmo-dashboard.service", SYSTEMD_UNITS)
        self.assertIn("gizmo-dashboard.service", REQUIRED_UNITS)

    def test_json_values_are_finite_and_timestamped(self) -> None:
        naive = dt.datetime(2026, 7, 27, 12, 30)

        self.assertIsNone(dashboard.json_value(math.nan))
        self.assertIsNone(dashboard.json_value(math.inf))
        self.assertEqual(
            dashboard.json_value(naive),
            "2026-07-27T12:30:00+00:00",
        )

    def test_browser_assets_are_self_contained_and_ids_are_resolved(self) -> None:
        html = (REPO_ROOT / "web" / "dashboard" / "index.html").read_text()
        javascript = (REPO_ROOT / "web" / "dashboard" / "app.js").read_text()
        stylesheet = (
            REPO_ROOT / "web" / "dashboard" / "styles.css"
        ).read_text()
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        referenced_ids = set(re.findall(r'byId\("([^"]+)"\)', javascript))

        self.assertFalse(referenced_ids - ids)
        self.assertNotRegex(html, r"https?://")
        self.assertNotRegex(stylesheet, r"url\(\s*[\"']?https?://")
        self.assertNotIn(".set_value(", inspect.getsource(dashboard))
        self.assertFalse(hasattr(dashboard.DashboardHandler, "do_PUT"))
        self.assertFalse(hasattr(dashboard.DashboardHandler, "do_DELETE"))

    def test_browser_uses_semantic_industrial_status_design(self) -> None:
        html = (REPO_ROOT / "web" / "dashboard" / "index.html").read_text()
        javascript = (REPO_ROOT / "web" / "dashboard" / "app.js").read_text()
        stylesheet = (
            REPO_ROOT / "web" / "dashboard" / "styles.css"
        ).read_text()

        self.assertIn('<nav class="section-nav"', html)
        self.assertIn('<details id="variables"', html)
        self.assertIn("<output id=\"resistance-value\"", html)
        self.assertIn('id="chart-stack"', html)
        self.assertNotIn('id="chart-tabs"', html)
        self.assertIn("--graphite:", stylesheet)
        self.assertIn("--amber-strong:", stylesheet)
        self.assertIn("--canvas: #1c1f22", stylesheet)
        self.assertIn("--surface: #25292d", stylesheet)
        self.assertIn("--emerald: #b8c41f", stylesheet)
        self.assertIn("--alarm: #ff6a2a", stylesheet)
        self.assertEqual(stylesheet.count("#ff6a2a"), 1)
        self.assertIn('const HIGH_Z_COLOR = "#b8c41f"', javascript)
        self.assertIn('"JetBrains Mono"', stylesheet)
        self.assertIn("grid-template-columns: repeat(2", stylesheet)
        self.assertRegex(
            stylesheet,
            r"\.chart-panel\s*\{[^}]*\bborder:\s*0;",
        )
        self.assertRegex(stylesheet, r"\.card\s*\{[^}]*\bborder:\s*0;")
        self.assertRegex(
            stylesheet,
            r"\.alarm-card\.state-clear[\s\S]*?h2\s*\{[^}]*#b8c41f",
        )
        self.assertIn("body.has-alarm", stylesheet)
        self.assertIn(
            'document.body.classList.toggle("has-alarm", activeAlarm)',
            javascript,
        )
        self.assertIn('const ALARM_COLOR = "#ff6a2a"', javascript)
        self.assertIn('alarm: "Authoritative composite relay/beacon alarm', javascript)
        self.assertIn("function drawCharts()", javascript)

        alarm_view = next(
            view for view in dashboard.CHART_VIEWS if view["id"] == "alarm"
        )
        self.assertEqual(alarm_view["paths"], ["Alarm.Active"])
        self.assertTrue(dashboard.CATALOG["Alarm.Active"].chartable)
        render_alarm = javascript.split("function renderAlarm()", 1)[1].split(
            "function pulseItem", 1
        )[0]
        self.assertIn('boolean("Alarm.Active")', render_alarm)
        self.assertNotIn("Measurement.Resistance", render_alarm)
        self.assertNotIn("Measurement.Phase", render_alarm)

    def test_uncertain_notification_retains_last_usable_value(self) -> None:
        path = "Measurement.ThresholdOhm"
        monitor = dashboard.OpcUaMonitor("opc.tcp://unused", 500)
        monitor._node_paths["test-node"] = path
        monitor._values[path]["value"] = 100.0
        node = SimpleNamespace(
            nodeid=SimpleNamespace(to_string=lambda: "test-node")
        )
        uncertain = SimpleNamespace(
            StatusCode=SimpleNamespace(name="UncertainLastUsableValue"),
            SourceTimestamp=None,
            ServerTimestamp=None,
        )

        monitor.record_datachange(node, None, uncertain)

        self.assertEqual(monitor.snapshot()["values"][path]["value"], 100.0)


class FakeMonitor:
    def snapshot(self) -> dict[str, object]:
        return {
            "sequence": 1,
            "generated_at": "2026-07-27T12:00:00+00:00",
            "connection": {
                "connected": True,
                "endpoint": "opc.tcp://127.0.0.1:4840",
            },
            "values": {},
        }


class DashboardHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = dashboard.DashboardServer(
            ("127.0.0.1", 0),
            dashboard.DashboardHandler,
            FakeMonitor(),
            REPO_ROOT / "web" / "dashboard",
            0.1,
        )
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def read(self, path: str) -> tuple[bytes, object]:
        with urllib.request.urlopen(self.base + path, timeout=2) as response:
            return response.read(), response.headers

    def test_static_page_has_security_headers(self) -> None:
        body, headers = self.read("/")

        self.assertIn(b"GIZMo", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        request = urllib.request.Request(self.base + "/", method="HEAD")
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertGreater(int(response.headers["Content-Length"]), 0)

    def test_catalog_and_health_endpoints(self) -> None:
        catalog_body, _ = self.read("/api/catalog")
        health_body, _ = self.read("/healthz")
        catalog = json.loads(catalog_body)
        health = json.loads(health_body)

        self.assertEqual(catalog["namespace_uri"], "urn:fnal:gizmo")
        self.assertGreater(len(catalog["variables"]), 100)
        self.assertEqual(catalog["resistance_high_z_floor_ohm"], 500)
        self.assertFalse(catalog["history_available"])
        self.assertTrue(health["opcua_connected"])
        self.assertFalse(health["historian_available"])

    def test_unavailable_historian_is_a_bounded_degraded_route(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(
                self.base + "/api/history/status",
                timeout=2,
            )

        self.assertEqual(raised.exception.code, 503)
        self.assertEqual(
            json.loads(raised.exception.read()),
            {"error": "persistent history is unavailable"},
        )

    def test_mutating_http_request_is_rejected(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/state",
            data=b"{}",
            method="POST",
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 405)
        self.assertEqual(
            json.loads(raised.exception.read()),
            {"error": "dashboard API is read-only"},
        )


if __name__ == "__main__":
    unittest.main()
