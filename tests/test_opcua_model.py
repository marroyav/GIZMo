#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "python"))

from gizmo_model import (
    MODEL_NAMESPACE_URI,
    QUALITY_BAD,
    QUALITY_GOOD,
    QUALITY_UNCERTAIN,
    RANGE_IN_RANGE,
    RANGE_OUT_OF_RANGE,
    SystemCollectors,
    parse_legacy_measurement,
    parse_legacy_thermals,
)


class MeasurementModelTests(unittest.TestCase):
    def test_complete_record_becomes_typed_measurement(self) -> None:
        sampled_at = dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.timezone.utc)
        result = parse_legacy_measurement(
            (
                "Data from C-server: Res=192.1,Cap=4,Th=100,Mag=91,"
                "Phase=-0.230,Phase2=179.770,PhaseRX=179.700,"
                "I=-20,Q=1,Alarm=1,AlarmReason=PhaseInterpolation,"
                "latched=1,LatchStamp=2026-07-27 10:11:12"
            ),
            sequence=7,
            averages_per_calculation=100,
            sampled_at=sampled_at,
        )

        self.assertEqual(result.sequence, 7)
        self.assertEqual(result.sampled_at, sampled_at)
        self.assertEqual(result.resistance_ohm, 192.1)
        self.assertEqual(result.resistance_range, RANGE_IN_RANGE)
        self.assertEqual(result.capacitance_nf, 4.0)
        self.assertEqual(result.threshold_ohm, 100.0)
        self.assertEqual(result.phase_atan_deg, -0.23)
        self.assertEqual(result.phase_atan2_deg, 179.77)
        self.assertTrue(result.alarm_active)
        self.assertEqual(result.alarm_reason, "PhaseInterpolation")
        self.assertTrue(result.alarm_latched)
        self.assertIsNotNone(result.latch_time)
        self.assertEqual(result.quality, QUALITY_GOOD)

    def test_internal_sentinel_is_not_published_as_resistance(self) -> None:
        result = parse_legacy_measurement(
            (
                "Res=1050,Cap=0,Th=100,Mag=1,Phase=0,Phase2=0,"
                "PhaseRX=0,I=1,Q=0,Alarm=0,AlarmReason=,"
                "latched=0,LatchStamp="
            ),
            sequence=1,
            averages_per_calculation=100,
        )

        self.assertIsNone(result.resistance_ohm)
        self.assertEqual(result.resistance_range, RANGE_OUT_OF_RANGE)
        self.assertEqual(result.quality, QUALITY_UNCERTAIN)
        self.assertIn("non-numeric out-of-range sentinel", result.diagnostic)

    def test_high_z_boundary_is_not_published_as_numeric_resistance(self) -> None:
        high = parse_legacy_measurement(
            (
                "Res=500.1,Cap=0,Th=100,Mag=1,Phase=0,Phase2=0,"
                "PhaseRX=0,I=1,Q=0,Alarm=0,AlarmReason=,"
                "latched=0,LatchStamp="
            ),
            sequence=1,
            averages_per_calculation=100,
        )
        boundary = parse_legacy_measurement(
            (
                "Res=500.0,Cap=0,Th=100,Mag=1,Phase=0,Phase2=0,"
                "PhaseRX=0,I=1,Q=0,Alarm=0,AlarmReason=,"
                "latched=0,LatchStamp="
            ),
            sequence=2,
            averages_per_calculation=100,
        )

        self.assertIsNone(high.resistance_ohm)
        self.assertEqual(high.resistance_range, RANGE_OUT_OF_RANGE)
        self.assertIn("validated 500 ohm", high.diagnostic)
        self.assertEqual(boundary.resistance_ohm, 500.0)
        self.assertEqual(boundary.resistance_range, RANGE_IN_RANGE)

    def test_invalid_capacitance_is_absent_and_uncertain(self) -> None:
        result = parse_legacy_measurement(
            (
                "Res=200,Cap=-1,Th=100,Mag=1,Phase=0,Phase2=0,"
                "PhaseRX=0,I=1,Q=0,Alarm=0,AlarmReason=,"
                "latched=0,LatchStamp="
            ),
            sequence=2,
            averages_per_calculation=100,
        )

        self.assertIsNone(result.capacitance_nf)
        self.assertEqual(result.quality, QUALITY_UNCERTAIN)
        self.assertIn("negative capacitance", result.diagnostic)

    def test_missing_fields_are_bad_not_silent_zeroes(self) -> None:
        result = parse_legacy_measurement(
            "Res=200,Th=100,latched=0",
            sequence=3,
            averages_per_calculation=100,
        )

        self.assertEqual(result.quality, QUALITY_BAD)
        self.assertIsNone(result.capacitance_nf)
        self.assertIn("missing ZMon fields", result.diagnostic)

    def test_alarm_is_consumed_from_zmon_not_recomputed(self) -> None:
        result = parse_legacy_measurement(
            (
                "Res=200,Cap=4,Th=100,Mag=91,Phase=0,"
                "Phase2=179.8,PhaseRX=150,I=-20,Q=1,"
                "Alarm=0,AlarmReason=,latched=0,LatchStamp="
            ),
            sequence=8,
            averages_per_calculation=100,
        )

        self.assertFalse(result.alarm_active)
        self.assertEqual(result.alarm_reason, "")
        self.assertEqual(result.quality, QUALITY_GOOD)

    def test_thermal_record_uses_explicit_sensor_names(self) -> None:
        result = parse_legacy_thermals("Chassis=24.5,CPU1=40.0,CPU2=41.0,CPU3=42.0")

        self.assertEqual(result.sensors_celsius["Chassis"], 24.5)
        self.assertEqual(result.sensors_celsius["CPU3"], 42.0)
        self.assertEqual(result.quality, QUALITY_GOOD)


