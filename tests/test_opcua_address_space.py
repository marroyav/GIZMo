#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import math
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


def unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


PORT = unused_tcp_port()
ENDPOINT = f"opc.tcp://127.0.0.1:{PORT}"
os.environ["GIZMO_OPCUA_ENDPOINT"] = ENDPOINT

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "python"))

from gizmo_security import credential_line

TEST_DIRECTORY = tempfile.TemporaryDirectory()
TEST_STATE = Path(TEST_DIRECTORY.name)
TEST_CREDENTIAL = TEST_STATE / "opcua-users"
TEST_CREDENTIAL.write_text(
    credential_line(
        "test-maintainer",
        "maintenance",
        "correct horse battery staple",
        salt=b"0123456789abcdef",
    )
    + "\n"
    + credential_line(
        "test-operator",
        "operator",
        "another correct horse password",
        salt=b"fedcba9876543210",
    )
    + "\n",
    encoding="utf-8",
)
os.chmod(TEST_CREDENTIAL, 0o600)
os.environ["GIZMO_STATE_DIR"] = str(TEST_STATE)
os.environ["GIZMO_OPCUA_COMMAND_STATE_FILE"] = str(TEST_STATE / "command.json")
os.environ["GIZMO_OPCUA_CREDENTIAL_FILE"] = str(TEST_CREDENTIAL)
os.environ["GIZMO_OPCUA_COMMAND_GATE"] = "maintenance"
os.environ["GIZMO_OPCUA_ALLOW_INSECURE_CREDENTIALS"] = "1"
os.environ["GIZMO_ZMON_BINARY"] = "/bin/true"

from gizmo_model import parse_legacy_measurement
from gizmo_opcua import GizmoOpcUaServer
from opcua import Client, ua


