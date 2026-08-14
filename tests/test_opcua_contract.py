#!/usr/bin/env python3
"""Validate the checked-in, machine-readable Kria OPC UA contract."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "schema" / "gizmo-opcua-contract.json"


class OpcUaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_contract_identity_and_inventory_are_stable(self) -> None:
        self.assertEqual(self.contract["schema_version"], 1)
        self.assertEqual(
            self.contract["authority"], "GIZMo Kria OPC UA implementation"
        )
        self.assertEqual(self.contract["namespace_uri"], "urn:fnal:gizmo")
        self.assertEqual(self.contract["model_version"], "1.3.1")
        self.assertEqual(len(self.contract["objects"]), 43)
        self.assertEqual(len(self.contract["variables"]), 457)
        self.assertEqual(len(self.contract["methods"]), 5)

    def test_embedded_digest_covers_the_canonical_contract(self) -> None:
        unsigned = dict(self.contract)
        expected = unsigned.pop("contract_sha256")
        canonical = json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

        self.assertEqual(hashlib.sha256(canonical).hexdigest(), expected)

    def test_threshold_metadata_remains_kria_authoritative(self) -> None:
        variables = {item["path"]: item for item in self.contract["variables"]}
        threshold = variables["Configuration.ThresholdOhm"]

        self.assertEqual(threshold["node_id"], (
            "nsu=urn:fnal:gizmo;s=GIZMo.Configuration.ThresholdOhm"
        ))
        self.assertEqual(threshold["data_type"], "UInt32")
        self.assertEqual(threshold["access"], "ReadWrite")
        self.assertEqual(threshold["engineering_range"], {
            "low": 0.0,
            "high": 1_000_000.0,
        })

    def test_contract_has_no_consumer_specific_overrides(self) -> None:
        self.assertNotIn("platform_profiles", self.contract)
        self.assertTrue(all(
            "access_by_platform" not in item
            for item in self.contract["variables"]
        ))
        self.assertTrue(all(
            "support_by_platform" not in item
            for item in self.contract["methods"]
        ))


if __name__ == "__main__":
    unittest.main()
