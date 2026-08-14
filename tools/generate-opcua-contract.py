#!/usr/bin/env python3
"""Generate the authoritative GIZMo--SC OPC UA contract from the Kria model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def localized_text(value: object) -> str:
    return str(getattr(value, "Text", "") or "")


def node_identifier(node: object) -> str:
    identifier = getattr(node.nodeid, "Identifier", None)
    if not isinstance(identifier, str) or not identifier.startswith("GIZMo."):
        raise RuntimeError(f"non-canonical NodeId in model: {node.nodeid}")
    return identifier


def argument_contract(argument: object) -> dict[str, Any]:
    from opcua import ua  # noqa: PLC0415

    identifier = getattr(argument.DataType, "Identifier", None)
    if not isinstance(identifier, int):
        raise RuntimeError(f"unsupported argument datatype {argument.DataType}")
    return {
        "name": str(argument.Name or ""),
        "data_type": ua.VariantType(identifier).name,
        "data_type_node_id": f"i={identifier}",
        "value_rank": int(argument.ValueRank),
        "description": localized_text(argument.Description),
    }


def render_contract(repo_root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(repo_root / "src" / "python"))
    os.environ.setdefault("GIZMO_OPCUA_ENDPOINT", "opc.tcp://127.0.0.1:48887")
    # Generation only constructs the in-process address space. A loopback,
    # non-started server needs no site certificate and must not depend on one.
    os.environ.setdefault("GIZMO_OPCUA_ALLOW_INSECURE", "1")

    from gizmo_model import (  # noqa: PLC0415
        MODEL_NAMESPACE_URI,
        MODEL_PUBLICATION_DATE,
        MODEL_VERSION,
    )
    from gizmo_opcua import GizmoOpcUaServer  # noqa: PLC0415
    from opcua import ua  # noqa: PLC0415

    server = GizmoOpcUaServer()
    try:
        root = server.server.get_node(server._node_id("Device"))
        objects: list[dict[str, Any]] = []
        methods: list[dict[str, Any]] = []
        pending = [root]
        seen: set[str] = set()
        while pending:
            node = pending.pop()
            identifier = node_identifier(node)
            if identifier in seen:
                continue
            seen.add(identifier)
            node_class = node.get_node_class()
            parent = node.get_parent()
            parent_identifier = getattr(parent.nodeid, "Identifier", None)
            parent_path = (
                parent_identifier.removeprefix("GIZMo.")
                if isinstance(parent_identifier, str)
                and parent_identifier.startswith("GIZMo.")
                else None
            )
            if node_class == ua.NodeClass.Object:
                objects.append(
                    {
                        "path": identifier.removeprefix("GIZMo."),
                        "node_id": f"nsu={MODEL_NAMESPACE_URI};s={identifier}",
                        "parent": parent_path,
                        "browse_name": node.get_browse_name().Name,
                        "description": localized_text(node.get_description()),
                    }
                )
                pending.extend(
                    node.get_children(refs=ua.ObjectIds.HierarchicalReferences)
                )
            elif node_class == ua.NodeClass.Method:
                inputs: list[dict[str, Any]] = []
                outputs: list[dict[str, Any]] = []
                for prop in node.get_properties():
                    name = prop.get_browse_name().Name
                    if name == "InputArguments":
                        inputs = [argument_contract(item) for item in prop.get_value()]
                    elif name == "OutputArguments":
                        outputs = [argument_contract(item) for item in prop.get_value()]
                methods.append(
                    {
                        "path": identifier.removeprefix("GIZMo."),
                        "node_id": f"nsu={MODEL_NAMESPACE_URI};s={identifier}",
                        "parent": parent_path,
                        "browse_name": node.get_browse_name().Name,
                        "description": localized_text(node.get_description()),
                        "inputs": inputs,
                        "outputs": outputs,
                    }
                )

        variables: list[dict[str, Any]] = []
        for path, (node, variant_type, _default) in sorted(server._points.items()):
            unit = None
            value_range = None
            for prop in node.get_properties():
                browse_name = prop.get_browse_name().Name
                value = prop.get_value()
                if browse_name == "EngineeringUnits":
                    unit = {
                        "namespace_uri": str(value.NamespaceUri),
                        "unit_id": int(value.UnitId),
                        "symbol": localized_text(value.DisplayName),
                        "description": localized_text(value.Description),
                    }
                elif browse_name == "EURange":
                    value_range = {
                        "low": float(value.Low),
                        "high": float(value.High),
                    }
            access_level = int(
                node.get_attribute(ua.AttributeIds.AccessLevel).Value.Value
            )
            variables.append(
                {
                    "path": path,
                    "node_id": f"nsu={MODEL_NAMESPACE_URI};s=GIZMo.{path}",
                    "parent": path.rsplit(".", 1)[0],
                    "browse_name": node.get_browse_name().Name,
                    "description": localized_text(node.get_description()),
                    "data_type": getattr(variant_type, "name", str(variant_type)),
                    "data_type_node_id": f"i={int(variant_type.value)}",
                    "value_rank": int(node.get_value_rank()),
                    "access_level": access_level,
                    "access": (
                        "ReadWrite"
                        if access_level & ua.AccessLevel.CurrentWrite.mask
                        else "ReadOnly"
                    ),
                    "engineering_unit": unit,
                    "engineering_range": value_range,
                }
            )

        contract: dict[str, Any] = {
            "schema_version": 1,
            "authority": "GIZMo Kria OPC UA implementation",
            "namespace_uri": MODEL_NAMESPACE_URI,
            "model_version": MODEL_VERSION,
            "publication_date": MODEL_PUBLICATION_DATE.isoformat(),
            "extension_policy": (
                "The listed nodes are the stable required baseline. Runtime "
                "inventory may add nodes below Network.Interfaces, "
                "Storage.Filesystems, and Services.Units without changing "
                "existing NodeIds or datatypes."
            ),
            "objects": sorted(objects, key=lambda item: item["path"]),
            "variables": variables,
            "methods": sorted(methods, key=lambda item: item["path"]),
        }
        canonical = json.dumps(
            contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        contract["contract_sha256"] = hashlib.sha256(canonical).hexdigest()
        return contract
    finally:
        server._executor.shutdown(wait=False, cancel_futures=True)


def serialized_contract(repo_root: Path) -> str:
    return json.dumps(
        render_contract(repo_root), indent=2, ensure_ascii=False, sort_keys=True
    ) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("schema/gizmo-opcua-contract.json"),
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    output = args.output if args.output.is_absolute() else repo_root / args.output
    rendered = serialized_contract(repo_root)
    if args.check:
        current = output.read_text(encoding="utf-8") if output.exists() else ""
        if current != rendered:
            raise SystemExit(f"generated OPC UA contract is stale: {output}")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