class AddressSpaceTests(unittest.TestCase):
    BASELINE_RECORD = (
        "Data from C-server: Res=192.1,Cap=4,Th=100,Mag=91,"
        "Phase=-0.230,Phase2=179.770,PhaseRX=179.700,"
        "I=-20,Q=1,Alarm=1,AlarmReason=PhaseInterpolation,"
        "latched=1,LatchStamp=2026-07-27 10:11:12"
    )

    @classmethod
    def setUpClass(cls) -> None:
        snapshot = parse_legacy_measurement(
            cls.BASELINE_RECORD,
            sequence=7,
            averages_per_calculation=100,
            sampled_at=dt.datetime.now(dt.timezone.utc),
        )
        cls.server = GizmoOpcUaServer()
        cls.server._apply_measurement((cls.BASELINE_RECORD, snapshot))
        cls.server.server.start()
        cls.client = Client(ENDPOINT)
        cls.client.connect()
        cls.namespace = cls.client.get_namespace_index("urn:fnal:gizmo")
        cls.control_client = Client(ENDPOINT)
        cls.control_client.set_user("test-maintainer")
        cls.control_client.set_password("correct horse battery staple")
        cls.control_client.connect()
        cls.operator_client = Client(ENDPOINT)
        cls.operator_client.set_user("test-operator")
        cls.operator_client.set_password("another correct horse password")
        cls.operator_client.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.operator_client.disconnect()
        cls.control_client.disconnect()
        cls.client.disconnect()
        cls.server.server.stop()
        cls.server._executor.shutdown(wait=False, cancel_futures=True)
        TEST_DIRECTORY.cleanup()

    @classmethod
    def node(cls, path: str):
        return cls.client.get_node(ua.NodeId(f"GIZMo.{path}", cls.namespace))

    @classmethod
    def control_node(cls, path: str):
        return cls.control_client.get_node(
            ua.NodeId(f"GIZMo.{path}", cls.namespace)
        )

    @classmethod
    def operator_node(cls, path: str):
        return cls.operator_client.get_node(
            ua.NodeId(f"GIZMo.{path}", cls.namespace)
        )

    def test_typed_measurement_has_quality_and_source_timestamp(self) -> None:
        snapshot = parse_legacy_measurement(
            self.BASELINE_RECORD,
            sequence=self.server._last_good_measurement_sequence + 1,
            averages_per_calculation=100,
            sampled_at=dt.datetime.now(dt.timezone.utc),
        )
        self.server._apply_measurement((self.BASELINE_RECORD, snapshot))
        value = self.node("Measurement.ResistanceOhm").get_data_value()

        self.assertEqual(value.Value.Value, 192.1)
        self.assertEqual(value.Value.VariantType, ua.VariantType.Double)
        self.assertTrue(value.StatusCode.is_good())
        self.assertIsNotNone(value.SourceTimestamp)

    def test_alarm_is_the_measurement_engine_boolean(self) -> None:
        active = self.node("Alarm.Active")
        reason = self.node("Alarm.Reason")

        self.assertTrue(active.get_value())
        self.assertEqual(reason.get_value(), "PhaseInterpolation")
        self.assertIn(
            "not recomputed",
            active.get_description().Text,
        )

    def test_measurement_exposes_standard_engineering_units_property(self) -> None:
        resistance = self.node("Measurement.ResistanceOhm")
        unit = resistance.get_child(["0:EngineeringUnits"]).get_value()

        self.assertEqual(unit.DisplayName.Text, "Ohm")

    def test_client_resolves_namespace_uri_instead_of_assuming_index(self) -> None:
        self.assertEqual(
            self.node("Identity.ModelNamespaceUri").get_value(),
            "urn:fnal:gizmo",
        )
        self.assertEqual(
            self.node("Identity.ModelVersion").get_value(),
            "1.4.0",
        )

    def test_threshold_range_remains_the_kria_authoritative_contract(self) -> None:
        threshold = self.node("Configuration.ThresholdOhm")
        engineering_range = threshold.get_child(["0:EURange"]).get_value()

        self.assertEqual(
            (engineering_range.Low, engineering_range.High),
            (0, 1_000_000),
        )

    def test_threshold_above_contract_limit_is_not_forwarded(self) -> None:
        threshold = self.server.configuration_threshold
        previous = self.server._last_threshold
        threshold.set_value(ua.Variant(1_000_001, ua.VariantType.UInt32))

        with mock.patch.object(self.server, "request") as request:
            with self.assertRaisesRegex(ValueError, "between 0 and 1000000"):
                self.server._forward_configuration_writes()

        request.assert_not_called()
        self.assertEqual(threshold.get_value(), previous)

    def test_legacy_command_object_is_preserved(self) -> None:
        legacy_namespace = self.client.get_namespace_index("SimpleOPCUAServer")
        value = (
            self.client.get_root_node()
            .get_child(
                [
                    "0:Objects",
                    f"{legacy_namespace}:CommandObject",
                    f"{legacy_namespace}:data",
                ]
            )
            .get_value()
        )

        self.assertTrue(value.startswith("Data from C-server:"))

    def test_descriptions_do_not_publish_the_internal_sentinel_as_ohms(self) -> None:
        description = self.node("Measurement.ResistanceOhm").get_description()

        self.assertIn("Good status", description.Text)
        self.assertNotIn("1050", description.Text)

    def test_unvalidated_stimulus_current_is_present_but_not_fabricated(self) -> None:
        value = self.node("Measurement.StimulusCurrentRmsAmpere").get_attributes(
            [ua.AttributeIds.Value]
        )[0]

        self.assertEqual(value.StatusCode.name, "BadNotSupported")
        self.assertTrue(math.isnan(value.Value.Value))

    def test_model14_operation_readbacks_are_typed_and_discoverable(self) -> None:
        self.assertEqual(
            self.node("Operations.CommandGateState").get_value(),
            "Ready",
        )
        self.assertEqual(
            self.node("Operations.LastCommandState").get_data_type_as_variant_type(),
            ua.VariantType.String,
        )
        self.assertEqual(
            self.node("Calibration.ProgressPercent").get_data_type_as_variant_type(),
            ua.VariantType.Double,
        )

    def test_dashboard_service_is_part_of_owned_runtime_inventory(self) -> None:
        variant_type = self.node(
            "Services.Units.gizmo_dashboard_service.ActiveState"
        ).get_data_type_as_variant_type()

        self.assertEqual(variant_type, ua.VariantType.String)

    def test_canonical_threshold_write_forwards_to_legacy_engine(self) -> None:
        threshold = self.control_node("Configuration.ThresholdOhm")
        threshold.set_value(ua.Variant(200, ua.VariantType.UInt32))

        with mock.patch.object(
            self.server,
            "request",
            return_value="Threshold updated to 200",
        ) as request:
            self.server._forward_configuration_writes()

        request.assert_called_once_with("set_th 200")
        self.assertEqual(threshold.get_value(), 200)
        self.assertEqual(self.server.legacy_threshold.get_value(), 200)
        self.assertEqual(self.server._audit["requester"], "test-maintainer")
        self.assertEqual(self.server._audit["state"], "Running")
        self.server._finish_command(
            self.server._active_command.command_id,
            "Succeeded",
            "test cleanup",
        )

    def test_clear_latch_is_an_explicit_method(self) -> None:
        operations = self.control_node("Operations")
        clear_latch = self.control_node("Operations.ClearLatch")

        with mock.patch.object(
            self.server, "request", return_value="Latch cleared"
        ) as request:
            result = operations.call_method(clear_latch)

        request.assert_called_once_with("clear_latch")
        self.assertIn("Accepted: Latch cleared", result)
        self.assertEqual(self.server._audit["state"], "Running")
        self.server._finish_command(
            self.server._active_command.command_id,
            "Succeeded",
            "test cleanup",
        )

    def test_system_time_is_a_typed_explicit_method(self) -> None:
        operations = self.control_node("Operations")
        set_time = self.control_node("Operations.SetSystemTime")
        requested = dt.datetime(2026, 7, 27, 16, 0, tzinfo=dt.timezone.utc)

        with mock.patch.object(
            self.server, "request", return_value="System time updated"
        ) as request:
            result = operations.call_method(
                set_time,
                ua.Variant(requested, ua.VariantType.DateTime),
            )

        request.assert_called_once_with("set_time_epoch 1785168000")
        self.assertIn("Accepted: System time updated", result)
        self.server._finish_command(
            self.server._active_command.command_id,
            "Succeeded",
            "test cleanup",
        )

    def test_restart_method_requires_pid_hash_and_fresh_measurement(self) -> None:
        operations = self.control_node("Operations")
        restart = self.control_node("Operations.RestartMeasurementEngine")
        self.server._zmon_pid = 100

        with mock.patch.object(
            self.server,
            "request",
            return_value="Measurement interval updated; restart requested",
        ) as request:
            result = operations.call_method(restart)

        request.assert_called_once_with(f"run {self.server._last_run_interval}")
        self.assertIn("awaiting new PID", result)
        self.assertEqual(self.server._audit["state"], "Running")

        self.server._zmon_pid = 101
        sequence = self.server._last_good_measurement_sequence + 1
        record = (
            "Res=1050,Cap=0,Th=200,Mag=1,Phase=0,Phase2=0,"
            "PhaseRX=0,I=1,Q=0,Alarm=0,AlarmReason=,latched=0,LatchStamp="
        )
        snapshot = parse_legacy_measurement(
            record,
            sequence=sequence,
            averages_per_calculation=self.server._last_run_interval,
            sampled_at=dt.datetime.now(dt.timezone.utc),
        )
        self.server._apply_measurement((record, snapshot))
        self.server._advance_active_command(time.monotonic())

        self.assertEqual(self.server._audit["state"], "Succeeded")
        self.assertIn("SHA-256", self.server._audit["result"])

    def test_anonymous_sessions_are_read_only(self) -> None:
        threshold = self.node("Configuration.ThresholdOhm")

        with self.assertRaisesRegex(Exception, "BadUserAccessDenied"):
            threshold.set_value(
                ua.Variant(self.server._last_threshold, ua.VariantType.UInt32)
            )

    def test_anonymous_method_call_is_denied_before_dispatch(self) -> None:
        operations = self.node("Operations")
        clear_latch = self.node("Operations.ClearLatch")

        with mock.patch.object(self.server, "request") as request:
            with self.assertRaisesRegex(Exception, "BadUserAccessDenied"):
                operations.call_method(clear_latch)

        request.assert_not_called()

    def test_operator_role_cannot_call_a_maintenance_method(self) -> None:
        operations = self.operator_node("Operations")
        restart = self.operator_node("Operations.RestartMeasurementEngine")

        with mock.patch.object(self.server, "request") as request:
            with self.assertRaisesRegex(Exception, "BadUserAccessDenied"):
                operations.call_method(restart)

        request.assert_not_called()
        self.assertEqual(self.server._audit["state"], "Rejected")
        self.assertEqual(self.server._audit["requester"], "test-operator")

    def test_calibration_timeout_restores_normal_acquisition(self) -> None:
        operations = self.control_node("Operations")
        calibration = self.control_node("Operations.StartCalibration")
        self.server._zmon_pid = 200

        with mock.patch.object(
            self.server,
            "request",
            side_effect=(
                "Calibration requested; zmon restart requested",
                "Measurement interval restored; zmon restart requested",
            ),
        ) as request:
            operations.call_method(
                calibration,
                ua.Variant(10, ua.VariantType.UInt32),
            )
            assert self.server._active_command is not None
            self.server._active_command.deadline_monotonic = 0
            self.server._advance_active_command(time.monotonic())

        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            self.server._active_command.verification,
            "calibration-timeout-restore",
        )
        self.assertEqual(
            self.node("Calibration.RestorationState").get_value(),
            "Commanded",
        )

        self.server._zmon_pid = 201
        snapshot = parse_legacy_measurement(
            self.BASELINE_RECORD,
            sequence=self.server._last_good_measurement_sequence + 1,
            averages_per_calculation=self.server._last_run_interval,
            sampled_at=dt.datetime.now(dt.timezone.utc),
        )
        self.server._apply_measurement((self.BASELINE_RECORD, snapshot))
        self.server._advance_active_command(time.monotonic())

        self.assertEqual(self.server._audit["state"], "TimedOut")
        self.assertEqual(
            self.node("Calibration.OperationState").get_value(),
            "Failed",
        )
        self.assertEqual(
            self.node("Calibration.RestorationState").get_value(),
            "Verified",
        )

    def test_missing_credentials_force_the_command_gate_disabled(self) -> None:
        missing = TEST_STATE / "does-not-exist"
        state = TEST_STATE / "disabled-command-state.json"
        with mock.patch.dict(
            os.environ,
            {
                "GIZMO_OPCUA_CREDENTIAL_FILE": str(missing),
                "GIZMO_OPCUA_COMMAND_STATE_FILE": str(state),
                "GIZMO_OPCUA_COMMAND_GATE": "maintenance",
                "GIZMO_OPCUA_ALLOW_INSECURE_CREDENTIALS": "1",
            },
        ):
            disabled = GizmoOpcUaServer()
        try:
            self.assertEqual(disabled._effective_gate_mode, "disabled")
            self.assertIn("no valid credentials", disabled._command_policy_reason)
            self.assertEqual(
                disabled._points["Operations.CommandGateState"][0].get_value(),
                "Disabled",
            )
        finally:
            disabled._executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
