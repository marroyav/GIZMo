#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import os
import socket
import sys
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

from gizmo_model import parse_legacy_measurement
from gizmo_opcua import GizmoOpcUaServer
from opcua import Client, ua


class AddressSpaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        record = (
            "Data from C-server: Res=192.1,Cap=4,Th=100,Mag=91,"
            "Phase=-0.230,Phase2=179.770,PhaseRX=179.700,"
            "I=-20,Q=1,latched=1,LatchStamp=2026-07-27 10:11:12"
        )
        snapshot = parse_legacy_measurement(
            record,
            sequence=7,
            averages_per_calculation=100,
            sampled_at=dt.datetime.now(dt.timezone.utc),
        )
        cls.server = GizmoOpcUaServer()
        cls.server._apply_measurement((record, snapshot))
        cls.server.server.start()
        cls.client = Client(ENDPOINT)
        cls.client.connect()
        cls.namespace = cls.client.get_namespace_index("urn:fnal:gizmo")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.disconnect()
        cls.server.server.stop()
        cls.server._executor.shutdown(wait=False, cancel_futures=True)

    @classmethod
    def node(cls, path: str):
        return cls.client.get_node(ua.NodeId(f"GIZMo.{path}", cls.namespace))

    def test_typed_measurement_has_quality_and_source_timestamp(self) -> None:
        value = self.node("Measurement.ResistanceOhm").get_data_value()

        self.assertEqual(value.Value.Value, 192.1)
        self.assertEqual(value.Value.VariantType, ua.VariantType.Double)
        self.assertTrue(value.StatusCode.is_good())
        self.assertIsNotNone(value.SourceTimestamp)

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
            "1.1.1",
        )

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

        self.assertIn("BadOutOfRange", description.Text)
        self.assertNotIn("1050", description.Text)

    def test_dashboard_service_is_part_of_owned_runtime_inventory(self) -> None:
        variant_type = self.node(
            "Services.Units.gizmo_dashboard_service.ActiveState"
        ).get_data_type_as_variant_type()

        self.assertEqual(variant_type, ua.VariantType.String)

    def test_canonical_threshold_write_forwards_to_legacy_engine(self) -> None:
        threshold = self.node("Configuration.ThresholdOhm")
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

    def test_clear_latch_is_an_explicit_method(self) -> None:
        operations = self.node("Operations")
        clear_latch = self.node("Operations.ClearLatch")

        with mock.patch.object(
            self.server, "request", return_value="Latch cleared"
        ) as request:
            result = operations.call_method(clear_latch)

        request.assert_called_once_with("clear_latch")
        self.assertEqual(result, "Latch cleared")

    def test_system_time_is_a_typed_explicit_method(self) -> None:
        operations = self.node("Operations")
        set_time = self.node("Operations.SetSystemTime")
        requested = dt.datetime(2026, 7, 27, 16, 0, tzinfo=dt.timezone.utc)

        with mock.patch.object(
            self.server, "request", return_value="System time updated"
        ) as request:
            result = operations.call_method(
                set_time,
                ua.Variant(requested, ua.VariantType.DateTime),
            )

        request.assert_called_once_with("set_time_epoch 1785168000")
        self.assertEqual(result, "System time updated")


if __name__ == "__main__":
    unittest.main()
