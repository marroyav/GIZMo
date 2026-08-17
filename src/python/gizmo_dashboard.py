#!/usr/bin/env python3
"""Read-only live web dashboard for the canonical GIZMo OPC UA model."""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import http.client
import json
import math
import mimetypes
import os
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from opcua import Client, ua
except ModuleNotFoundError:  # Host-side pure helper tests do not need OPC UA.
    Client = None
    ua = None


NAMESPACE_URI = "urn:fnal:gizmo"
LEGACY_NAMESPACE_URI = "SimpleOPCUAServer"
DEFAULT_ENDPOINT = "opc.tcp://127.0.0.1:4840"
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_PUBLISH_INTERVAL = 1.0
DEFAULT_HISTORIAN_SOCKET = "/run/gizmo/historian.sock"
MAX_HISTORY_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_SSE_CLIENTS = 24
MAX_CALIBRATION_ROWS = 4096
HIGH_Z_FLOOR_OHM = float(
    os.environ.get("GIZMO_RESISTANCE_VALID_MAX_OHM", "500")
)


def parse_resistance_calibration(value: Any) -> dict[str, Any]:
    """Parse the legacy four-column resistance calibration payload.

    RCalData is a flattened representation of Rcalibration_ph.csv.  Its
    magnitude column is the lock-in vector magnitude, not a statistical RMS
    over repeated calibration reads.  For the sinusoidal calibration signal,
    magnitude/sqrt(2) is exposed explicitly as an RMS estimate.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("resistance calibration is empty")
    fields = [field.strip() for field in value.replace("\n", ",").split(",")]
    fields = [field for field in fields if field]
    if not fields or len(fields) % 4:
        raise ValueError("resistance calibration must contain four columns")
    row_count = len(fields) // 4
    if row_count > MAX_CALIBRATION_ROWS:
        raise ValueError("resistance calibration exceeds the row limit")

    rows: list[dict[str, float]] = []
    for offset in range(0, len(fields), 4):
        try:
            z_ohm, magnitude, phase_atan, phase_atan2 = (
                float(field) for field in fields[offset : offset + 4]
            )
        except ValueError as error:
            raise ValueError(
                f"resistance calibration row {offset // 4 + 1} is not numeric"
            ) from error
        if not all(
            math.isfinite(field)
            for field in (z_ohm, magnitude, phase_atan, phase_atan2)
        ):
            raise ValueError(
                f"resistance calibration row {offset // 4 + 1} is not finite"
            )
        if z_ohm < 0 or magnitude < 0:
            raise ValueError(
                f"resistance calibration row {offset // 4 + 1} is negative"
            )
        rows.append(
            {
                "z_ohm": z_ohm,
                "lockin_magnitude_count": magnitude,
                "sine_rms_estimate_count": magnitude / math.sqrt(2.0),
                "phase_atan_degrees": phase_atan,
                "phase_atan2_degrees": phase_atan2,
            }
        )

    return {
        "row_count": len(rows),
        "columns": [
            "z_ohm",
            "lockin_magnitude_count",
            "sine_rms_estimate_count",
            "phase_atan_degrees",
            "phase_atan2_degrees",
        ],
        "rms_definition": "lockin_magnitude_count / sqrt(2)",
        "rms_note": (
            "Sinusoidal amplitude estimate in ADC-count units; raw waveform "
            "samples are required for a statistical waveform RMS."
        ),
        "rows": rows,
    }


@dataclasses.dataclass(frozen=True)
class VariableSpec:
    path: str
    label: str
    group: str
    data_type: str
    unit: str = ""
    precision: int = 1
    chartable: bool = False
    description: str = ""

    def public(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def variable(
    path: str,
    label: str,
    group: str,
    data_type: str,
    *,
    unit: str = "",
    precision: int = 1,
    chartable: bool = False,
    description: str = "",
) -> VariableSpec:
    return VariableSpec(
        path=path,
        label=label,
        group=group,
        data_type=data_type,
        unit=unit,
        precision=precision,
        chartable=chartable,
        description=description,
    )


VARIABLES: list[VariableSpec] = [
    variable("Identity.ProductName", "Instrument", "Identity", "String"),
    variable("Identity.ModelVersion", "OPC UA model", "Identity", "String"),
    variable("Identity.RuntimeVersion", "Runtime", "Identity", "String"),
    variable("Identity.Hostname", "Hostname", "Identity", "String"),
    variable("Identity.BootId", "Boot ID", "Identity", "String"),
    variable(
        "Measurement.Sequence",
        "Measurement sequence",
        "Measurement",
        "UInt64",
        precision=0,
    ),
    variable(
        "Measurement.SampleTime",
        "Measurement time",
        "Measurement",
        "DateTime",
    ),
    variable(
        "Measurement.ResistanceOhm",
        "Resistance",
        "Measurement",
        "Double",
        unit="Ω",
        chartable=True,
        description="Equivalent resistance; valid only when range is InRange.",
    ),
    variable(
        "Measurement.ResistanceRange",
        "Resistance range",
        "Measurement",
        "String",
    ),
    variable(
        "Measurement.CapacitanceNanofarad",
        "Capacitance",
        "Measurement",
        "Double",
        unit="nF",
        precision=2,
        chartable=True,
    ),
    variable(
        "Measurement.CapacitanceRange",
        "Capacitance range",
        "Measurement",
        "String",
    ),
    variable(
        "Measurement.ThresholdOhm",
        "Alarm threshold",
        "Measurement",
        "Double",
        unit="Ω",
        chartable=True,
    ),
    variable(
        "Measurement.StimulusFrequencyHertz",
        "Stimulus frequency",
        "Measurement",
        "Double",
        unit="Hz",
        chartable=True,
    ),
    variable(
        "Measurement.StimulusCurrentRmsAmpere",
        "Stimulus current RMS",
        "Measurement",
        "Double",
        unit="A",
        precision=6,
        description=(
            "Reserved physical monitor. Model 1.4 reports BadNotSupported and "
            "no numeric value until its transfer function and RMS conversion "
            "are validated."
        ),
    ),
    variable(
        "Measurement.MagnitudeCount",
        "Lock-in magnitude",
        "Measurement",
        "Double",
        unit="count",
        precision=0,
        chartable=True,
    ),
    variable(
        "Measurement.PhaseAtanDegrees",
        "Phase atan",
        "Measurement",
        "Double",
        unit="°",
        precision=2,
        chartable=True,
    ),
    variable(
        "Measurement.PhaseAtan2Degrees",
        "Phase atan2",
        "Measurement",
        "Double",
        unit="°",
        precision=2,
        chartable=True,
    ),
    variable(
        "Measurement.PhaseInterpolatedDegrees",
        "Interpolated phase",
        "Measurement",
        "Double",
        unit="°",
        precision=2,
        chartable=True,
    ),
    variable(
        "Measurement.InPhaseCount",
        "In-phase component",
        "Measurement",
        "Double",
        unit="count",
        precision=0,
        chartable=True,
    ),
    variable(
        "Measurement.QuadratureCount",
        "Quadrature component",
        "Measurement",
        "Double",
        unit="count",
        precision=0,
        chartable=True,
    ),
    variable(
        "Measurement.AveragesPerCalculation",
        "Averages per calculation",
        "Measurement",
        "UInt32",
        precision=0,
    ),
    variable("Measurement.Quality", "Measurement quality", "Measurement", "String"),
    variable(
        "Measurement.Diagnostic",
        "Measurement diagnostic",
        "Measurement",
        "String",
    ),
    variable("Alarm.Latched", "Alarm latched", "Alarm", "Boolean"),
    variable(
        "Alarm.Active",
        "Composite alarm",
        "Alarm",
        "Boolean",
        precision=0,
        chartable=True,
        description=(
            "Authoritative ZMon relay/beacon alarm decision; displayed and "
            "stored without recomputing resistance or phase rules."
        ),
    ),
    variable("Alarm.Reason", "Alarm reason", "Alarm", "String"),
    variable("Alarm.LatchTime", "Latch time", "Alarm", "DateTime"),
    variable(
        "Operations.CommandGateState",
        "Command gate",
        "Operations",
        "String",
    ),
    variable(
        "Operations.LastCommandId",
        "Last command ID",
        "Operations",
        "String",
    ),
    variable(
        "Operations.LastCommandName",
        "Last command",
        "Operations",
        "String",
    ),
    variable(
        "Operations.LastCommandParameters",
        "Command parameters",
        "Operations",
        "String",
    ),
    variable(
        "Operations.LastCommandRequester",
        "Command requester",
        "Operations",
        "String",
    ),
    variable(
        "Operations.LastCommandState",
        "Command state",
        "Operations",
        "String",
    ),
    variable(
        "Operations.LastCommandRequestTime",
        "Command requested",
        "Operations",
        "DateTime",
    ),
    variable(
        "Operations.LastCommandCompletionTime",
        "Command completed",
        "Operations",
        "DateTime",
    ),
    variable(
        "Operations.LastCommandResult",
        "Command result",
        "Operations",
        "String",
    ),
    variable(
        "Calibration.OperationState",
        "Calibration operation",
        "Calibration",
        "String",
    ),
    variable(
        "Calibration.ProgressPercent",
        "Calibration progress",
        "Calibration",
        "Double",
        unit="%",
    ),
    variable(
        "Calibration.LastOperationTime",
        "Calibration operation time",
        "Calibration",
        "DateTime",
    ),
    variable(
        "Calibration.LastOperationResult",
        "Calibration operation result",
        "Calibration",
        "String",
    ),
    variable(
        "Calibration.RestorationState",
        "Normal-state restoration",
        "Calibration",
        "String",
    ),
    variable(
        "Thermal.ChassisTemperatureCelsius",
        "Chassis temperature",
        "Thermal",
        "Double",
        unit="°C",
        chartable=True,
    ),
    variable(
        "Thermal.CPU1TemperatureCelsius",
        "CPU 1 temperature",
        "Thermal",
        "Double",
        unit="°C",
        chartable=True,
    ),
    variable(
        "Thermal.CPU2TemperatureCelsius",
        "CPU 2 temperature",
        "Thermal",
        "Double",
        unit="°C",
        chartable=True,
    ),
    variable(
        "Thermal.CPU3TemperatureCelsius",
        "CPU 3 temperature",
        "Thermal",
        "Double",
        unit="°C",
        chartable=True,
    ),
    variable("Thermal.Quality", "Thermal quality", "Thermal", "String"),
    variable("Thermal.Diagnostic", "Thermal diagnostic", "Thermal", "String"),
    variable("Thermal.LastUpdate", "Thermal update", "Thermal", "DateTime"),
    variable("Time.CurrentUtc", "Current UTC", "Time", "DateTime"),
    variable("Time.CurrentLocal", "Local time", "Time", "String"),
    variable("Time.BootTime", "Boot time", "Time", "DateTime"),
    variable(
        "Time.UptimeSeconds",
        "Uptime",
        "Time",
        "Double",
        unit="s",
        precision=0,
        chartable=True,
    ),
    variable("Time.TimezoneName", "Timezone", "Time", "String"),
    variable("Time.NtpSynchronized", "NTP synchronized", "Time", "Boolean"),
    variable("Time.NtpServiceActive", "NTP service active", "Time", "Boolean"),
    variable("Time.RtcPresent", "RTC present", "Time", "Boolean"),
    variable("Time.CurrentClocksource", "Clocksource", "Time", "String"),
    variable("Time.Quality", "Time quality", "Time", "String"),
    variable("Time.Diagnostic", "Time diagnostic", "Time", "String"),
    variable("OperatingSystem.PrettyName", "Operating system", "System", "String"),
    variable(
        "OperatingSystem.KernelRelease",
        "Kernel release",
        "System",
        "String",
    ),
    variable(
        "OperatingSystem.LogicalCpuCount",
        "Logical CPUs",
        "System",
        "UInt32",
        precision=0,
    ),
    variable(
        "OperatingSystem.CpuUtilizationPercent",
        "CPU utilization",
        "System",
        "Double",
        unit="%",
        chartable=True,
    ),
    variable(
        "OperatingSystem.Load1Minute",
        "Load average · 1 min",
        "System",
        "Double",
        precision=2,
        chartable=True,
    ),
    variable(
        "OperatingSystem.Load5Minute",
        "Load average · 5 min",
        "System",
        "Double",
        precision=2,
        chartable=True,
    ),
    variable(
        "OperatingSystem.Load15Minute",
        "Load average · 15 min",
        "System",
        "Double",
        precision=2,
        chartable=True,
    ),
    variable(
        "OperatingSystem.MemoryTotalBytes",
        "Memory total",
        "System",
        "UInt64",
        unit="byte",
        precision=0,
    ),
    variable(
        "OperatingSystem.MemoryUsedBytes",
        "Memory used",
        "System",
        "UInt64",
        unit="byte",
        precision=0,
        chartable=True,
    ),
    variable(
        "OperatingSystem.MemoryAvailableBytes",
        "Memory available",
        "System",
        "UInt64",
        unit="byte",
        precision=0,
        chartable=True,
    ),
    variable(
        "OperatingSystem.ProcessCount",
        "Processes",
        "System",
        "UInt32",
        precision=0,
        chartable=True,
    ),
    variable(
        "OperatingSystem.OpenFileHandles",
        "Open file handles",
        "System",
        "UInt64",
        precision=0,
        chartable=True,
    ),
    variable("OperatingSystem.Quality", "System quality", "System", "String"),
    variable(
        "Storage.Filesystems.Root.UsedPercent",
        "Root filesystem used",
        "Storage",
        "Double",
        unit="%",
        chartable=True,
    ),
    variable(
        "Storage.Filesystems.State.UsedPercent",
        "State filesystem used",
        "Storage",
        "Double",
        unit="%",
        chartable=True,
    ),
    variable(
        "Storage.Filesystems.Run.UsedPercent",
        "Runtime filesystem used",
        "Storage",
        "Double",
        unit="%",
        chartable=True,
    ),
    variable("Storage.Quality", "Storage quality", "Storage", "String"),
    variable("Storage.Diagnostic", "Storage diagnostic", "Storage", "String"),
    variable("Firmware.RuntimeVersion", "Firmware runtime", "Firmware", "String"),
    variable("Firmware.OverlayName", "FPGA overlay", "Firmware", "String"),
    variable("Firmware.OverlayLoaded", "Overlay loaded", "Firmware", "Boolean"),
    variable("Firmware.OverlayState", "Overlay state", "Firmware", "String"),
    variable("Firmware.BoardModel", "Board model", "Firmware", "String"),
    variable(
        "Firmware.CarrierProductName",
        "Carrier product",
        "Firmware",
        "String",
    ),
    variable("Firmware.Quality", "Firmware quality", "Firmware", "String"),
    variable("Firmware.Diagnostic", "Firmware diagnostic", "Firmware", "String"),
    variable("SDR.Available", "SDR available", "SDR", "Boolean"),
    variable(
        "SDR.SamplesPerFrame",
        "SDR samples per frame",
        "SDR",
        "UInt32",
        precision=0,
    ),
    variable(
        "SDR.FrameSequence",
        "SDR frame sequence",
        "SDR",
        "UInt64",
        precision=0,
        chartable=True,
    ),
    variable("SDR.SampleTime", "SDR sample time", "SDR", "DateTime"),
    variable("SDR.Quality", "SDR quality", "SDR", "String"),
    variable("SDR.Diagnostic", "SDR diagnostic", "SDR", "String"),
    variable("Health.Overall", "Overall health", "Health", "String"),
    variable("Health.Measurement", "Measurement health", "Health", "String"),
    variable("Health.Thermal", "Thermal health", "Health", "String"),
    variable("Health.Time", "Time health", "Health", "String"),
    variable("Health.OperatingSystem", "System health", "Health", "String"),
    variable("Health.Network", "Network health", "Health", "String"),
    variable("Health.Storage", "Storage health", "Health", "String"),
    variable("Health.Firmware", "Firmware health", "Health", "String"),
    variable("Health.Services", "Services health", "Health", "String"),
    variable("Health.Calibration", "Calibration health", "Health", "String"),
    variable("Health.SDR", "SDR health", "Health", "String"),
    variable("Health.LastUpdate", "Health update", "Health", "DateTime"),
]


for interface in ("eth0", "eth1"):
    prefix = f"Network.Interfaces.{interface}"
    label = interface.upper()
    VARIABLES.extend(
        [
            variable(f"{prefix}.Carrier", f"{label} carrier", "Network", "Boolean"),
            variable(
                f"{prefix}.OperationalState",
                f"{label} state",
                "Network",
                "String",
            ),
            variable(
                f"{prefix}.Addresses",
                f"{label} addresses",
                "Network",
                "String",
            ),
            variable(
                f"{prefix}.MacAddress",
                f"{label} MAC address",
                "Network",
                "String",
            ),
            variable(
                f"{prefix}.MacAddressSource",
                f"{label} MAC source",
                "Network",
                "String",
            ),
            variable(
                f"{prefix}.SpeedMegabitPerSecond",
                f"{label} speed",
                "Network",
                "UInt32",
                unit="Mbit/s",
                precision=0,
            ),
            variable(
                f"{prefix}.RxBytes",
                f"{label} received",
                "Network",
                "UInt64",
                unit="byte",
                precision=0,
                chartable=True,
            ),
            variable(
                f"{prefix}.TxBytes",
                f"{label} transmitted",
                "Network",
                "UInt64",
                unit="byte",
                precision=0,
                chartable=True,
            ),
            variable(
                f"{prefix}.RxErrors",
                f"{label} receive errors",
                "Network",
                "UInt64",
                precision=0,
                chartable=True,
            ),
            variable(
                f"{prefix}.TxErrors",
                f"{label} transmit errors",
                "Network",
                "UInt64",
                precision=0,
                chartable=True,
            ),
        ]
    )


SERVICE_UNITS = (
    "gizmo.target",
    "gizmo-network.service",
    "gizmo-hardware.service",
    "gizmo-control.socket",
    "gizmo-control.service",
    "gizmo-zmon.service",
    "gizmo-display.service",
    "gizmo-temperature.service",
    "gizmo-sdr.service",
    "gizmo-zmq.service",
    "gizmo-opcua.service",
    "gizmo-historian.service",
    "gizmo-dashboard.service",
)


def service_node_key(service: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in service)


for service in SERVICE_UNITS:
    prefix = f"Services.Units.{service_node_key(service)}"
    short_name = service.removeprefix("gizmo-").removesuffix(".service")
    VARIABLES.extend(
        [
            variable(
                f"{prefix}.ActiveState",
                f"{short_name} active state",
                "Services",
                "String",
            ),
            variable(
                f"{prefix}.SubState",
                f"{short_name} sub-state",
                "Services",
                "String",
            ),
            variable(
                f"{prefix}.Result",
                f"{short_name} result",
                "Services",
                "String",
            ),
            variable(
                f"{prefix}.RestartCount",
                f"{short_name} restarts",
                "Services",
                "UInt32",
                precision=0,
                chartable=True,
            ),
        ]
    )


CATALOG = {item.path: item for item in VARIABLES}
if len(CATALOG) != len(VARIABLES):
    raise RuntimeError("duplicate dashboard variable path")


CHART_VIEWS = [
    {
        "id": "alarm",
        "label": "Alarm",
        "unit": "state",
        "paths": ["Alarm.Active"],
    },
    {
        "id": "impedance",
        "label": "Impedance",
        "unit": "Ω",
        "paths": [
            "Measurement.ResistanceOhm",
            "Measurement.ThresholdOhm",
        ],
    },
    {
        "id": "thermal",
        "label": "Thermal",
        "unit": "°C",
        "paths": [
            "Thermal.ChassisTemperatureCelsius",
            "Thermal.CPU1TemperatureCelsius",
            "Thermal.CPU2TemperatureCelsius",
            "Thermal.CPU3TemperatureCelsius",
        ],
    },
    {
        "id": "lockin",
        "label": "Lock-in",
        "unit": "count",
        "paths": [
            "Measurement.MagnitudeCount",
            "Measurement.InPhaseCount",
            "Measurement.QuadratureCount",
        ],
    },
    {
        "id": "phase",
        "label": "Phase",
        "unit": "°",
        "paths": [
            "Measurement.PhaseAtanDegrees",
            "Measurement.PhaseAtan2Degrees",
            "Measurement.PhaseInterpolatedDegrees",
        ],
    },
    {
        "id": "system",
        "label": "System",
        "unit": "%",
        "paths": [
            "OperatingSystem.CpuUtilizationPercent",
            "Storage.Filesystems.Root.UsedPercent",
            "Storage.Filesystems.State.UsedPercent",
        ],
    },
]


STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
HISTORY_ROUTES = {
    "/api/history/status": "/status",
    "/api/history/series": "/series",
    "/api/history/query": "/query",
    "/api/history/events": "/events",
    "/api/history/export.csv": "/export.csv",
    "/api/history/replication": "/replication",
}


def json_value(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def status_name(status: Any) -> str:
    return getattr(status, "name", str(status))


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float = 10.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self.socket_path))
        self.sock = connection


class SubscriptionHandler:
    def __init__(self, monitor: "OpcUaMonitor") -> None:
        self.monitor = monitor

    def datachange_notification(self, node: Any, value: Any, data: Any) -> None:
        data_value = getattr(getattr(data, "monitored_item", None), "Value", None)
        self.monitor.record_datachange(node, value, data_value)

    def event_notification(self, event: Any) -> None:
        del event


class OpcUaMonitor:
    """One resilient OPC UA subscription shared by every HTTP client."""

    def __init__(self, endpoint: str, subscription_interval_ms: int) -> None:
        self.endpoint = endpoint
        self.subscription_interval_ms = subscription_interval_ms
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._subscription: Any = None
        self._nodes: list[Any] = []
        self._node_paths: dict[str, str] = {}
        self._calibration_node_id: str | None = None
        self._values = {
            path: {
                "value": None,
                "status": "BadWaitingForInitialData",
                "source_timestamp": None,
                "server_timestamp": None,
                "received_at": None,
            }
            for path in CATALOG
        }
        self._connected = False
        self._namespace_index: int | None = None
        self._connected_at: str | None = None
        self._last_notification: str | None = None
        self._error = "OPC UA connection has not started"
        self._sequence = 0
        self._resistance_calibration: dict[str, Any] = {
            "status": "BadWaitingForInitialData",
            "source_timestamp": None,
            "received_at": None,
            "error": "resistance calibration has not been read",
            "row_count": 0,
            "rows": [],
        }

    def start(self) -> None:
        if Client is None or ua is None:
            raise RuntimeError("python-opcua is required by gizmo-dashboard")
        self._thread = threading.Thread(
            target=self._run,
            name="gizmo-opcua-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=8)
        self._disconnect()

    def _mark_disconnected(self, error: str) -> None:
        with self._lock:
            self._connected = False
            self._error = error
            if self._resistance_calibration.get("rows"):
                self._resistance_calibration["status"] = (
                    "UncertainLastUsableValue"
                )
            self._sequence += 1

    def _disconnect(self) -> None:
        subscription, client = self._subscription, self._client
        self._subscription = None
        self._client = None
        self._nodes = []
        self._calibration_node_id = None
        if subscription is not None:
            try:
                subscription.delete()
            except Exception:
                pass
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

    def _connect(self) -> None:
        client = Client(self.endpoint, timeout=5)
        client.connect()
        with self._lock:
            self._client = client
        namespace_index = client.get_namespace_index(NAMESPACE_URI)
        calibration_node = None
        try:
            legacy_index = client.get_namespace_index(LEGACY_NAMESPACE_URI)
            calibration_node = client.get_objects_node().get_child(
                [
                    f"{legacy_index}:CommandObject",
                    f"{legacy_index}:RCalData",
                ]
            )
            calibration_value = calibration_node.get_data_value()
            self._record_resistance_calibration(
                calibration_value.Value.Value,
                status_name(calibration_value.StatusCode),
                json_value(calibration_value.SourceTimestamp),
            )
        except Exception as error:
            calibration_node = None
            self._record_resistance_calibration_error(
                f"{type(error).__name__}: {error}"
            )
        candidate_nodes = []
        node_paths: dict[str, str] = {}
        for spec in VARIABLES:
            node = client.get_node(
                ua.NodeId(f"GIZMo.{spec.path}", namespace_index)
            )
            candidate_nodes.append(node)
            node_paths[node.nodeid.to_string()] = spec.path

        # Probe NodeClass rather than Value status: a valid live node may
        # legitimately carry BadNoCommunication or UncertainLastUsableValue.
        # Filtering only unknown NodeIds allows the web package and OPC UA
        # model to be upgraded independently without turning one optional
        # variable into a reconnect loop for the entire dashboard.
        existence_results = client.uaclient.get_attributes(
            [node.nodeid for node in candidate_nodes],
            ua.AttributeIds.NodeClass,
        )
        nodes = []
        if len(candidate_nodes) != len(existence_results):
            raise RuntimeError(
                "OPC UA existence result count does not match the requested nodes"
            )
        for node, result in zip(candidate_nodes, existence_results):
            path = node_paths[node.nodeid.to_string()]
            if result.StatusCode.is_good():
                nodes.append(node)
                continue
            with self._lock:
                self._values[path] = {
                    "value": None,
                    "status": status_name(result.StatusCode),
                    "source_timestamp": None,
                    "server_timestamp": None,
                    "received_at": utc_now(),
                }

        # Seed the cache with one direct read before subscribing.  Some OPC UA
        # stacks omit the Variant from a data-change notification carrying
        # UncertainLastUsableValue; the direct read retains that last value.
        with self._lock:
            self._nodes = nodes
            self._node_paths = node_paths
            self._calibration_node_id = (
                calibration_node.nodeid.to_string()
                if calibration_node is not None
                else None
            )
        self._refresh_values(client, nodes)

        subscription = client.create_subscription(
            self.subscription_interval_ms,
            SubscriptionHandler(self),
        )
        with self._lock:
            self._client = client
            self._subscription = subscription
            self._namespace_index = namespace_index
            self._connected = True
            self._connected_at = utc_now()
            self._error = ""
            self._sequence += 1
        try:
            subscription_nodes = [*nodes]
            if calibration_node is not None:
                subscription_nodes.append(calibration_node)
            subscription.subscribe_data_change(subscription_nodes)
        except Exception:
            with self._lock:
                self._connected = False
            raise

    def _refresh_values(self, client: Any, nodes: list[Any]) -> None:
        """Reconcile status-only changes that some SDKs do not notify."""
        results = client.uaclient.get_attributes(
            [node.nodeid for node in nodes],
            ua.AttributeIds.Value,
        )
        received_at = utc_now()
        with self._lock:
            if len(nodes) != len(results):
                raise RuntimeError(
                    "OPC UA read result count does not match the requested nodes"
                )
            for node, result in zip(nodes, results):
                path = self._node_paths[node.nodeid.to_string()]
                value = json_value(result.Value.Value)
                status = status_name(result.StatusCode)
                if (
                    value is None
                    and status.startswith("Uncertain")
                    and self._values[path]["value"] is not None
                ):
                    value = self._values[path]["value"]
                self._values[path] = {
                    "value": value,
                    "status": status,
                    "source_timestamp": json_value(result.SourceTimestamp),
                    "server_timestamp": json_value(result.ServerTimestamp),
                    "received_at": received_at,
                }
            self._sequence += 1

    def _record_resistance_calibration_error(self, error: str) -> None:
        with self._lock:
            retained = bool(self._resistance_calibration.get("rows"))
            self._resistance_calibration["status"] = (
                "UncertainLastUsableValue"
                if retained
                else "BadDataUnavailable"
            )
            self._resistance_calibration["error"] = error
            self._resistance_calibration["received_at"] = utc_now()

    def _record_resistance_calibration(
        self,
        value: Any,
        status: str,
        source_timestamp: Any,
    ) -> None:
        received_at = utc_now()
        try:
            parsed = parse_resistance_calibration(value)
        except ValueError as error:
            self._record_resistance_calibration_error(str(error))
            return
        with self._lock:
            self._resistance_calibration = {
                **parsed,
                "status": status,
                "source_timestamp": source_timestamp,
                "received_at": received_at,
                "error": "",
            }

    def resistance_calibration(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._resistance_calibration)

    def _run(self) -> None:
        retry_delay = 1.0
        while not self._stop.is_set():
            try:
                self._connect()
                retry_delay = 1.0
                while not self._stop.wait(5.0):
                    # One batched local Read is both a session keepalive and a
                    # reconciliation for status-only changes.
                    self._refresh_values(self._client, self._nodes)
            except Exception as error:
                self._mark_disconnected(f"{type(error).__name__}: {error}")
            finally:
                self._disconnect()
            if self._stop.wait(retry_delay):
                break
            retry_delay = min(retry_delay * 2.0, 15.0)

    def record_datachange(
        self,
        node: Any,
        value: Any,
        data_value: Any,
    ) -> None:
        node_id = node.nodeid.to_string()
        if node_id == self._calibration_node_id:
            status = "Good"
            source_timestamp = None
            if data_value is not None:
                status = status_name(data_value.StatusCode)
                source_timestamp = json_value(data_value.SourceTimestamp)
            self._record_resistance_calibration(
                value,
                status,
                source_timestamp,
            )
            return
        path = self._node_paths.get(node_id)
        if path is None:
            return
        status = "Good"
        source_timestamp = None
        server_timestamp = None
        if data_value is not None:
            status = status_name(data_value.StatusCode)
            source_timestamp = json_value(data_value.SourceTimestamp)
            server_timestamp = json_value(data_value.ServerTimestamp)
        received_at = utc_now()
        with self._lock:
            if (
                value is None
                and status.startswith("Uncertain")
                and self._values[path]["value"] is not None
            ):
                value = self._values[path]["value"]
            self._values[path] = {
                "value": json_value(value),
                "status": status,
                "source_timestamp": source_timestamp,
                "server_timestamp": server_timestamp,
                "received_at": received_at,
            }
            self._last_notification = received_at
            self._sequence += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            values = {path: payload.copy() for path, payload in self._values.items()}
            return {
                "sequence": self._sequence,
                "generated_at": utc_now(),
                "connection": {
                    "connected": self._connected,
                    "endpoint": self.endpoint,
                    "namespace_uri": NAMESPACE_URI,
                    "namespace_index": self._namespace_index,
                    "connected_at": self._connected_at,
                    "last_notification": self._last_notification,
                    "error": self._error,
                },
                "values": values,
            }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        monitor: OpcUaMonitor,
        asset_root: Path,
        publish_interval: float,
        historian_socket: Path | None = None,
    ) -> None:
        super().__init__(server_address, handler)
        self.monitor = monitor
        self.asset_root = asset_root
        self.publish_interval = publish_interval
        self.historian_socket = historian_socket
        self.sse_slots = threading.BoundedSemaphore(MAX_SSE_CLIENTS)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "GIZMoDashboard/1.0"
    sys_version = ""

    @property
    def dashboard(self) -> DashboardServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            json.dumps(
                {
                    "time": utc_now(),
                    "client": self.client_address[0],
                    "request": fmt % args,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )

    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, route: str) -> None:
        filename, content_type = STATIC_ROUTES[route]
        path = self.dashboard.asset_root / filename
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self._common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _catalog(self) -> dict[str, Any]:
        return {
            "namespace_uri": NAMESPACE_URI,
            "variables": [item.public() for item in VARIABLES],
            "views": CHART_VIEWS,
            "service_units": list(SERVICE_UNITS),
            "resistance_high_z_floor_ohm": HIGH_Z_FLOOR_OHM,
            "history_available": bool(
                self.dashboard.historian_socket
                and self.dashboard.historian_socket.is_socket()
            ),
        }

    def _history(self, route: str, query: str) -> None:
        socket_path = self.dashboard.historian_socket
        if socket_path is None or not socket_path.is_socket():
            self._json(
                {"error": "persistent history is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        target = HISTORY_ROUTES[route]
        if query:
            target = f"{target}?{query}"
        connection = UnixHTTPConnection(socket_path)
        try:
            connection.request("GET", target, headers={"Accept": "*/*"})
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None and int(length) > MAX_HISTORY_RESPONSE_BYTES:
                self._json(
                    {"error": "history response exceeds dashboard limit"},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            body = response.read(MAX_HISTORY_RESPONSE_BYTES + 1)
            if len(body) > MAX_HISTORY_RESPONSE_BYTES:
                self._json(
                    {"error": "history response exceeds dashboard limit"},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            self.send_response(response.status)
            self._common_headers()
            self.send_header(
                "Content-Type",
                response.getheader(
                    "Content-Type",
                    "application/octet-stream",
                ),
            )
            self.send_header("Cache-Control", "no-store")
            disposition = response.getheader("Content-Disposition")
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            self._json(
                {"error": f"persistent history is unavailable: {error}"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        finally:
            connection.close()

    def _stream(self) -> None:
        if not self.dashboard.sse_slots.acquire(blocking=False):
            self._json(
                {"error": "too many live dashboard clients"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            self.send_response(HTTPStatus.OK)
            self._common_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                payload = json.dumps(
                    self.dashboard.monitor.snapshot(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                frame = f"event: sample\ndata: {payload}\n\n".encode()
                self.wfile.write(frame)
                self.wfile.flush()
                time.sleep(self.dashboard.publish_interval)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.dashboard.sse_slots.release()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        split = urlsplit(self.path)
        route = split.path
        if route in STATIC_ROUTES:
            self._static(route)
        elif route == "/api/catalog":
            self._json(self._catalog())
        elif route == "/api/state":
            self._json(self.dashboard.monitor.snapshot())
        elif route == "/api/calibration/resistance":
            calibration_reader = getattr(
                self.dashboard.monitor,
                "resistance_calibration",
                None,
            )
            calibration = (
                calibration_reader()
                if callable(calibration_reader)
                else {
                    "status": "BadNotSupported",
                    "error": "resistance calibration is unavailable",
                    "row_count": 0,
                    "rows": [],
                }
            )
            self._json(
                {
                    "source": (
                        "OPC UA SimpleOPCUAServer/CommandObject/RCalData"
                    ),
                    "validated_max_z_ohm": HIGH_Z_FLOOR_OHM,
                    **calibration,
                },
                (
                    HTTPStatus.OK
                    if calibration.get("rows")
                    else HTTPStatus.SERVICE_UNAVAILABLE
                ),
            )
        elif route == "/api/stream":
            self._stream()
        elif route in HISTORY_ROUTES:
            self._history(route, split.query)
        elif route == "/healthz":
            snapshot = self.dashboard.monitor.snapshot()
            connected = snapshot["connection"]["connected"]
            self._json(
                {
                    "status": "ok" if connected else "degraded",
                    "opcua_connected": connected,
                    "historian_available": bool(
                        self.dashboard.historian_socket
                        and self.dashboard.historian_socket.is_socket()
                    ),
                    "generated_at": snapshot["generated_at"],
                },
                HTTPStatus.OK,
            )
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlsplit(self.path).path
        if route not in STATIC_ROUTES:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        filename, content_type = STATIC_ROUTES[route]
        path = self.dashboard.asset_root / filename
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self._common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(size))
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._json(
            {"error": "dashboard API is read-only"},
            HTTPStatus.METHOD_NOT_ALLOWED,
        )


def default_asset_root() -> Path:
    installed = Path("/usr/share/gizmo/dashboard")
    if installed.is_dir():
        return installed
    return Path(__file__).resolve().parents[2] / "web" / "dashboard"


def main() -> None:
    endpoint = os.environ.get("GIZMO_DASHBOARD_OPCUA_ENDPOINT", DEFAULT_ENDPOINT)
    bind = os.environ.get("GIZMO_DASHBOARD_BIND", DEFAULT_BIND)
    port = int(os.environ.get("GIZMO_DASHBOARD_PORT", str(DEFAULT_PORT)))
    subscription_ms = int(
        os.environ.get("GIZMO_DASHBOARD_SUBSCRIPTION_MS", "500")
    )
    publish_interval = float(
        os.environ.get(
            "GIZMO_DASHBOARD_PUBLISH_INTERVAL_SECONDS",
            str(DEFAULT_PUBLISH_INTERVAL),
        )
    )
    asset_root = Path(
        os.environ.get("GIZMO_DASHBOARD_ASSET_ROOT", str(default_asset_root()))
    )
    historian_socket = Path(
        os.environ.get(
            "GIZMO_DASHBOARD_HISTORIAN_SOCKET",
            DEFAULT_HISTORIAN_SOCKET,
        )
    )
    if not asset_root.is_dir():
        raise SystemExit(f"dashboard asset directory is missing: {asset_root}")
    mimetypes.init()
    monitor = OpcUaMonitor(endpoint, subscription_ms)
    monitor.start()
    server = DashboardServer(
        (bind, port),
        DashboardHandler,
        monitor,
        asset_root,
        publish_interval,
        historian_socket,
    )
    print(
        f"GIZMo dashboard listening on http://{bind}:{port}; OPC UA {endpoint}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        monitor.stop()


if __name__ == "__main__":
    main()