class InventoryModelTests(unittest.TestCase):
    def test_mac_provenance_only_claims_fru_when_verified(self) -> None:
        mac = "00:11:22:33:44:55"

        self.assertEqual(
            SystemCollectors._mac_source(mac, "", 0, []),
            "Permanent hardware address",
        )
        self.assertEqual(
            SystemCollectors._mac_source(mac, "", 1, []),
            "Random",
        )
        self.assertEqual(
            SystemCollectors._mac_source(mac, "", 0, [mac]),
            "FRU EEPROM (verified)",
        )

    def test_boardid_parser_separates_factory_mac_addresses(self) -> None:
        values, macs = SystemCollectors._parse_boardid(
            "FRU Board Manufacturer: XILINX\n"
            "FRU Board Product Name: SCK-KR-G\n"
            "FRU Board Serial Number: ABC123\n"
            "FRU OEM MAC ID 0: 00:11:22:33:44:55\n"
            "FRU Error: multirecord area checksum invalid\n"
        )

        self.assertEqual(values["manufacturer"], "XILINX")
        self.assertEqual(values["product_name"], "SCK-KR-G")
        self.assertEqual(values["serial_number"], "ABC123")
        self.assertEqual(macs, ["00:11:22:33:44:55"])

    def test_active_overlay_requires_a_nonnegative_active_slot(self) -> None:
        output = (
            "Accelerator Base Active_slot\n"
            "k26-starter-kits k26-starter-kits -1\n"
            "GIZMo_Kria_3_7_25 GIZMo_Kria_3_7_25 0,\n"
        )

        self.assertTrue(
            SystemCollectors._overlay_is_active(output, "GIZMo_Kria_3_7_25")
        )
        self.assertFalse(
            SystemCollectors._overlay_is_active(output, "k26-starter-kits")
        )

    def test_namespace_is_stable_uri_not_runtime_index(self) -> None:
        self.assertEqual(MODEL_NAMESPACE_URI, "urn:fnal:gizmo")


if __name__ == "__main__":
    unittest.main()
