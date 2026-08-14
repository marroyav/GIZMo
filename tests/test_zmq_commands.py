#!/usr/bin/env python3

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


try:
    import zmq  # noqa: F401
except ImportError:
    sys.modules["zmq"] = types.SimpleNamespace()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "python"))

import gizmo_common  # noqa: E402
import gizmo_zmq  # noqa: E402


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temporary.name)
        self.previous_state_dir = gizmo_common.STATE_DIR
        gizmo_common.STATE_DIR = self.state_dir
        (self.state_dir / "setThreshold.env").write_text(
            "export threshold=100\n", encoding="utf-8"
        )
        (self.state_dir / "setRunInterval.env").write_text(
            "export runInterval=100\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        gizmo_common.STATE_DIR = self.previous_state_dir
        self.temporary.cleanup()

    @mock.patch.object(gizmo_zmq, "request_control", return_value="zmon restart requested")
    def test_run_updates_state_and_only_restarts_zmon(self, request: mock.Mock) -> None:
        reply = gizmo_zmq.handle_message("run 250")

        self.assertIn("250", reply)
        self.assertEqual(
            (self.state_dir / "setRunInterval.env").read_text(encoding="utf-8"),
            "export runInterval=250\n",
        )
        self.assertEqual(
            (self.state_dir / "ZMonArg1.env").read_text(encoding="utf-8"),
            'ZMonArg1="set_th 100"\n',
        )
        self.assertEqual(
            (self.state_dir / "ZMonArg2.env").read_text(encoding="utf-8"),
            'ZMonArg2="run 250"\n',
        )
        request.assert_called_once_with("restart-zmon")

    @mock.patch.object(gizmo_zmq, "request_control", return_value="zmon restart requested")
    def test_calibration_writes_all_startup_arguments(self, request: mock.Mock) -> None:
        reply = gizmo_zmq.handle_message("CAL 1000")

        self.assertIn("Calibration requested", reply)
        self.assertEqual(
            (self.state_dir / "ZMonArg1.env").read_text(encoding="utf-8"),
            'ZMonArg1="CAL 1000"\n',
        )
        self.assertEqual(
            (self.state_dir / "ZMonArg3.env").read_text(encoding="utf-8"),
            'ZMonArg3="run 1000"\n',
        )
        request.assert_called_once_with("restart-zmon")

    @mock.patch.object(gizmo_zmq, "request_control", return_value="zmon restart requested")
    def test_threshold_update_is_persistent(self, request: mock.Mock) -> None:
        gizmo_zmq.handle_message("set_th 275")
        self.assertEqual(
            (self.state_dir / "setThreshold.env").read_text(encoding="utf-8"),
            "export threshold=275\n",
        )
        request.assert_called_once_with("restart-zmon")

    def test_threshold_range_is_the_authoritative_kria_contract(self) -> None:
        with self.assertRaises(ValueError):
            gizmo_zmq.handle_message("set_th 1000001")
        self.assertEqual(
            (self.state_dir / "setThreshold.env").read_text(encoding="utf-8"),
            "export threshold=100\n",
        )

    def test_clear_latch_is_atomic_and_compatible(self) -> None:
        reply = gizmo_zmq.handle_message("clear_latch")
        self.assertIn("Cleared", reply)
        self.assertEqual(
            (self.state_dir / "latchState.env").read_text(encoding="utf-8"),
            "latched=0\n\n",
        )

    def test_invalid_commands_do_not_change_state(self) -> None:
        with self.assertRaises(ValueError):
            gizmo_zmq.handle_message("run -1")
        with self.assertRaises(ValueError):
            gizmo_zmq.handle_message("run 0")
        self.assertEqual(
            (self.state_dir / "setRunInterval.env").read_text(encoding="utf-8"),
            "export runInterval=100\n",
        )

    @mock.patch.object(
        gizmo_zmq,
        "request_control",
        side_effect=("system time updated", "zmon restart requested"),
    )
    def test_epoch_time_command_uses_allow_listed_control(
        self, request: mock.Mock
    ) -> None:
        reply = gizmo_zmq.handle_message("set_time_epoch 1785168000")

        self.assertIn("system time updated", reply)
        self.assertEqual(
            request.call_args_list,
            [
                mock.call("set-time 1785168000"),
                mock.call("restart-zmon"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
