#!/usr/bin/env python3
"""Canonical OPC UA server and legacy compatibility bridge for GIZMo."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import zmq
from gizmo_common import atomic_write, notify_systemd, read_exported_int, state_path
from gizmo_security import CredentialStore, secure_file_mode
from gizmo_model import (
    LEGACY_NAMESPACE_URI,
    MODEL_NAMESPACE_URI,
    MODEL_PUBLICATION_DATE,
    MODEL_VERSION,
    QUALITY_BAD,
    QUALITY_GOOD,
    QUALITY_NOT_AVAILABLE,
    QUALITY_UNCERTAIN,
    RANGE_OUT_OF_RANGE,
    CalibrationSnapshot,
    FirmwareSnapshot,
    HostSnapshot,
    MeasurementSnapshot,
    NetworkSnapshot,
    ServiceInventorySnapshot,
    StorageSnapshot,
    SYSTEMD_UNITS,
    SystemCollectors,
    THRESHOLD_MAX_OHM,
    THRESHOLD_MIN_OHM,
    ThermalSnapshot,
    TimeSnapshot,
    parse_legacy_measurement,
    parse_legacy_thermals,
    runtime_version,
    utc_now,
)
from opcua import Server, ua
from opcua.server.internal_server import InternalServer, InternalSession
from opcua.server.user_manager import UserManager

ENDPOINT = os.environ.get("GIZMO_OPCUA_ENDPOINT", "opc.tcp://0.0.0.0:4840")
APPLICATION_URI = os.environ.get("GIZMO_OPCUA_APPLICATION_URI", "urn:fnal:gizmo:server")
ZMQ_ENDPOINT = os.environ.get("GIZMO_ZMQ_CLIENT_ENDPOINT", "tcp://127.0.0.1:5555")
TEMPERATURE_HOST = os.environ.get("GIZMO_TEMPERATURE_CLIENT_HOST", "127.0.0.1")
TEMPERATURE_PORT = int(os.environ.get("GIZMO_TEMPERATURE_PORT", "5005"))
SDR_HOST = os.environ.get("GIZMO_SDR_CLIENT_HOST", "127.0.0.1")
SDR_PORT = int(os.environ.get("GIZMO_SDR_PORT", "5556"))
SDR_SAMPLE_COUNT = int(os.environ.get("GIZMO_SDR_SAMPLE_COUNT", "2048"))
REQUEST_TIMEOUT_MS = int(os.environ.get("GIZMO_BRIDGE_TIMEOUT_MS", "3000"))
MEASUREMENT_INTERVAL = float(os.environ.get("GIZMO_OPCUA_MEASUREMENT_INTERVAL", "1"))
PLATFORM_INTERVAL = float(os.environ.get("GIZMO_OPCUA_PLATFORM_INTERVAL", "10"))
INVENTORY_INTERVAL = float(os.environ.get("GIZMO_OPCUA_INVENTORY_INTERVAL", "30"))
SDR_INTERVAL = float(os.environ.get("GIZMO_OPCUA_SDR_INTERVAL", "1"))

_MISSING_DATETIME = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
_UNIT_IDS = {
    "Ohm": 1,
    "nF": 2,
    "Hz": 3,
    "deg": 4,
    "Cel": 5,
    "%": 6,
    "s": 7,
    "byte": 8,
    "Mbit/s": 9,
    "A": 10,
}

# These objects are the stable, minimum inventory in the public OPC UA
# contract. Runtime discovery may add further interfaces or filesystems, but
# both supported GIZMo implementations always expose this baseline.
CONTRACT_NETWORK_INTERFACES = ("lo", "eth0", "eth1")
CONTRACT_FILESYSTEMS = ("Root", "Run", "State")

COMMAND_GATE_MODES = frozenset({"disabled", "operator", "maintenance"})
COMMAND_STATES = frozenset(
    {"Idle", "Accepted", "Running", "Succeeded", "Failed", "Rejected", "TimedOut"}
)
MAINTENANCE_METHODS = frozenset(
    {
        "Operations.StartCalibration",
        "Operations.CaptureAdc",
        "Operations.NormalizeMagnitude",
        "Operations.SetSystemTime",
        "Operations.RestartMeasurementEngine",
        "Operations.AbortCalibration",
        "Operations.RestoreNormalState",
    }
)
CANONICAL_METHODS = frozenset({"Operations.ClearLatch", *MAINTENANCE_METHODS})
CANONICAL_WRITES = {
    "Configuration.ThresholdOhm": "ThresholdOhm",
    "Configuration.AveragesPerCalculation": "AveragesPerCalculation",
}
COMMAND_RESULT_LIMIT = 512
COMMAND_PARAMETER_LIMIT = 256
COMMAND_REQUESTER_LIMIT = 128
ZMON_BINARY = Path(os.environ.get("GIZMO_ZMON_BINARY", "/usr/bin/gizmo-zmon"))


@dataclass
class ActiveCommand:
    """One serialized mutation awaiting dispatch or readback verification."""

    command_id: str
    name: str
    requester: str
    role: str
    parameters: str
    requested_at: dt.datetime
    deadline_monotonic: float
    verification: str = "dispatching"
    start_sequence: int = 0
    start_pid: int = 0
    expected_hash: str = ""
    expected_value: object | None = None
    terminal_calibration_state: str = ""
    accepted_result: str = ""
    restore_attempted: bool = False


class GizmoInternalSession(InternalSession):
    """Pinned python-opcua session with a real mutation authorization hook.

    python-opcua 0.98.13 checks variable access bits but does not pass the
    session identity to Method callbacks.  This session subclass keeps reads
    unchanged and routes remote Writes/Calls through the GIZMo policy before
    the library touches the address space.  The package pins that dependency,
    and integration tests exercise this boundary.
    """

    gizmo_requester = "anonymous"
    gizmo_role = "anonymous"

    def write(self, params: object) -> list[ua.StatusCode]:
        if self.user == UserManager.User.Admin:
            return super().write(params)
        owner = getattr(self.iserver, "gizmo_owner", None)
        if owner is None:
            return [
                ua.StatusCode(ua.StatusCodes.BadOutOfService)
                for _ in params.NodesToWrite
            ]
        return owner._remote_write(self, params)

    def call(self, params: object) -> list[ua.CallMethodResult]:
        if self.user == UserManager.User.Admin:
            return super().call(params)
        owner = getattr(self.iserver, "gizmo_owner", None)
        if owner is None:
            return [
                GizmoOpcUaServer._call_error(ua.StatusCodes.BadOutOfService)
                for _ in params
            ]
        return owner._remote_call(self, params)


def configured_threshold() -> int:
    value = read_exported_int("setThreshold.env", "threshold", 100)
    if not THRESHOLD_MIN_OHM <= value <= THRESHOLD_MAX_OHM:
        raise ValueError(
            f"stored threshold must be between {THRESHOLD_MIN_OHM} "
            f"and {THRESHOLD_MAX_OHM}"
        )
    return value


class GizmoOpcUaServer:
    """One browsable, typed OPC UA address space for the complete instrument."""

    def __init__(self) -> None:
        self._context = zmq.Context.instance()
        self._request_lock = threading.Lock()
        self._command_lock = threading.RLock()
        self._command_context = threading.local()
        self._executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="gizmo-opcua"
        )
        self._collectors = SystemCollectors()
        self._jobs: dict[str, Future[object]] = {}
        self._next_run: dict[str, float] = {}
        self._attempted: set[str] = set()
        self._has_data: set[str] = set()
        self._category_quality: dict[str, str] = {}
        self._points: dict[str, tuple[object, object, object]] = {}
        self._interface_points: dict[str, dict[str, str]] = {}
        self._filesystem_points: dict[str, dict[str, str]] = {}
        self._service_points: dict[str, dict[str, str]] = {}
        self._calibration_points: dict[str, dict[str, str]] = {}
        self._measurement_sequence = 0
        self._sdr_sequence = 0
        self._adc_ready_at: float | None = None
        self._active_command: ActiveCommand | None = None
        self._last_good_measurement_sequence = 0
        self._last_good_measurement_at: dt.datetime | None = None
        self._last_measurement_snapshot: MeasurementSnapshot | None = None
        self._zmon_pid = 0
        self._latest_time_snapshot: TimeSnapshot | None = None
        self._command_fault_locked = False
        self._command_state_path = Path(
            os.environ.get(
                "GIZMO_OPCUA_COMMAND_STATE_FILE",
                str(state_path("opcua-command-state.json")),
            )
        )
        self._configured_gate_mode = os.environ.get(
            "GIZMO_OPCUA_COMMAND_GATE", "disabled"
        ).strip().lower()
        self._command_policy_reason = ""
        if self._configured_gate_mode not in COMMAND_GATE_MODES:
            self._command_policy_reason = (
                f"invalid GIZMO_OPCUA_COMMAND_GATE={self._configured_gate_mode!r}"
            )
            self._configured_gate_mode = "disabled"
        self._credential_path = Path(
            os.environ.get(
                "GIZMO_OPCUA_CREDENTIAL_FILE", "/etc/gizmo/opcua-users"
            )
        )
        try:
            if self._credential_path.exists() and not secure_file_mode(
                self._credential_path
            ):
                raise RuntimeError(
                    "credential file must not be a symlink or world-accessible"
                )
            self._credentials = CredentialStore.load(self._credential_path)
        except RuntimeError as error:
            self._credentials = CredentialStore()
            self._command_policy_reason = str(error)
        self._effective_gate_mode = "disabled"
        self._running = True
        self._ready_sent = False
        self._last_notified_health = ""
        self._started_at = utc_now()

        internal_server = InternalServer(session_cls=GizmoInternalSession)
        self.server = Server(iserver=internal_server)
        self.server.iserver.gizmo_owner = self
        self.server.set_endpoint(ENDPOINT)
        self.server.set_server_name("Fermilab GIZMo OPC UA Server")
        self.server.set_application_uri(APPLICATION_URI)
        version = runtime_version()
        self.server.set_build_info(
            MODEL_NAMESPACE_URI,
            "Fermi National Accelerator Laboratory",
            "GIZMo Kria Runtime",
            version,
            os.environ.get("GIZMO_BUILD_ID", ""),
            self._started_at,
        )
        self._configure_security()

        # Register the recovered URI first so its namespace index remains
        # compatible with the legacy bridge. New clients resolve the canonical
        # namespace by URI instead of assuming an index.
        self.legacy_namespace = self.server.register_namespace(LEGACY_NAMESPACE_URI)
        self.namespace = self.server.register_namespace(MODEL_NAMESPACE_URI)

        self._build_legacy_model()
        self._build_canonical_model()
        self._mark_initial_values()
        self._restore_command_state()

        threshold = configured_threshold()
        run_interval = read_exported_int("setRunInterval.env", "runInterval", 100)
        self._last_threshold = threshold
        self._last_run_interval = run_interval
        self._last_time = self.legacy_set_time.get_value()

    def _configure_security(self) -> None:
        certificate = os.environ.get("GIZMO_OPCUA_CERTIFICATE", "").strip()
        private_key = os.environ.get("GIZMO_OPCUA_PRIVATE_KEY", "").strip()
        allow_insecure = (
            os.environ.get("GIZMO_OPCUA_ALLOW_INSECURE", "0").strip() == "1"
        )
        allow_insecure_credentials = (
            os.environ.get("GIZMO_OPCUA_ALLOW_INSECURE_CREDENTIALS", "0").strip()
            == "1"
        )
        if bool(certificate) != bool(private_key):
            raise RuntimeError(
                "both GIZMO_OPCUA_CERTIFICATE and GIZMO_OPCUA_PRIVATE_KEY "
                "must be configured together"
            )
        policies = []
        if certificate and private_key:
            self.server.load_certificate(certificate)
            self.server.load_private_key(private_key)
            policies.extend(
                [
                    ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
                    ua.SecurityPolicyType.Basic256Sha256_Sign,
                ]
            )
        elif not allow_insecure:
            raise RuntimeError(
                "GIZMO_OPCUA_ALLOW_INSECURE=0 requires a certificate and private key"
            )
        if allow_insecure:
            policies.append(ua.SecurityPolicyType.NoSecurity)
        self.server.set_security_policy(policies)
        self.server.user_manager.set_user_manager(self._authenticate_user)
        self.server.allow_remote_admin(False)
        identities = ["Anonymous"]
        if self._credentials:
            identities.append("Username")
        self.server.set_security_IDs(identities)

        secure_only = bool(certificate and private_key and not allow_insecure)
        credential_transport_accepted = secure_only or allow_insecure_credentials
        if self._configured_gate_mode == "disabled":
            self._effective_gate_mode = "disabled"
            self._command_policy_reason = (
                self._command_policy_reason or "command gate is disabled by configuration"
            )
        elif not self._credentials:
            self._effective_gate_mode = "disabled"
            self._command_policy_reason = (
                self._command_policy_reason
                or f"no valid credentials in {self._credential_path}"
            )
        elif not credential_transport_accepted:
            self._effective_gate_mode = "disabled"
            self._command_policy_reason = (
                "username commands require secure-only OPC UA transport or "
                "an explicit isolated-network insecure-credential exception"
            )
        else:
            self._effective_gate_mode = self._configured_gate_mode
            self._command_policy_reason = ""

    def _authenticate_user(
        self, session: GizmoInternalSession, username: str, password: str
    ) -> bool:
        role = self._credentials.authenticate(str(username), str(password))
        if role is None:
            return False
        session.user = UserManager.User.User
        session.gizmo_requester = self._bounded(str(username), COMMAND_REQUESTER_LIMIT)
        session.gizmo_role = role
        return True

    @staticmethod
    def _safe_identifier(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
        return normalized or hashlib.sha256(value.encode()).hexdigest()[:12]

    def _node_id(self, path: str) -> ua.NodeId:
        return ua.NodeId(f"GIZMo.{path}", self.namespace)

    def _browse_name(self, name: str) -> ua.QualifiedName:
        return ua.QualifiedName(name, self.namespace)

    @staticmethod
    def _set_description(node: object, description: str) -> None:
        value = ua.DataValue(
            ua.Variant(ua.LocalizedText(description), ua.VariantType.LocalizedText)
        )
        node.set_attribute(ua.AttributeIds.Description, value)

    def _add_object(
        self, parent: object, path: str, name: str, description: str
    ) -> object:
        node = parent.add_object(self._node_id(path), self._browse_name(name))
        self._set_description(node, description)
        return node

    def _unit_information(self, symbol: str) -> ua.EUInformation:
        unit = ua.EUInformation()
        unit.NamespaceUri = f"{MODEL_NAMESPACE_URI}:units"
        unit.UnitId = _UNIT_IDS[symbol]
        unit.DisplayName = ua.LocalizedText(symbol)
        unit.Description = ua.LocalizedText(symbol)
        return unit

    def _add_point(
        self,
        parent: object,
        path: str,
        name: str,
        default: object,
        variant_type: object,
        description: str,
        *,
        unit: str | None = None,
        value_range: tuple[float, float] | None = None,
        writable: bool = False,
    ) -> object:
        node = parent.add_variable(
            self._node_id(path),
            self._browse_name(name),
            default,
            variant_type,
        )
        self._set_description(node, description)
        if writable:
            node.set_writable()
        if unit is not None:
            node.add_property(
                ua.NodeId(f"GIZMo.{path}.EngineeringUnits", self.namespace),
                ua.QualifiedName("EngineeringUnits", 0),
                self._unit_information(unit),
                datatype=ua.NodeId(ua.ObjectIds.EUInformation, 0),
            )
        if value_range is not None:
            engineering_range = ua.Range()
            engineering_range.Low = value_range[0]
            engineering_range.High = value_range[1]
            node.add_property(
                ua.NodeId(f"GIZMo.{path}.EURange", self.namespace),
                ua.QualifiedName("EURange", 0),
                engineering_range,
                datatype=ua.NodeId(ua.ObjectIds.Range, 0),
            )
        self._points[path] = (node, variant_type, default)
        return node

    def _add_method(
        self,
        parent: object,
        path: str,
        name: str,
        callback: Callable[..., object],
        inputs: list[ua.Argument],
        description: str,
    ) -> object:
        output = self._method_argument(
            "Result",
            ua.VariantType.String,
            "Human-readable operation result.",
        )
        node = parent.add_method(
            self._node_id(path),
            self._browse_name(name),
            callback,
            inputs,
            [output],
        )
        self._set_description(node, description)
        return node

    @staticmethod
    def _method_argument(
        name: str, variant_type: ua.VariantType, description: str
    ) -> ua.Argument:
        argument = ua.Argument()
        argument.Name = name
        argument.DataType = ua.NodeId(variant_type.value, 0)
        argument.ValueRank = ua.ValueRank.Scalar
        argument.ArrayDimensions = []
        argument.Description = ua.LocalizedText(description)
        return argument

    def _build_legacy_model(self) -> None:
        command = self.server.get_objects_node().add_object(
            self.legacy_namespace, "CommandObject"
        )
        command.add_method(
            self.legacy_namespace,
            "send_command",
            self.send_legacy_command,
            [ua.VariantType.String],
            [ua.VariantType.String],
        )
        threshold = configured_threshold()
        run_interval = read_exported_int("setRunInterval.env", "runInterval", 100)
        self.legacy_threshold = command.add_variable(
            self.legacy_namespace, "set_th", threshold
        )
        self.legacy_threshold.set_writable()
        self.legacy_data = command.add_variable(self.legacy_namespace, "data", "")
        self.legacy_set_time = command.add_variable(
            self.legacy_namespace, "set_time", ""
        )
        self.legacy_set_time.set_writable()
        self.legacy_clear_latch = command.add_variable(
            self.legacy_namespace, "clear_latch", ""
        )
        self.legacy_clear_latch.set_writable()
        self.legacy_measurements = command.add_variable(
            self.legacy_namespace, "measurements_per_calc", run_interval
        )
        self.legacy_measurements.set_writable()
        self.legacy_calibrate = command.add_variable(
            self.legacy_namespace, "calibrate", 0
        )
        self.legacy_calibrate.set_writable()
        self.legacy_read_adc = command.add_variable(self.legacy_namespace, "ReadADC", 0)
        self.legacy_read_adc.set_writable()
        self.legacy_csv_data = command.add_variable(
            self.legacy_namespace, "csvData", ""
        )
        self.legacy_resistance_calibration = command.add_variable(
            self.legacy_namespace, "RCalData", ""
        )
        self.legacy_capacitance_calibration = command.add_variable(
            self.legacy_namespace, "CCalData", ""
        )
        self.legacy_thermals = command.add_variable(
            self.legacy_namespace, "thermals", ""
        )
        self.legacy_sdr = command.add_variable(
            self.legacy_namespace,
            "SDR",
            ua.Variant([], ua.VariantType.Int32),
        )
        self.legacy_normalize = command.add_variable(
            self.legacy_namespace, "normalize", 0
        )
        self.legacy_normalize.set_writable()

    def _build_canonical_model(self) -> None:
        objects = self.server.get_objects_node()
        root = self._add_object(
            objects,
            "Device",
            "GIZMo",
            "Fermilab Ground Impedance Monitor instrument.",
        )

        identity = self._add_object(
            root,
            "Identity",
            "Identity",
            "Stable instrument, software, and information-model identity.",
        )
        identity_values = (
            (
                "Identity.Manufacturer",
                "Manufacturer",
                "Fermi National Accelerator Laboratory",
                "Organization responsible for this runtime package.",
            ),
            (
                "Identity.ProductName",
                "ProductName",
                "GIZMo Kria",
                "Instrument product and hardware platform name.",
            ),
            (
                "Identity.ModelNamespaceUri",
                "ModelNamespaceUri",
                MODEL_NAMESPACE_URI,
                "Canonical namespace URI; clients resolve its runtime index.",
            ),
            (
                "Identity.ModelVersion",
                "ModelVersion",
                MODEL_VERSION,
                "Semantic version of this OPC UA information model.",
            ),
            (
                "Identity.RuntimeVersion",
                "RuntimeVersion",
                runtime_version(),
                "Installed gizmo-runtime package version.",
            ),
            (
                "Identity.Hostname",
                "Hostname",
                socket.gethostname(),
                "Current Linux host name.",
            ),
            (
                "Identity.BootId",
                "BootId",
                self._read_text("/proc/sys/kernel/random/boot_id"),
                "Linux boot identifier; changes on every boot.",
            ),
            (
                "Identity.DeviceId",
                "DeviceId",
                self._device_id(),
                "Privacy-preserving stable identifier derived from machine-id.",
            ),
        )
        for path, name, value, description in identity_values:
            self._add_point(
                identity,
                path,
                name,
                value,
                ua.VariantType.String,
                description,
            )
        self._add_point(
            identity,
            "Identity.ModelPublicationDate",
            "ModelPublicationDate",
            MODEL_PUBLICATION_DATE,
            ua.VariantType.DateTime,
            "Publication date of this OPC UA information model.",
        )
        self._add_point(
            identity,
            "Identity.ServerStartTime",
            "ServerStartTime",
            self._started_at,
            ua.VariantType.DateTime,
            "Time this OPC UA server process started.",
        )

        measurement = self._add_object(
            root,
            "Measurement",
            "Measurement",
            "Latest impedance calculation and underlying lock-in values.",
        )
        self._add_point(
            measurement,
            "Measurement.Sequence",
            "Sequence",
            0,
            ua.VariantType.UInt64,
            "Monotonic sequence assigned to each observed ZMon record.",
        )
        self._add_point(
            measurement,
            "Measurement.SampleTime",
            "SampleTime",
            _MISSING_DATETIME,
            ua.VariantType.DateTime,
            "Time the server sampled this measurement from ZMon.",
        )
        self._add_point(
            measurement,
            "Measurement.ResistanceOhm",
            "ResistanceOhm",
            math.nan,
            ua.VariantType.Double,
            (
                "Equivalent resistive impedance at the stimulus frequency. "
                "This value is available only when ResistanceRange is "
                "InRange. Above the validated presentation range, the result "
                "is NaN with Good status and ResistanceRange=OutOfRange: "
                "HIGH Z is a valid measurement state, not a quality fault."
            ),
            unit="Ohm",
        )
        self._add_point(
            measurement,
            "Measurement.ResistanceRange",
            "ResistanceRange",
            "Unknown",
            ua.VariantType.String,
            "Interpretation of ResistanceOhm: InRange, OutOfRange, or Invalid.",
        )
        self._add_point(
            measurement,
            "Measurement.CapacitanceNanofarad",
            "CapacitanceNanofarad",
            math.nan,
            ua.VariantType.Double,
            "Equivalent capacitive component estimated by the calibration.",
            unit="nF",
        )
        self._add_point(
            measurement,
            "Measurement.CapacitanceRange",
            "CapacitanceRange",
            "Unknown",
            ua.VariantType.String,
            "Interpretation of CapacitanceNanofarad.",
        )
        self._add_point(
            measurement,
            "Measurement.ThresholdOhm",
            "ThresholdOhm",
            math.nan,
            ua.VariantType.Double,
            "Alarm threshold active in the measurement engine.",
            unit="Ohm",
            value_range=(float(THRESHOLD_MIN_OHM), float(THRESHOLD_MAX_OHM)),
        )
        self._add_point(
            measurement,
            "Measurement.StimulusFrequencyHertz",
            "StimulusFrequencyHertz",
            math.nan,
            ua.VariantType.Double,
            "Nominal sine-wave stimulus frequency.",
            unit="Hz",
        )
        self._add_point(
            measurement,
            "Measurement.StimulusCurrentRmsAmpere",
            "StimulusCurrentRmsAmpere",
            math.nan,
            ua.VariantType.Double,
            (
                "RMS AC stimulus current delivered to the detector-ground "
                "measurement path. The node remains BadNotSupported/NaN until "
                "the monitor-point transfer function, bandwidth, RMS conversion, "
                "and uncertainty are validated."
            ),
            unit="A",
        )
        for path, name, description, unit in (
            (
                "Measurement.MagnitudeCount",
                "MagnitudeCount",
                "Lock-in magnitude in uncalibrated ADC counts.",
                None,
            ),
            (
                "Measurement.PhaseAtanDegrees",
                "PhaseAtanDegrees",
                "Phase calculated with atan.",
                "deg",
            ),
            (
                "Measurement.PhaseAtan2Degrees",
                "PhaseAtan2Degrees",
                "Phase calculated with atan2.",
                "deg",
            ),
            (
                "Measurement.PhaseInterpolatedDegrees",
                "PhaseInterpolatedDegrees",
                "Phase used by the impedance calibration interpolation.",
                "deg",
            ),
            (
                "Measurement.InPhaseCount",
                "InPhaseCount",
                "Lock-in in-phase component in ADC counts.",
                None,
            ),
            (
                "Measurement.QuadratureCount",
                "QuadratureCount",
                "Lock-in quadrature component in ADC counts.",
                None,
            ),
        ):
            self._add_point(
                measurement,
                path,
                name,
                math.nan,
                ua.VariantType.Double,
                description,
                unit=unit,
            )
        self._add_point(
            measurement,
            "Measurement.AveragesPerCalculation",
            "AveragesPerCalculation",
            0,
            ua.VariantType.UInt32,
            "Number of measurements averaged in each calculation.",
        )
        for path, name, description in (
            (
                "Measurement.Quality",
                "Quality",
                "Human-readable aggregate quality; use each DataValue StatusCode programmatically.",
            ),
            (
                "Measurement.Diagnostic",
                "Diagnostic",
                "Diagnostic explaining degraded or bad measurement quality.",
            ),
            (
                "Measurement.LegacyRecord",
                "LegacyRecord",
                "Original comma-delimited ZMon record retained for audit only.",
            ),
        ):
            self._add_point(
                measurement,
                path,
                name,
                "",
                ua.VariantType.String,
                description,
            )

        alarm = self._add_object(
            root,
            "Alarm",
            "Alarm",
            "Current threshold/phase alarm state and persistent latch.",
        )
        self._add_point(
            alarm,
            "Alarm.Latched",
            "Latched",
            False,
            ua.VariantType.Boolean,
            "True when the persistent GIZMo alarm latch is set.",
        )
        self._add_point(
            alarm,
            "Alarm.Active",
            "Active",
            False,
            ua.VariantType.Boolean,
            "Authoritative composite alarm decision reported by the ZMon "
            "measurement engine at the relay/beacon control branch; not "
            "recomputed by OPC UA.",
        )
        self._add_point(
            alarm,
            "Alarm.Reason",
            "Reason",
            "",
            ua.VariantType.String,
            "Alarm reason reported directly by the ZMon measurement engine, "
            "or empty when the composite alarm is clear.",
        )
        self._add_point(
            alarm,
            "Alarm.LatchTime",
            "LatchTime",
            _MISSING_DATETIME,
            ua.VariantType.DateTime,
            "Time recorded when the persistent latch was set.",
        )

        thermal = self._add_object(
            root,
            "Thermal",
            "Thermal",
            "Chassis and CPU temperature sensors.",
        )
        for key, display in (
            ("Chassis", "ChassisTemperatureCelsius"),
            ("CPU1", "Cpu1TemperatureCelsius"),
            ("CPU2", "Cpu2TemperatureCelsius"),
            ("CPU3", "Cpu3TemperatureCelsius"),
        ):
            self._add_point(
                thermal,
                f"Thermal.{key}TemperatureCelsius",
                display,
                math.nan,
                ua.VariantType.Double,
                f"{key} temperature.",
                unit="Cel",
            )
        self._add_quality_points(thermal, "Thermal")

        clock = self._add_object(
            root,
            "Time",
            "Time",
            "Linux wall clock, timezone, NTP, uptime, RTC, and clocksource.",
        )
        for path, name, default, variant_type, description, unit in (
            (
                "Time.CurrentUtc",
                "CurrentUtc",
                _MISSING_DATETIME,
                ua.VariantType.DateTime,
                "Observed wall-clock time encoded in UTC.",
                None,
            ),
            (
                "Time.CurrentLocal",
                "CurrentLocal",
                "",
                ua.VariantType.String,
                "ISO-8601 local time including UTC offset.",
                None,
            ),
            (
                "Time.BootTime",
                "BootTime",
                _MISSING_DATETIME,
                ua.VariantType.DateTime,
                "Estimated Linux boot time.",
                None,
            ),
            (
                "Time.UptimeSeconds",
                "UptimeSeconds",
                0.0,
                ua.VariantType.Double,
                "Linux uptime.",
                "s",
            ),
            (
                "Time.MonotonicNanoseconds",
                "MonotonicNanoseconds",
                0,
                ua.VariantType.UInt64,
                "Python monotonic clock reading.",
                None,
            ),
            (
                "Time.TimezoneName",
                "TimezoneName",
                "",
                ua.VariantType.String,
                "Configured IANA timezone name.",
                None,
            ),
            (
                "Time.UtcOffsetSeconds",
                "UtcOffsetSeconds",
                0,
                ua.VariantType.Int32,
                "Local UTC offset at CurrentUtc.",
                "s",
            ),
            (
                "Time.NtpSynchronized",
                "NtpSynchronized",
                False,
                ua.VariantType.Boolean,
                "True when timedatectl confirms NTP synchronization.",
                None,
            ),
            (
                "Time.NtpServiceActive",
                "NtpServiceActive",
                False,
                ua.VariantType.Boolean,
                "True when network time service is enabled.",
                None,
            ),
            (
                "Time.NtpService",
                "NtpService",
                "",
                ua.VariantType.String,
                "Source used to determine NTP status.",
                None,
            ),
            (
                "Time.RtcPresent",
                "RtcPresent",
                False,
                ua.VariantType.Boolean,
                "True when Linux exposes an RTC device.",
                None,
            ),
            (
                "Time.RtcDevice",
                "RtcDevice",
                "",
                ua.VariantType.String,
                "Linux RTC sysfs device path.",
                None,
            ),
            (
                "Time.CurrentClocksource",
                "CurrentClocksource",
                "",
                ua.VariantType.String,
                "Kernel clocksource currently selected.",
                None,
            ),
            (
                "Time.AvailableClocksources",
                "AvailableClocksources",
                [],
                ua.VariantType.String,
                "Kernel clocksources available for selection.",
                None,
            ),
        ):
            self._add_point(
                clock,
                path,
                name,
                default,
                variant_type,
                description,
                unit=unit,
            )
        self._add_quality_points(clock, "Time")

        host = self._add_object(
            root,
            "OperatingSystem",
            "OperatingSystem",
            "Linux identity and resource status.",
        )
        for path, name, default, variant_type, description, unit in (
            (
                "OperatingSystem.Hostname",
                "Hostname",
                "",
                ua.VariantType.String,
                "Current Linux hostname.",
                None,
            ),
            (
                "OperatingSystem.PrettyName",
                "PrettyName",
                "",
                ua.VariantType.String,
                "Operating-system distribution name.",
                None,
            ),
            (
                "OperatingSystem.VersionId",
                "VersionId",
                "",
                ua.VariantType.String,
                "Operating-system distribution version.",
                None,
            ),
            (
                "OperatingSystem.KernelRelease",
                "KernelRelease",
                "",
                ua.VariantType.String,
                "Linux kernel release.",
                None,
            ),
            (
                "OperatingSystem.KernelVersion",
                "KernelVersion",
                "",
                ua.VariantType.String,
                "Linux kernel build information.",
                None,
            ),
            (
                "OperatingSystem.Architecture",
                "Architecture",
                "",
                ua.VariantType.String,
                "Machine architecture.",
                None,
            ),
            (
                "OperatingSystem.LogicalCpuCount",
                "LogicalCpuCount",
                0,
                ua.VariantType.UInt32,
                "Number of logical CPUs visible to the runtime.",
                None,
            ),
            (
                "OperatingSystem.CpuUtilizationPercent",
                "CpuUtilizationPercent",
                math.nan,
                ua.VariantType.Double,
                "CPU utilization between consecutive samples.",
                "%",
            ),
            (
                "OperatingSystem.Load1Minute",
                "Load1Minute",
                math.nan,
                ua.VariantType.Double,
                "One-minute Linux load average.",
                None,
            ),
            (
                "OperatingSystem.Load5Minute",
                "Load5Minute",
                math.nan,
                ua.VariantType.Double,
                "Five-minute Linux load average.",
                None,
            ),
            (
                "OperatingSystem.Load15Minute",
                "Load15Minute",
                math.nan,
                ua.VariantType.Double,
                "Fifteen-minute Linux load average.",
                None,
            ),
            (
                "OperatingSystem.MemoryTotalBytes",
                "MemoryTotalBytes",
                0,
                ua.VariantType.UInt64,
                "Total physical memory.",
                "byte",
            ),
            (
                "OperatingSystem.MemoryAvailableBytes",
                "MemoryAvailableBytes",
                0,
                ua.VariantType.UInt64,
                "Memory currently available without swapping.",
                "byte",
            ),
            (
                "OperatingSystem.MemoryUsedBytes",
                "MemoryUsedBytes",
                0,
                ua.VariantType.UInt64,
                "Total memory minus available memory.",
                "byte",
            ),
            (
                "OperatingSystem.SwapTotalBytes",
                "SwapTotalBytes",
                0,
                ua.VariantType.UInt64,
                "Configured swap capacity.",
                "byte",
            ),
            (
                "OperatingSystem.SwapFreeBytes",
                "SwapFreeBytes",
                0,
                ua.VariantType.UInt64,
                "Currently free swap capacity.",
                "byte",
            ),
            (
                "OperatingSystem.ProcessCount",
                "ProcessCount",
                0,
                ua.VariantType.UInt32,
                "Number of Linux processes.",
                None,
            ),
            (
                "OperatingSystem.RunningProcessCount",
                "RunningProcessCount",
                0,
                ua.VariantType.UInt32,
                "Number of runnable Linux processes.",
                None,
            ),
            (
                "OperatingSystem.EntropyAvailableBits",
                "EntropyAvailableBits",
                0,
                ua.VariantType.UInt64,
                "Kernel random-pool entropy estimate.",
                None,
            ),
            (
                "OperatingSystem.OpenFileHandles",
                "OpenFileHandles",
                0,
                ua.VariantType.UInt64,
                "Allocated Linux file handles.",
                None,
            ),
        ):
            self._add_point(
                host,
                path,
                name,
                default,
                variant_type,
                description,
                unit=unit,
            )
        self._add_quality_points(host, "OperatingSystem")

        network = self._add_object(
            root,
            "Network",
            "Network",
            "Interface identity, addresses, routing, DNS, counters, and MAC provenance.",
        )
        self.network_interfaces_object = self._add_object(
            network,
            "Network.Interfaces",
            "Interfaces",
            "One object per Linux network interface.",
        )
        for interface in CONTRACT_NETWORK_INTERFACES:
            self._ensure_interface_points(interface)
        self._add_point(
            network,
            "Network.ExpectedInterfaces",
            "ExpectedInterfaces",
            [],
            ua.VariantType.String,
            "Linux interface names expected on this GIZMo deployment.",
        )
        self._add_point(
            network,
            "Network.MissingInterfaces",
            "MissingInterfaces",
            [],
            ua.VariantType.String,
            "Expected Linux interfaces that are currently absent.",
        )
        self._add_point(
            network,
            "Network.Routes",
            "Routes",
            [],
            ua.VariantType.String,
            "Kernel routing-table entries in normalized text form.",
        )
        self._add_point(
            network,
            "Network.DnsServers",
            "DnsServers",
            [],
            ua.VariantType.String,
            "Configured DNS resolver addresses.",
        )
        self._add_point(
            network,
            "Network.DomainName",
            "DomainName",
            "",
            ua.VariantType.String,
            "Configured DNS search/domain name.",
        )
        self._add_quality_points(network, "Network")

        storage = self._add_object(
            root,
            "Storage",
            "Storage",
            "Capacity and health of filesystems used by GIZMo.",
        )
        self.storage_filesystems_object = self._add_object(
            storage,
            "Storage.Filesystems",
            "Filesystems",
            "One object per relevant mounted filesystem.",
        )
        for filesystem in CONTRACT_FILESYSTEMS:
            self._ensure_filesystem_points(filesystem)
        self._add_quality_points(storage, "Storage")

        firmware = self._add_object(
            root,
            "Firmware",
            "Firmware",
            "Runtime, compute platform, FPGA image, and expected-device state.",
        )
        for path, name, default, variant_type, description in (
            (
                "Firmware.RuntimeVersion",
                "RuntimeVersion",
                "",
                ua.VariantType.String,
                "Installed gizmo-runtime package version.",
            ),
            (
                "Firmware.OverlayName",
                "OverlayName",
                "",
                ua.VariantType.String,
                "Configured xmutil FPGA application name.",
            ),
            (
                "Firmware.OverlayInstalled",
                "OverlayInstalled",
                False,
                ua.VariantType.Boolean,
                "True when the overlay files exist under /lib/firmware/Xilinx.",
            ),
            (
                "Firmware.OverlayLoaded",
                "OverlayLoaded",
                False,
                ua.VariantType.Boolean,
                "True when xmutil confirms the configured application is loaded.",
            ),
            (
                "Firmware.OverlayState",
                "OverlayState",
                "",
                ua.VariantType.String,
                "Running, Degraded, Missing, or load state unconfirmed.",
            ),
            (
                "Firmware.OverlayPath",
                "OverlayPath",
                "",
                ua.VariantType.String,
                "Installed FPGA overlay directory.",
            ),
            (
                "Firmware.OverlayBitstreamSha256",
                "OverlayBitstreamSha256",
                "",
                ua.VariantType.String,
                "SHA-256 digest of the installed FPGA bitstream.",
            ),
            (
                "Firmware.DeviceTreeOverlay",
                "DeviceTreeOverlay",
                "",
                ua.VariantType.String,
                "Installed device-tree overlay path.",
            ),
            (
                "Firmware.DeviceTreeOverlaySha256",
                "DeviceTreeOverlaySha256",
                "",
                ua.VariantType.String,
                "SHA-256 digest of the device-tree overlay.",
            ),
            (
                "Firmware.ShellName",
                "ShellName",
                "",
                ua.VariantType.String,
                "FPGA platform shell type when reported by the runtime image.",
            ),
            (
                "Firmware.ExpectedDevices",
                "ExpectedDevices",
                [],
                ua.VariantType.String,
                "Device nodes expected after the overlay loads.",
            ),
            (
                "Firmware.MissingDevices",
                "MissingDevices",
                [],
                ua.VariantType.String,
                "Expected overlay devices that are currently absent.",
            ),
            (
                "Firmware.BoardModel",
                "BoardModel",
                "",
                ua.VariantType.String,
                "Device-tree board model.",
            ),
            (
                "Firmware.BoardSerialNumber",
                "BoardSerialNumber",
                "",
                ua.VariantType.String,
                "Device-tree serial number.",
            ),
            (
                "Firmware.CarrierManufacturer",
                "CarrierManufacturer",
                "",
                ua.VariantType.String,
                "Carrier manufacturer read through xmutil boardid.",
            ),
            (
                "Firmware.CarrierProductName",
                "CarrierProductName",
                "",
                ua.VariantType.String,
                "Carrier product name read through xmutil boardid.",
            ),
            (
                "Firmware.CarrierPartNumber",
                "CarrierPartNumber",
                "",
                ua.VariantType.String,
                "Carrier part number read through xmutil boardid.",
            ),
            (
                "Firmware.CarrierSerialNumber",
                "CarrierSerialNumber",
                "",
                ua.VariantType.String,
                "Carrier serial number read through xmutil boardid.",
            ),
            (
                "Firmware.CarrierRevision",
                "CarrierRevision",
                "",
                ua.VariantType.String,
                "Carrier revision read through xmutil boardid.",
            ),
            (
                "Firmware.FactoryMacAddresses",
                "FactoryMacAddresses",
                [],
                ua.VariantType.String,
                "Factory MAC addresses read from carrier FRU data.",
            ),
        ):
            self._add_point(
                firmware,
                path,
                name,
                default,
                variant_type,
                description,
            )
        self._add_quality_points(firmware, "Firmware")

        services = self._add_object(
            root,
            "Services",
            "Services",
            "State of every systemd unit owned by the GIZMo package.",
        )
        self.services_units_object = self._add_object(
            services,
            "Services.Units",
            "Units",
            "One object per GIZMo systemd unit.",
        )
        for unit in SYSTEMD_UNITS:
            self._ensure_service_points(unit)
        self._add_quality_points(services, "Services")

        calibration = self._add_object(
            root,
            "Calibration",
            "Calibration",
            "Installed calibration tables and current calibration configuration.",
        )
        for path, name, default, variant_type, description, unit in (
            (
                "Calibration.State",
                "State",
                "Unknown",
                ua.VariantType.String,
                "Valid only when all expected tables contain numeric rows.",
                None,
            ),
            (
                "Calibration.ConfiguredThresholdOhm",
                "ConfiguredThresholdOhm",
                0.0,
                ua.VariantType.Double,
                "Threshold persisted in package-owned state.",
                "Ohm",
            ),
            (
                "Calibration.MeasurementsPerCalculation",
                "MeasurementsPerCalculation",
                0,
                ua.VariantType.UInt32,
                "Averaging interval persisted in package-owned state.",
                None,
            ),
            (
                "Calibration.MagnitudeNormalizationPending",
                "MagnitudeNormalizationPending",
                False,
                ua.VariantType.Boolean,
                "True when magnitude normalization has been requested.",
                None,
            ),
            (
                "Calibration.LastCalibrationTime",
                "LastCalibrationTime",
                _MISSING_DATETIME,
                ua.VariantType.DateTime,
                "Newest modification time among the calibration tables.",
                None,
            ),
            (
                "Calibration.OperationState",
                "OperationState",
                "Idle",
                ua.VariantType.String,
                (
                    "Calibration execution state: Idle, Starting, Running, "
                    "Restoring, Completed, Failed, Aborted, or Unknown."
                ),
                None,
            ),
            (
                "Calibration.LastOperationTime",
                "LastOperationTime",
                _MISSING_DATETIME,
                ua.VariantType.DateTime,
                "Completion, failure, or abort time of the latest calibration operation.",
                None,
            ),
            (
                "Calibration.LastOperationResult",
                "LastOperationResult",
                "",
                ua.VariantType.String,
                "Bounded result or validation summary for the latest calibration operation.",
                None,
            ),
            (
                "Calibration.RestorationState",
                "RestorationState",
                "NotRequired",
                ua.VariantType.String,
                (
                    "Normal-state restoration: NotRequired, Commanded, "
                    "Verified, Failed, or Unknown."
                ),
                None,
            ),
        ):
            self._add_point(
                calibration,
                path,
                name,
                default,
                variant_type,
                description,
                unit=unit,
            )
        self._add_point(
            calibration,
            "Calibration.ProgressPercent",
            "ProgressPercent",
            math.nan,
            ua.VariantType.Double,
            (
                "Best available calibration progress estimate. Unknown progress "
                "is NaN with a non-good StatusCode."
            ),
            unit="%",
            value_range=(0.0, 100.0),
        )
        tables_object = self._add_object(
            calibration,
            "Calibration.Tables",
            "Tables",
            "Metadata for each calibration table.",
        )
        for key in (
            "Resistance",
            "ResistancePhase",
            "Capacitance",
            "CapacitancePhase",
        ):
            self._ensure_calibration_points(tables_object, key)
        self._add_quality_points(calibration, "Calibration")

        sdr = self._add_object(
            root,
            "SDR",
            "SDR",
            "Latest raw ADC/SDR frame and stream status.",
        )
        for path, name, default, variant_type, description in (
            (
                "SDR.Available",
                "Available",
                False,
                ua.VariantType.Boolean,
                "True when a complete frame was received from the SDR service.",
            ),
            (
                "SDR.Endpoint",
                "Endpoint",
                f"tcp://{SDR_HOST}:{SDR_PORT}",
                ua.VariantType.String,
                "Internal SDR stream endpoint.",
            ),
            (
                "SDR.SampleFormat",
                "SampleFormat",
                "little-endian signed int32",
                ua.VariantType.String,
                "Binary representation produced by the SDR service.",
            ),
            (
                "SDR.SamplesPerFrame",
                "SamplesPerFrame",
                SDR_SAMPLE_COUNT,
                ua.VariantType.UInt32,
                "Configured sample count in each complete frame.",
            ),
            (
                "SDR.FrameSequence",
                "FrameSequence",
                0,
                ua.VariantType.UInt64,
                "Monotonic sequence assigned to each complete SDR frame.",
            ),
            (
                "SDR.SampleTime",
                "SampleTime",
                _MISSING_DATETIME,
                ua.VariantType.DateTime,
                "Time the server received the complete SDR frame.",
            ),
            (
                "SDR.LatestFrame",
                "LatestFrame",
                [],
                ua.VariantType.Int32,
                "Complete signed Int32 SDR frame; subscriptions should use a suitable interval.",
            ),
        ):
            self._add_point(
                sdr,
                path,
                name,
                default,
                variant_type,
                description,
            )
        self._add_quality_points(sdr, "SDR")

        configuration = self._add_object(
            root,
            "Configuration",
            "Configuration",
            "Validated operator configuration forwarded to the measurement engine.",
        )
        threshold = configured_threshold()
        interval = read_exported_int("setRunInterval.env", "runInterval", 100)
        self.configuration_threshold = self._add_point(
            configuration,
            "Configuration.ThresholdOhm",
            "ThresholdOhm",
            threshold,
            ua.VariantType.UInt32,
            "Writable integer alarm threshold from 0 through 1,000,000 ohm.",
            unit="Ohm",
            value_range=(float(THRESHOLD_MIN_OHM), float(THRESHOLD_MAX_OHM)),
            writable=True,
        )
        self.configuration_interval = self._add_point(
            configuration,
            "Configuration.AveragesPerCalculation",
            "AveragesPerCalculation",
            interval,
            ua.VariantType.UInt32,
            "Writable averaging count; accepted values are 1 through 1,000,000.",
            writable=True,
        )
        self._add_point(
            configuration,
            "Configuration.LastCommandResult",
            "LastCommandResult",
            "",
            ua.VariantType.String,
            "Result of the most recent canonical configuration write or method call.",
        )

        operations = self._add_object(
            root,
            "Operations",
            "Operations",
            "Explicit operations replacing legacy magic writable variables.",
        )
        for path, name, default, variant_type, description in (
            (
                "Operations.CommandGateState",
                "CommandGateState",
                "Disabled",
                ua.VariantType.String,
                (
                    "Mutating-command gate: Disabled, Ready, Busy, "
                    "MaintenanceLocked, or FaultLocked."
                ),
            ),
            (
                "Operations.LastCommandId",
                "LastCommandId",
                "",
                ua.VariantType.String,
                "Opaque identifier for the latest accepted or rejected mutation.",
            ),
            (
                "Operations.LastCommandName",
                "LastCommandName",
                "",
                ua.VariantType.String,
                "Canonical name of the latest accepted or rejected mutation.",
            ),
            (
                "Operations.LastCommandParameters",
                "LastCommandParameters",
                "",
                ua.VariantType.String,
                "Bounded, sanitized parameters for the latest mutation.",
            ),
            (
                "Operations.LastCommandRequester",
                "LastCommandRequester",
                "",
                ua.VariantType.String,
                "Authenticated user or service identity for the latest mutation.",
            ),
            (
                "Operations.LastCommandState",
                "LastCommandState",
                "Idle",
                ua.VariantType.String,
                (
                    "Execution state: Idle, Accepted, Running, Succeeded, "
                    "Failed, Rejected, or TimedOut."
                ),
            ),
            (
                "Operations.LastCommandRequestTime",
                "LastCommandRequestTime",
                _MISSING_DATETIME,
                ua.VariantType.DateTime,
                "UTC time the latest mutating request was received.",
            ),
            (
                "Operations.LastCommandCompletionTime",
                "LastCommandCompletionTime",
                _MISSING_DATETIME,
                ua.VariantType.DateTime,
                "UTC time the latest mutating request reached a terminal state.",
            ),
            (
                "Operations.LastCommandResult",
                "LastCommandResult",
                "",
                ua.VariantType.String,
                "Bounded result or failure reason for the latest mutation.",
            ),
        ):
            self._add_point(
                operations,
                path,
                name,
                default,
                variant_type,
                description,
            )
        self._add_method(
            operations,
            "Operations.ClearLatch",
            "ClearLatch",
            self.clear_latch_method,
            [],
            "Clear the persistent alarm latch.",
        )
        self._add_method(
            operations,
            "Operations.StartCalibration",
            "StartCalibration",
            self.start_calibration_method,
            [
                self._method_argument(
                    "ReadsPerPoint",
                    ua.VariantType.UInt32,
                    "Requested measurements at each calibration point.",
                )
            ],
            "Restart ZMon in calibration mode with the requested reads per point.",
        )
        self._add_method(
            operations,
            "Operations.CaptureAdc",
            "CaptureAdc",
            self.capture_adc_method,
            [],
            "Request a raw ADC capture; read Legacy/CommandObject/csvData when complete.",
        )
        self._add_method(
            operations,
            "Operations.NormalizeMagnitude",
            "NormalizeMagnitude",
            self.normalize_magnitude_method,
            [],
            "Set the one-shot magnitude-normalization flag.",
        )
        self._add_method(
            operations,
            "Operations.SetSystemTime",
            "SetSystemTime",
            self.set_system_time_method,
            [
                self._method_argument(
                    "RequestedTime",
                    ua.VariantType.DateTime,
                    "Absolute UTC time requested for the Linux clock.",
                )
            ],
            "Set the Linux wall clock from an absolute OPC UA DateTime.",
        )
        self._add_method(
            operations,
            "Operations.RestartMeasurementEngine",
            "RestartMeasurementEngine",
            self.restart_measurement_engine_method,
            [],
            (
                "Restart only the ZMon measurement engine and verify a new PID, "
                "the expected executable hash, and a fresh measurement."
            ),
        )
        self._add_method(
            operations,
            "Operations.AbortCalibration",
            "AbortCalibration",
            self.abort_calibration_method,
            [],
            "Abort an active calibration and begin verified normal-state restoration.",
        )
        self._add_method(
            operations,
            "Operations.RestoreNormalState",
            "RestoreNormalState",
            self.restore_normal_state_method,
            [],
            "Command and verify the approved normal measurement state.",
        )

        health = self._add_object(
            root,
            "Health",
            "Health",
            "Aggregate availability of every monitored subsystem.",
        )
        self._add_point(
            health,
            "Health.Overall",
            "Overall",
            "Starting",
            ua.VariantType.String,
            "OK, Degraded, Failed, or Starting.",
        )
        self._add_point(
            health,
            "Health.LastUpdate",
            "LastUpdate",
            _MISSING_DATETIME,
            ua.VariantType.DateTime,
            "Time aggregate health was last evaluated.",
        )
        for category in (
            "Measurement",
            "Thermal",
            "Time",
            "OperatingSystem",
            "Network",
            "Storage",
            "Firmware",
            "Services",
            "Calibration",
            "SDR",
        ):
            self._add_point(
                health,
                f"Health.{category}",
                category,
                "Starting",
                ua.VariantType.String,
                f"Aggregate quality of the {category} subtree.",
            )

    def _add_quality_points(self, parent: object, prefix: str) -> None:
        self._add_point(
            parent,
            f"{prefix}.Quality",
            "Quality",
            QUALITY_NOT_AVAILABLE,
            ua.VariantType.String,
            "Human-readable aggregate quality; inspect DataValue StatusCode for machine use.",
        )
        self._add_point(
            parent,
            f"{prefix}.Diagnostic",
            "Diagnostic",
            "",
            ua.VariantType.String,
            "Diagnostic explaining degraded or bad quality.",
        )
        self._add_point(
            parent,
            f"{prefix}.LastUpdate",
            "LastUpdate",
            _MISSING_DATETIME,
            ua.VariantType.DateTime,
            "Source timestamp of the latest completed update.",
        )

    @staticmethod
    def _read_text(path: str | Path) -> str:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _device_id(self) -> str:
        machine_id = self._read_text("/etc/machine-id") or socket.gethostname()
        return hashlib.sha256(f"gov.fnal.gizmo:{machine_id}".encode()).hexdigest()[:32]

    def _ensure_interface_points(self, name: str) -> dict[str, str]:
        if name in self._interface_points:
            return self._interface_points[name]
        key = self._safe_identifier(name)
        base = f"Network.Interfaces.{key}"
        obj = self._add_object(
            self.network_interfaces_object,
            base,
            name,
            f"Linux network interface {name}.",
        )
        definitions = (
            ("Name", name, ua.VariantType.String, "Linux interface name.", None),
            (
                "Present",
                False,
                ua.VariantType.Boolean,
                "True when the interface exists in the current inventory.",
                None,
            ),
            ("Index", 0, ua.VariantType.UInt32, "Linux interface index.", None),
            (
                "MacAddress",
                "",
                ua.VariantType.String,
                "Current link-layer address.",
                None,
            ),
            (
                "PermanentMacAddress",
                "",
                ua.VariantType.String,
                "Permanent address reported by the driver when available.",
                None,
            ),
            (
                "MacAssignmentCode",
                -1,
                ua.VariantType.Int32,
                "Linux addr_assign_type: 0 permanent, 1 random, 2 stolen, 3 software set.",
                None,
            ),
            (
                "MacAddressSource",
                "",
                ua.VariantType.String,
                "Observed MAC provenance; FRU EEPROM is only reported when verified against xmutil boardid.",
                None,
            ),
            (
                "AdministrativeUp",
                False,
                ua.VariantType.Boolean,
                "True when the interface UP flag is set.",
                None,
            ),
            (
                "Carrier",
                False,
                ua.VariantType.Boolean,
                "Physical link carrier state.",
                None,
            ),
            (
                "OperationalState",
                "",
                ua.VariantType.String,
                "Linux operational state.",
                None,
            ),
            ("Mtu", 0, ua.VariantType.UInt32, "Interface MTU.", None),
            (
                "SpeedMegabitPerSecond",
                0,
                ua.VariantType.UInt32,
                "Negotiated link speed.",
                "Mbit/s",
            ),
            (
                "Duplex",
                "",
                ua.VariantType.String,
                "Negotiated duplex mode.",
                None,
            ),
            (
                "Driver",
                "",
                ua.VariantType.String,
                "Bound Linux network driver.",
                None,
            ),
            (
                "Addresses",
                [],
                ua.VariantType.String,
                "Assigned IP addresses with prefix, family, scope, and flags.",
                None,
            ),
            ("RxBytes", 0, ua.VariantType.UInt64, "Received bytes.", "byte"),
            ("RxPackets", 0, ua.VariantType.UInt64, "Received packets.", None),
            ("RxErrors", 0, ua.VariantType.UInt64, "Receive errors.", None),
            ("RxDropped", 0, ua.VariantType.UInt64, "Dropped receive packets.", None),
            ("TxBytes", 0, ua.VariantType.UInt64, "Transmitted bytes.", "byte"),
            ("TxPackets", 0, ua.VariantType.UInt64, "Transmitted packets.", None),
            ("TxErrors", 0, ua.VariantType.UInt64, "Transmit errors.", None),
            ("TxDropped", 0, ua.VariantType.UInt64, "Dropped transmit packets.", None),
            ("Collisions", 0, ua.VariantType.UInt64, "Link collisions.", None),
        )
        points: dict[str, str] = {}
        for field_name, default, variant_type, description, unit in definitions:
            path = f"{base}.{field_name}"
            self._add_point(
                obj,
                path,
                field_name,
                default,
                variant_type,
                description,
                unit=unit,
            )
            points[field_name] = path
        self._interface_points[name] = points
        return points

    def _ensure_filesystem_points(self, key: str) -> dict[str, str]:
        if key in self._filesystem_points:
            return self._filesystem_points[key]
        safe_key = self._safe_identifier(key)
        base = f"Storage.Filesystems.{safe_key}"
        obj = self._add_object(
            self.storage_filesystems_object,
            base,
            key,
            f"Filesystem serving the GIZMo {key.lower()} path.",
        )
        definitions = (
            ("MountPoint", "", ua.VariantType.String, "Mount point.", None),
            ("Source", "", ua.VariantType.String, "Filesystem source.", None),
            ("Type", "", ua.VariantType.String, "Filesystem type.", None),
            ("TotalBytes", 0, ua.VariantType.UInt64, "Total capacity.", "byte"),
            ("UsedBytes", 0, ua.VariantType.UInt64, "Used capacity.", "byte"),
            (
                "AvailableBytes",
                0,
                ua.VariantType.UInt64,
                "Capacity available to this service.",
                "byte",
            ),
            (
                "UsedPercent",
                0.0,
                ua.VariantType.Double,
                "Percentage of total capacity in use.",
                "%",
            ),
            (
                "ReadOnly",
                False,
                ua.VariantType.Boolean,
                "True when mounted read-only.",
                None,
            ),
            ("Quality", "", ua.VariantType.String, "Filesystem quality.", None),
        )
        points: dict[str, str] = {}
        for field_name, default, variant_type, description, unit in definitions:
            path = f"{base}.{field_name}"
            self._add_point(
                obj,
                path,
                field_name,
                default,
                variant_type,
                description,
                unit=unit,
            )
            points[field_name] = path
        self._filesystem_points[key] = points
        return points

    def _ensure_service_points(self, unit: str) -> dict[str, str]:
        if unit in self._service_points:
            return self._service_points[unit]
        key = self._safe_identifier(unit)
        base = f"Services.Units.{key}"
        obj = self._add_object(
            self.services_units_object,
            base,
            unit,
            f"systemd state for {unit}.",
        )
        definitions = (
            ("Unit", unit, ua.VariantType.String, "systemd unit name."),
            ("Description", "", ua.VariantType.String, "systemd description."),
            ("LoadState", "", ua.VariantType.String, "systemd load state."),
            ("ActiveState", "", ua.VariantType.String, "systemd active state."),
            ("SubState", "", ua.VariantType.String, "systemd sub-state."),
            ("Result", "", ua.VariantType.String, "Last systemd result."),
            ("MainPid", 0, ua.VariantType.UInt32, "Current main process ID."),
            ("RestartCount", 0, ua.VariantType.UInt32, "Automatic restart count."),
            ("ExitStatus", 0, ua.VariantType.Int32, "Last main-process exit status."),
            (
                "ActiveSince",
                _MISSING_DATETIME,
                ua.VariantType.DateTime,
                "Time the unit entered active state.",
            ),
            ("StatusText", "", ua.VariantType.String, "Current systemd status text."),
            (
                "Required",
                False,
                ua.VariantType.Boolean,
                "True when gizmo.target treats this unit as required.",
            ),
            ("Quality", "", ua.VariantType.String, "Unit quality."),
        )
        points: dict[str, str] = {}
        for field_name, default, variant_type, description in definitions:
            path = f"{base}.{field_name}"
            self._add_point(
                obj,
                path,
                field_name,
                default,
                variant_type,
                description,
            )
            points[field_name] = path
        self._service_points[unit] = points
        return points

    def _ensure_calibration_points(self, parent: object, key: str) -> dict[str, str]:
        if key in self._calibration_points:
            return self._calibration_points[key]
        safe_key = self._safe_identifier(key)
        base = f"Calibration.Tables.{safe_key}"
        obj = self._add_object(
            parent,
            base,
            key,
            f"Metadata for the {key} calibration table.",
        )
        definitions = (
            ("Kind", key, ua.VariantType.String, "Calibration-table kind."),
            ("Path", "", ua.VariantType.String, "Package-owned mutable table path."),
            ("State", "Unknown", ua.VariantType.String, "Valid, Invalid, or Missing."),
            ("Sha256", "", ua.VariantType.String, "SHA-256 table digest."),
            (
                "ModifiedTime",
                _MISSING_DATETIME,
                ua.VariantType.DateTime,
                "Table modification time.",
            ),
            ("RowCount", 0, ua.VariantType.UInt32, "Number of numeric input rows."),
            ("InputMinimum", math.nan, ua.VariantType.Double, "Minimum input value."),
            ("InputMaximum", math.nan, ua.VariantType.Double, "Maximum input value."),
            ("InputUnit", "", ua.VariantType.String, "Unit of the first table column."),
            ("Format", "", ua.VariantType.String, "Expected CSV column format."),
        )
        points: dict[str, str] = {}
        for field_name, default, variant_type, description in definitions:
            path = f"{base}.{field_name}"
            self._add_point(
                obj,
                path,
                field_name,
                default,
                variant_type,
                description,
            )
            points[field_name] = path
        self._calibration_points[key] = points
        return points

    @staticmethod
    def _quality_status(quality: str) -> int:
        if quality == QUALITY_GOOD:
            return ua.StatusCodes.Good
        if quality == QUALITY_UNCERTAIN:
            return ua.StatusCodes.UncertainLastUsableValue
        if quality == QUALITY_NOT_AVAILABLE:
            return ua.StatusCodes.BadNoCommunication
        return ua.StatusCodes.BadDataUnavailable

    def _update(
        self,
        path: str,
        value: object,
        quality: str = QUALITY_GOOD,
        source_time: dt.datetime | None = None,
        *,
        status_code: int | None = None,
    ) -> None:
        node, variant_type, default = self._points[path]
        if value is None:
            value = default
            if quality == QUALITY_GOOD:
                quality = QUALITY_NOT_AVAILABLE
        stamp = source_time or utc_now()
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(dt.timezone.utc)
        data_value = ua.DataValue(
            ua.Variant(value, variant_type),
            ua.StatusCode(
                status_code
                if status_code is not None
                else self._quality_status(quality)
            ),
            sourceTimestamp=stamp,
        )
        node.set_data_value(data_value)

    def _mark_initial_values(self) -> None:
        live_prefixes = (
            "Measurement.",
            "Alarm.",
            "Thermal.",
            "Time.",
            "OperatingSystem.",
            "Network.",
            "Storage.",
            "Firmware.",
            "Services.",
            "Calibration.",
            "SDR.",
            "Health.",
        )
        stamp = utc_now()
        for path, (node, variant_type, default) in self._points.items():
            if path.startswith(live_prefixes):
                node.set_data_value(
                    ua.DataValue(
                        ua.Variant(default, variant_type),
                        ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData),
                        sourceTimestamp=stamp,
                    )
                )

    def _mark_prefix_failed(self, prefix: str, error: BaseException) -> None:
        quality = (
            QUALITY_UNCERTAIN if prefix in self._has_data else QUALITY_NOT_AVAILABLE
        )
        status = (
            ua.StatusCodes.UncertainLastUsableValue
            if prefix in self._has_data
            else ua.StatusCodes.BadNoCommunication
        )
        stamp = utc_now()
        for path, (node, variant_type, _) in self._points.items():
            if not path.startswith(f"{prefix}."):
                continue
            try:
                current = node.get_attributes([ua.AttributeIds.Value])[0].Value.Value
                node.set_data_value(
                    ua.DataValue(
                        ua.Variant(current, variant_type),
                        ua.StatusCode(status),
                        sourceTimestamp=stamp,
                    )
                )
            except (ValueError, RuntimeError):
                continue
        quality_path = f"{prefix}.Quality"
        diagnostic_path = f"{prefix}.Diagnostic"
        update_path = f"{prefix}.LastUpdate"
        if quality_path in self._points:
            self._update(quality_path, quality, QUALITY_GOOD, stamp)
        if diagnostic_path in self._points:
            self._update(diagnostic_path, str(error), QUALITY_GOOD, stamp)
        if update_path in self._points:
            self._update(update_path, stamp, QUALITY_GOOD, stamp)
        self._category_quality[prefix] = quality
        self._update_health()

    def request(self, command: str) -> str:
        """Use a request-local socket so OPC callback threads never share one."""
        with self._request_lock:
            client = self._context.socket(zmq.REQ)
            client.setsockopt(zmq.LINGER, 0)
            client.setsockopt(zmq.SNDTIMEO, REQUEST_TIMEOUT_MS)
            client.setsockopt(zmq.RCVTIMEO, REQUEST_TIMEOUT_MS)
            client.connect(ZMQ_ENDPOINT)
            try:
                client.send_string(command)
                return client.recv_string()
            finally:
                client.close()

    @staticmethod
    def _method_value(value: object) -> object:
        return getattr(value, "Value", value)

    @staticmethod
    def _bounded(value: object, limit: int) -> str:
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
        return text if len(text) <= limit else f"{text[: limit - 3]}..."

    @staticmethod
    def _call_error(status_code: int) -> ua.CallMethodResult:
        result = ua.CallMethodResult()
        result.StatusCode = ua.StatusCode(status_code)
        return result

    @staticmethod
    def _parse_persisted_time(value: object) -> dt.datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(dt.timezone.utc)

    def _base_gate_state(self) -> str:
        if self._command_fault_locked:
            return "FaultLocked"
        if self._active_command is not None:
            return "Busy"
        if self._effective_gate_mode == "disabled":
            return "Disabled"
        if self._effective_gate_mode == "operator":
            return "MaintenanceLocked"
        return "Ready"

    def _publish_gate_state(self) -> None:
        self._update(
            "Operations.CommandGateState",
            self._base_gate_state(),
            QUALITY_GOOD,
            utc_now(),
        )

    def _publish_audit(self) -> None:
        stamp = utc_now()
        for path, value in (
            ("Operations.LastCommandId", self._audit["id"]),
            ("Operations.LastCommandName", self._audit["name"]),
            ("Operations.LastCommandParameters", self._audit["parameters"]),
            ("Operations.LastCommandRequester", self._audit["requester"]),
            ("Operations.LastCommandState", self._audit["state"]),
            ("Operations.LastCommandResult", self._audit["result"]),
        ):
            self._update(path, value, QUALITY_GOOD, stamp)
        request_time = self._audit.get("request_time")
        completion_time = self._audit.get("completion_time")
        self._update(
            "Operations.LastCommandRequestTime",
            request_time,
            QUALITY_GOOD if request_time else QUALITY_NOT_AVAILABLE,
            stamp,
            status_code=(None if request_time else ua.StatusCodes.BadNoData),
        )
        self._update(
            "Operations.LastCommandCompletionTime",
            completion_time,
            QUALITY_GOOD if completion_time else QUALITY_NOT_AVAILABLE,
            stamp,
            status_code=(None if completion_time else ua.StatusCodes.BadNoData),
        )

    def _publish_calibration_operation(self) -> None:
        stamp = utc_now()
        for path, value in (
            ("Calibration.OperationState", self._calibration_operation["state"]),
            (
                "Calibration.LastOperationResult",
                self._calibration_operation["result"],
            ),
            (
                "Calibration.RestorationState",
                self._calibration_operation["restoration"],
            ),
        ):
            self._update(path, value, QUALITY_GOOD, stamp)
        progress = self._calibration_operation.get("progress")
        self._update(
            "Calibration.ProgressPercent",
            progress,
            QUALITY_GOOD if progress is not None else QUALITY_NOT_AVAILABLE,
            stamp,
            status_code=(
                None if progress is not None else ua.StatusCodes.BadDataUnavailable
            ),
        )
        completed = self._calibration_operation.get("time")
        self._update(
            "Calibration.LastOperationTime",
            completed,
            QUALITY_GOOD if completed else QUALITY_NOT_AVAILABLE,
            stamp,
            status_code=(None if completed else ua.StatusCodes.BadNoData),
        )

    def _persist_command_state(self) -> None:
        payload = {
            "schema": 1,
            "fault_locked": self._command_fault_locked,
            "audit": {
                **self._audit,
                "request_time": (
                    self._audit["request_time"].isoformat()
                    if self._audit.get("request_time")
                    else None
                ),
                "completion_time": (
                    self._audit["completion_time"].isoformat()
                    if self._audit.get("completion_time")
                    else None
                ),
            },
            "calibration": {
                **self._calibration_operation,
                "time": (
                    self._calibration_operation["time"].isoformat()
                    if self._calibration_operation.get("time")
                    else None
                ),
            },
        }
        try:
            atomic_write(
                self._command_state_path,
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            )
        except OSError as error:
            print(f"Unable to persist OPC UA command state: {error}", flush=True)

    def _restore_command_state(self) -> None:
        self._audit = {
            "id": "",
            "name": "",
            "parameters": "",
            "requester": "",
            "state": "Idle",
            "request_time": None,
            "completion_time": None,
            "result": "",
        }
        self._calibration_operation = {
            "state": "Idle",
            "progress": None,
            "time": None,
            "result": "",
            "restoration": "NotRequired",
        }
        try:
            raw = json.loads(self._command_state_path.read_text(encoding="utf-8"))
            if raw.get("schema") != 1:
                raise ValueError("unsupported command-state schema")
            audit = raw.get("audit", {})
            calibration = raw.get("calibration", {})
            state = str(audit.get("state", "Idle"))
            if state not in COMMAND_STATES:
                raise ValueError("invalid persisted command state")
            self._audit.update(
                {
                    "id": self._bounded(audit.get("id", ""), 64),
                    "name": self._bounded(audit.get("name", ""), 96),
                    "parameters": self._bounded(
                        audit.get("parameters", ""), COMMAND_PARAMETER_LIMIT
                    ),
                    "requester": self._bounded(
                        audit.get("requester", ""), COMMAND_REQUESTER_LIMIT
                    ),
                    "state": state,
                    "request_time": self._parse_persisted_time(
                        audit.get("request_time")
                    ),
                    "completion_time": self._parse_persisted_time(
                        audit.get("completion_time")
                    ),
                    "result": self._bounded(
                        audit.get("result", ""), COMMAND_RESULT_LIMIT
                    ),
                }
            )
            calibration_state = str(calibration.get("state", "Idle"))
            restoration = str(calibration.get("restoration", "NotRequired"))
            if calibration_state not in {
                "Idle",
                "Starting",
                "Running",
                "Restoring",
                "Completed",
                "Failed",
                "Aborted",
                "Unknown",
            }:
                raise ValueError("invalid persisted calibration state")
            if restoration not in {
                "NotRequired",
                "Commanded",
                "Verified",
                "Failed",
                "Unknown",
            }:
                raise ValueError("invalid persisted restoration state")
            progress = calibration.get("progress")
            if not isinstance(progress, (int, float)) or isinstance(progress, bool):
                progress = None
            elif not 0 <= float(progress) <= 100:
                progress = None
            self._calibration_operation.update(
                {
                    "state": calibration_state,
                    "progress": float(progress) if progress is not None else None,
                    "time": self._parse_persisted_time(calibration.get("time")),
                    "result": self._bounded(
                        calibration.get("result", ""), COMMAND_RESULT_LIMIT
                    ),
                    "restoration": restoration,
                }
            )
            self._command_fault_locked = bool(raw.get("fault_locked", False))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            self._command_fault_locked = True
            self._audit["result"] = self._bounded(
                f"Stored command state is invalid: {error}", COMMAND_RESULT_LIMIT
            )

        if self._audit["state"] in {"Accepted", "Running"}:
            now = utc_now()
            self._audit["state"] = "Failed"
            self._audit["completion_time"] = now
            self._audit["result"] = self._bounded(
                "OPC UA server restarted before command completion; normal state requires verification",
                COMMAND_RESULT_LIMIT,
            )
            self._calibration_operation.update(
                {
                    "state": "Unknown",
                    "progress": None,
                    "time": now,
                    "result": "Command interrupted by OPC UA server restart",
                    "restoration": "Unknown",
                }
            )
            self._command_fault_locked = True
            self._persist_command_state()

        self._publish_audit()
        self._publish_calibration_operation()
        self._publish_gate_state()
        self._update(
            "Measurement.StimulusCurrentRmsAmpere",
            None,
            QUALITY_NOT_AVAILABLE,
            utc_now(),
            status_code=ua.StatusCodes.BadNotSupported,
        )

    def _set_calibration_operation(
        self,
        state: str,
        *,
        progress: float | None = None,
        result: str | None = None,
        restoration: str | None = None,
        terminal: bool = False,
    ) -> None:
        self._calibration_operation["state"] = state
        self._calibration_operation["progress"] = progress
        if result is not None:
            self._calibration_operation["result"] = self._bounded(
                result, COMMAND_RESULT_LIMIT
            )
        if restoration is not None:
            self._calibration_operation["restoration"] = restoration
        if terminal:
            self._calibration_operation["time"] = utc_now()
        self._publish_calibration_operation()
        self._persist_command_state()

    def _record_rejected(
        self, name: str, parameters: str, requester: str, reason: str
    ) -> None:
        if self._active_command is not None:
            print(
                f"Rejected concurrent OPC UA command {name} from {requester}: {reason}",
                flush=True,
            )
            return
        now = utc_now()
        self._audit = {
            "id": uuid.uuid4().hex,
            "name": name,
            "parameters": self._bounded(parameters, COMMAND_PARAMETER_LIMIT),
            "requester": self._bounded(requester, COMMAND_REQUESTER_LIMIT),
            "state": "Rejected",
            "request_time": now,
            "completion_time": now,
            "result": self._bounded(reason, COMMAND_RESULT_LIMIT),
        }
        self._publish_audit()
        self._publish_gate_state()
        self._persist_command_state()

    def _finish_command(
        self,
        command_id: str,
        state: str,
        result: str,
        *,
        fault_locked: bool = False,
    ) -> None:
        with self._command_lock:
            if self._active_command is None or self._active_command.command_id != command_id:
                return
            if state not in {"Succeeded", "Failed", "TimedOut"}:
                raise ValueError(f"invalid terminal command state {state}")
            self._audit["state"] = state
            self._audit["completion_time"] = utc_now()
            self._audit["result"] = self._bounded(result, COMMAND_RESULT_LIMIT)
            self._update(
                "Configuration.LastCommandResult",
                self._audit["result"],
                QUALITY_GOOD if state == "Succeeded" else QUALITY_BAD,
                self._audit["completion_time"],
            )
            self._active_command = None
            if fault_locked:
                self._command_fault_locked = True
            self._publish_audit()
            self._publish_gate_state()
            self._persist_command_state()

    def _set_command_running(
        self,
        command_id: str,
        verification: str,
        accepted_result: str,
        *,
        timeout_seconds: float,
        expected_value: object | None = None,
        terminal_calibration_state: str = "",
    ) -> None:
        with self._command_lock:
            active = self._active_command
            if active is None or active.command_id != command_id:
                raise RuntimeError("command reservation was lost")
            active.verification = verification
            active.accepted_result = self._bounded(
                accepted_result, COMMAND_RESULT_LIMIT
            )
            active.expected_value = expected_value
            active.terminal_calibration_state = terminal_calibration_state
            active.deadline_monotonic = time.monotonic() + max(1.0, timeout_seconds)
            self._audit["state"] = "Running"
            self._audit["result"] = active.accepted_result
            self._publish_audit()
            self._publish_gate_state()
            self._persist_command_state()
            self._next_run["Measurement"] = 0.0
            self._next_run["Services"] = 0.0

    @staticmethod
    def _sha256_file(path: Path) -> str:
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return ""

    def _begin_remote_command(
        self,
        session: GizmoInternalSession,
        path: str,
        parameters: str,
    ) -> tuple[ActiveCommand | None, int | None]:
        requester = getattr(session, "gizmo_requester", "anonymous")
        role = getattr(session, "gizmo_role", "anonymous")
        name = path.rsplit(".", 1)[-1]
        maintenance = path in MAINTENANCE_METHODS
        with self._command_lock:
            if role not in {"operator", "maintenance"}:
                reason = "authenticated operator or maintenance role required"
                self._record_rejected(name, parameters, requester, reason)
                return None, ua.StatusCodes.BadUserAccessDenied
            if self._effective_gate_mode == "disabled":
                reason = self._command_policy_reason or "command gate is disabled"
                self._record_rejected(name, parameters, requester, reason)
                return None, ua.StatusCodes.BadOutOfService
            if maintenance and role != "maintenance":
                reason = "maintenance role required"
                self._record_rejected(name, parameters, requester, reason)
                return None, ua.StatusCodes.BadUserAccessDenied
            if maintenance and self._effective_gate_mode != "maintenance":
                reason = "maintenance command gate is locked"
                self._record_rejected(name, parameters, requester, reason)
                return None, ua.StatusCodes.BadInvalidState
            if self._command_fault_locked and path != "Operations.RestoreNormalState":
                reason = "command gate is fault-locked; RestoreNormalState is required"
                self._record_rejected(name, parameters, requester, reason)
                return None, ua.StatusCodes.BadInvalidState

            if self._active_command is not None:
                if path == "Operations.AbortCalibration" and self._active_command.verification in {
                    "calibration",
                    "calibration-timeout-restore",
                }:
                    previous = self._active_command
                    self._finish_command(
                        previous.command_id,
                        "Failed",
                        f"Superseded by AbortCalibration from {requester}",
                    )
                else:
                    reason = "another mutating command is already running"
                    self._record_rejected(name, parameters, requester, reason)
                    return None, ua.StatusCodes.BadInvalidState

            now = utc_now()
            command = ActiveCommand(
                command_id=uuid.uuid4().hex,
                name=name,
                requester=self._bounded(requester, COMMAND_REQUESTER_LIMIT),
                role=role,
                parameters=self._bounded(parameters, COMMAND_PARAMETER_LIMIT),
                requested_at=now,
                deadline_monotonic=time.monotonic()
                + self._configured_timeout(
                    "GIZMO_OPCUA_COMMAND_TIMEOUT_SECONDS", 60
                ),
                start_sequence=self._last_good_measurement_sequence,
                start_pid=self._zmon_pid,
                expected_hash=self._sha256_file(ZMON_BINARY),
            )
            self._active_command = command
            self._audit = {
                "id": command.command_id,
                "name": command.name,
                "parameters": command.parameters,
                "requester": command.requester,
                "state": "Accepted",
                "request_time": now,
                "completion_time": None,
                "result": "Accepted; awaiting dispatch",
            }
            self._publish_audit()
            self._publish_gate_state()
            self._persist_command_state()
            return command, None

    def _method_parameters(self, path: str, arguments: list[object]) -> str:
        values = [self._method_value(item) for item in arguments]
        if path == "Operations.StartCalibration" and values:
            payload: dict[str, object] = {"reads_per_point": int(values[0])}
        elif path == "Operations.SetSystemTime" and values:
            value = values[0]
            payload = {
                "requested_time": (
                    value.isoformat() if isinstance(value, dt.datetime) else str(value)
                )
            }
        else:
            payload = {}
        return self._bounded(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            COMMAND_PARAMETER_LIMIT,
        )

    def _remote_call(
        self, session: GizmoInternalSession, params: object
    ) -> list[ua.CallMethodResult]:
        results: list[ua.CallMethodResult] = []
        for request in params:
            identifier = getattr(request.MethodId, "Identifier", "")
            path = (
                identifier.removeprefix("GIZMo.")
                if isinstance(identifier, str) and identifier.startswith("GIZMo.")
                else ""
            )
            if path not in CANONICAL_METHODS:
                results.append(self._call_error(ua.StatusCodes.BadUserAccessDenied))
                continue
            try:
                parameters = self._method_parameters(path, list(request.InputArguments))
            except (TypeError, ValueError):
                parameters = "{}"
            command, status = self._begin_remote_command(session, path, parameters)
            if command is None:
                results.append(self._call_error(status or ua.StatusCodes.BadInvalidState))
                continue
            self._command_context.command_id = command.command_id
            try:
                result = self.server.iserver.method_service.call([request])[0]
            finally:
                self._command_context.command_id = ""
            if not result.StatusCode.is_good():
                self._finish_command(
                    command.command_id,
                    "Failed",
                    f"Method dispatch failed: {result.StatusCode}",
                )
            results.append(result)
        return results

    def _remote_write(
        self, session: GizmoInternalSession, params: object
    ) -> list[ua.StatusCode]:
        writes = list(params.NodesToWrite)
        if len(writes) != 1:
            return [
                ua.StatusCode(ua.StatusCodes.BadTooManyOperations) for _ in writes
            ]
        request = writes[0]
        identifier = getattr(request.NodeId, "Identifier", "")
        path = (
            identifier.removeprefix("GIZMo.")
            if isinstance(identifier, str) and identifier.startswith("GIZMo.")
            else ""
        )
        if path not in CANONICAL_WRITES:
            return [ua.StatusCode(ua.StatusCodes.BadUserAccessDenied)]
        requested = request.Value.Value.Value
        parameters = self._bounded(
            json.dumps({"value": requested}, sort_keys=True, separators=(",", ":")),
            COMMAND_PARAMETER_LIMIT,
        )
        command, status = self._begin_remote_command(session, path, parameters)
        if command is None:
            return [ua.StatusCode(status or ua.StatusCodes.BadInvalidState)]
        result = self.server.iserver.attribute_service.write(params, session.user)
        if not result[0].is_good():
            self._finish_command(
                command.command_id,
                "Failed",
                f"OPC UA write failed: {result[0]}",
            )
            return result
        command.verification = "pending-write"
        command.expected_value = requested
        return result

    def _current_command(self) -> ActiveCommand:
        command_id = getattr(self._command_context, "command_id", "")
        active = self._active_command
        if active is None or active.command_id != command_id:
            raise RuntimeError("mutating method requires an authorized OPC UA session")
        return active

    @staticmethod
    def _configured_timeout(name: str, default: float) -> float:
        try:
            value = float(os.environ.get(name, str(default)))
        except ValueError:
            value = default
        return min(max(value, 1.0), 86_400.0)

    @staticmethod
    def _require_accepted_reply(reply: str) -> str:
        text = reply.strip()
        if not text or text.lower().startswith(("command failed", "unknown command")):
            raise RuntimeError(text or "empty command response")
        return text

    def _command_result(self, result: str) -> list[ua.Variant]:
        bounded = self._bounded(result, COMMAND_RESULT_LIMIT)
        self._update(
            "Configuration.LastCommandResult", bounded, QUALITY_GOOD, utc_now()
        )
        return [ua.Variant(bounded, ua.VariantType.String)]

    def send_legacy_command(self, parent: object, command: object) -> list[ua.Variant]:
        del parent
        reply = self.request(str(self._method_value(command)))
        return [ua.Variant(reply, ua.VariantType.String)]

    def clear_latch_method(self, parent: object) -> list[ua.Variant]:
        del parent
        command = self._current_command()
        try:
            reply = self._require_accepted_reply(self.request("clear_latch"))
            accepted = f"Accepted: {reply}; awaiting fresh alarm readback"
            self._set_command_running(
                command.command_id,
                "clear-latch",
                accepted,
                timeout_seconds=self._configured_timeout(
                    "GIZMO_OPCUA_COMMAND_TIMEOUT_SECONDS", 60
                ),
            )
            return self._command_result(accepted)
        except Exception as error:
            self._finish_command(
                command.command_id, "Failed", f"ClearLatch failed: {error}"
            )
            raise

    def start_calibration_method(
        self, parent: object, reads_per_point: object
    ) -> list[ua.Variant]:
        del parent
        command = self._current_command()
        reads = int(self._method_value(reads_per_point))
        if reads < 1 or reads > 1_000_000:
            self._finish_command(
                command.command_id,
                "Failed",
                "reads_per_point must be between 1 and 1000000",
            )
            raise ValueError("reads_per_point must be between 1 and 1000000")
        self._set_calibration_operation(
            "Starting",
            progress=None,
            result=f"Calibration requested with {reads} reads per point",
            restoration="Unknown",
        )
        try:
            reply = self._require_accepted_reply(self.request(f"CAL {reads}"))
            accepted = f"Accepted: {reply}; calibration progress is not observable"
            self._set_calibration_operation(
                "Running",
                progress=None,
                result=accepted,
                restoration="Unknown",
            )
            self._set_command_running(
                command.command_id,
                "calibration",
                accepted,
                timeout_seconds=self._configured_timeout(
                    "GIZMO_OPCUA_CALIBRATION_TIMEOUT_SECONDS", 1800
                ),
                expected_value=reads,
                terminal_calibration_state="Completed",
            )
            return self._command_result(accepted)
        except Exception as error:
            message = f"StartCalibration failed: {error}"
            self._set_calibration_operation(
                "Failed",
                progress=None,
                result=message,
                restoration="Unknown",
                terminal=True,
            )
            self._finish_command(command.command_id, "Failed", message)
            raise

    def capture_adc_method(self, parent: object) -> list[ua.Variant]:
        del parent
        command = self._current_command()
        try:
            reply = self._require_accepted_reply(self.request("read_adc"))
            self._adc_ready_at = time.monotonic() + 5
            accepted = f"Accepted: {reply}; awaiting bounded ADC file and normal acquisition"
            self._set_command_running(
                command.command_id,
                "capture-adc",
                accepted,
                timeout_seconds=self._configured_timeout(
                    "GIZMO_OPCUA_COMMAND_TIMEOUT_SECONDS", 60
                ),
                expected_value=command.requested_at.timestamp(),
            )
            return self._command_result(accepted)
        except Exception as error:
            self._finish_command(
                command.command_id, "Failed", f"CaptureAdc failed: {error}"
            )
            raise

    def normalize_magnitude_method(self, parent: object) -> list[ua.Variant]:
        del parent
        command = self._current_command()
        try:
            path = state_path("normalizeMagFlag.env")
            atomic_write(path, "normalizeMagFlag=1\n")
            if read_exported_int(path.name, "normalizeMagFlag", 0) != 1:
                raise RuntimeError("normalization request did not read back")
            result = "Magnitude normalization requested and state-file write verified"
            self._finish_command(command.command_id, "Succeeded", result)
            self._next_run["Calibration"] = 0.0
            return self._command_result(result)
        except Exception as error:
            self._finish_command(
                command.command_id,
                "Failed",
                f"NormalizeMagnitude failed: {error}",
            )
            raise

    def set_system_time_method(
        self, parent: object, requested_time: object
    ) -> list[ua.Variant]:
        del parent
        command = self._current_command()
        requested = self._method_value(requested_time)
        if not isinstance(requested, dt.datetime):
            self._finish_command(
                command.command_id,
                "Failed",
                "requested_time must be an OPC UA DateTime",
            )
            raise ValueError("requested_time must be an OPC UA DateTime")
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=dt.timezone.utc)
        epoch_seconds = int(requested.timestamp())
        if epoch_seconds < 946_684_800 or epoch_seconds > 4_102_444_800:
            self._finish_command(
                command.command_id,
                "Failed",
                "requested_time must be between 2000 and 2100",
            )
            raise ValueError("requested_time must be between 2000 and 2100")
        try:
            reply = self._require_accepted_reply(
                self.request(f"set_time_epoch {epoch_seconds}")
            )
            accepted = f"Accepted: {reply}; awaiting clock and acquisition readback"
            self._next_run["Time"] = 0.0
            self._set_command_running(
                command.command_id,
                "set-time",
                accepted,
                timeout_seconds=self._configured_timeout(
                    "GIZMO_OPCUA_COMMAND_TIMEOUT_SECONDS", 60
                ),
                expected_value=epoch_seconds,
            )
            return self._command_result(accepted)
        except Exception as error:
            self._finish_command(
                command.command_id, "Failed", f"SetSystemTime failed: {error}"
            )
            raise

    def restart_measurement_engine_method(
        self, parent: object
    ) -> list[ua.Variant]:
        del parent
        command = self._current_command()
        try:
            reply = self._require_accepted_reply(
                self.request(f"run {self._last_run_interval}")
            )
            accepted = (
                f"Accepted: {reply}; awaiting new PID, executable hash, "
                "and fresh measurement"
            )
            self._set_command_running(
                command.command_id,
                "restart",
                accepted,
                timeout_seconds=self._configured_timeout(
                    "GIZMO_OPCUA_COMMAND_TIMEOUT_SECONDS", 60
                ),
            )
            return self._command_result(accepted)
        except Exception as error:
            self._finish_command(
                command.command_id,
                "Failed",
                f"RestartMeasurementEngine failed: {error}",
            )
            raise

    def abort_calibration_method(self, parent: object) -> list[ua.Variant]:
        del parent
        command = self._current_command()
        self._set_calibration_operation(
            "Restoring",
            progress=None,
            result="Calibration abort accepted; commanding normal acquisition",
            restoration="Commanded",
        )
        try:
            reply = self._require_accepted_reply(
                self.request(f"run {self._last_run_interval}")
            )
            accepted = f"Accepted: {reply}; awaiting verified normal acquisition"
            self._set_command_running(
                command.command_id,
                "restore",
                accepted,
                timeout_seconds=self._configured_timeout(
                    "GIZMO_OPCUA_RESTORE_TIMEOUT_SECONDS", 90
                ),
                terminal_calibration_state="Aborted",
            )
            return self._command_result(accepted)
        except Exception as error:
            message = f"AbortCalibration restoration failed: {error}"
            self._set_calibration_operation(
                "Failed",
                progress=None,
                result=message,
                restoration="Failed",
                terminal=True,
            )
            self._finish_command(
                command.command_id, "Failed", message, fault_locked=True
            )
            raise

    def restore_normal_state_method(self, parent: object) -> list[ua.Variant]:
        del parent
        command = self._current_command()
        self._set_calibration_operation(
            "Restoring",
            progress=None,
            result="Normal acquisition requested",
            restoration="Commanded",
        )
        try:
            reply = self._require_accepted_reply(
                self.request(f"run {self._last_run_interval}")
            )
            accepted = f"Accepted: {reply}; awaiting verified normal acquisition"
            self._set_command_running(
                command.command_id,
                "restore",
                accepted,
                timeout_seconds=self._configured_timeout(
                    "GIZMO_OPCUA_RESTORE_TIMEOUT_SECONDS", 90
                ),
                terminal_calibration_state="Idle",
            )
            return self._command_result(accepted)
        except Exception as error:
            message = f"RestoreNormalState failed: {error}"
            self._set_calibration_operation(
                "Failed",
                progress=None,
                result=message,
                restoration="Failed",
                terminal=True,
            )
            self._finish_command(
                command.command_id, "Failed", message, fault_locked=True
            )
            raise

    @staticmethod
    def csv_as_text(name: str) -> str:
        try:
            lines = state_path(name).read_text(encoding="utf-8").splitlines()
            return ", ".join(line.strip() for line in lines)
        except OSError as error:
            return f"Unable to read {name}: {error}"

    def _collect_measurement(self) -> tuple[str, MeasurementSnapshot]:
        raw = self.request("get_data")
        self._measurement_sequence += 1
        snapshot = parse_legacy_measurement(
            raw,
            sequence=self._measurement_sequence,
            averages_per_calculation=read_exported_int(
                "setRunInterval.env", "runInterval", 100
            ),
        )
        return raw, snapshot

    @staticmethod
    def _collect_thermal() -> tuple[str, ThermalSnapshot]:
        raw = bytearray()
        with socket.create_connection(
            (TEMPERATURE_HOST, TEMPERATURE_PORT), timeout=3
        ) as client:
            client.settimeout(3)
            while len(raw) < 4096 and b"\n" not in raw:
                chunk = client.recv(1024)
                if not chunk:
                    break
                raw.extend(chunk)
        if not raw:
            raise ConnectionError("temperature service closed without data")
        record = raw.decode("utf-8", errors="replace").strip()
        return record, parse_legacy_thermals(record)

    @staticmethod
    def _collect_sdr() -> tuple[dt.datetime, list[int]]:
        frame_size = SDR_SAMPLE_COUNT * 4
        raw = bytearray(frame_size)
        view = memoryview(raw)
        received = 0
        with socket.create_connection((SDR_HOST, SDR_PORT), timeout=3) as client:
            client.settimeout(3)
            while received < frame_size:
                count = client.recv_into(view[received:], frame_size - received)
                if count == 0:
                    raise ConnectionError("SDR service closed before a complete frame")
                received += count
        return utc_now(), np.frombuffer(raw, dtype="<i4").tolist()

    def _apply_measurement(self, result: tuple[str, MeasurementSnapshot]) -> None:
        raw, snapshot = result
        stamp = snapshot.sampled_at
        self._last_measurement_snapshot = snapshot
        if snapshot.quality == QUALITY_GOOD:
            self._last_good_measurement_sequence = snapshot.sequence
            self._last_good_measurement_at = stamp
        self.legacy_data.set_value(raw)
        self._update("Measurement.Sequence", snapshot.sequence, snapshot.quality, stamp)
        self._update("Measurement.SampleTime", stamp, snapshot.quality, stamp)
        resistance_quality = (
            QUALITY_GOOD
            if snapshot.resistance_range == RANGE_OUT_OF_RANGE
            or snapshot.resistance_ohm is not None
            else QUALITY_NOT_AVAILABLE
        )
        resistance_status = (
            ua.StatusCodes.Good
            if snapshot.resistance_range == RANGE_OUT_OF_RANGE
            else None
        )
        self._update(
            "Measurement.ResistanceOhm",
            snapshot.resistance_ohm,
            resistance_quality,
            stamp,
            status_code=resistance_status,
        )
        self._update(
            "Measurement.ResistanceRange",
            snapshot.resistance_range,
            snapshot.quality,
            stamp,
        )
        self._update(
            "Measurement.CapacitanceNanofarad",
            snapshot.capacitance_nf,
            (
                QUALITY_GOOD
                if snapshot.capacitance_nf is not None
                else QUALITY_NOT_AVAILABLE
            ),
            stamp,
        )
        self._update(
            "Measurement.CapacitanceRange",
            snapshot.capacitance_range,
            snapshot.quality,
            stamp,
        )
        self._update(
            "Measurement.StimulusCurrentRmsAmpere",
            None,
            QUALITY_NOT_AVAILABLE,
            stamp,
            status_code=ua.StatusCodes.BadNotSupported,
        )
        for path, value in (
            ("Measurement.ThresholdOhm", snapshot.threshold_ohm),
            (
                "Measurement.StimulusFrequencyHertz",
                snapshot.stimulus_frequency_hz,
            ),
            ("Measurement.MagnitudeCount", snapshot.magnitude_count),
            ("Measurement.PhaseAtanDegrees", snapshot.phase_atan_deg),
            ("Measurement.PhaseAtan2Degrees", snapshot.phase_atan2_deg),
            (
                "Measurement.PhaseInterpolatedDegrees",
                snapshot.phase_interpolated_deg,
            ),
            ("Measurement.InPhaseCount", snapshot.in_phase_count),
            ("Measurement.QuadratureCount", snapshot.quadrature_count),
        ):
            self._update(path, value, snapshot.quality, stamp)
        self._update(
            "Measurement.AveragesPerCalculation",
            snapshot.averages_per_calculation,
            snapshot.quality,
            stamp,
        )
        self._update("Measurement.Quality", snapshot.quality, QUALITY_GOOD, stamp)
        self._update(
            "Measurement.Diagnostic",
            snapshot.diagnostic,
            QUALITY_GOOD,
            stamp,
        )
        self._update(
            "Measurement.LegacyRecord",
            snapshot.raw_record,
            snapshot.quality,
            stamp,
        )
        self._update("Alarm.Latched", snapshot.alarm_latched, snapshot.quality, stamp)
        self._update("Alarm.Active", snapshot.alarm_active, snapshot.quality, stamp)
        self._update("Alarm.Reason", snapshot.alarm_reason, snapshot.quality, stamp)
        self._update(
            "Alarm.LatchTime",
            snapshot.latch_time,
            (
                snapshot.quality
                if snapshot.latch_time is not None
                else QUALITY_NOT_AVAILABLE
            ),
            stamp,
        )
        self._has_data.add("Measurement")
        self._category_quality["Measurement"] = snapshot.quality
        self._update_health()

    def _apply_thermal(self, result: tuple[str, ThermalSnapshot]) -> None:
        raw, snapshot = result
        self.legacy_thermals.set_value(raw)
        for key, value in snapshot.sensors_celsius.items():
            self._update(
                f"Thermal.{key}TemperatureCelsius",
                value,
                QUALITY_GOOD if value is not None else QUALITY_NOT_AVAILABLE,
                snapshot.sampled_at,
            )
        self._apply_quality(
            "Thermal", snapshot.quality, snapshot.diagnostic, snapshot.sampled_at
        )

    def _apply_time(self, snapshot: TimeSnapshot) -> None:
        stamp = snapshot.observed_at
        self._latest_time_snapshot = snapshot
        local_time = stamp.astimezone().isoformat(timespec="seconds")
        for path, value in (
            ("Time.CurrentUtc", stamp),
            ("Time.CurrentLocal", local_time),
            ("Time.BootTime", snapshot.boot_time),
            ("Time.UptimeSeconds", snapshot.uptime_seconds),
            ("Time.MonotonicNanoseconds", snapshot.monotonic_ns),
            ("Time.TimezoneName", snapshot.timezone_name),
            ("Time.UtcOffsetSeconds", snapshot.utc_offset_seconds),
            ("Time.NtpSynchronized", snapshot.ntp_synchronized),
            ("Time.NtpServiceActive", snapshot.ntp_service_active),
            ("Time.NtpService", snapshot.ntp_service),
            ("Time.RtcPresent", snapshot.rtc_present),
            ("Time.RtcDevice", snapshot.rtc_device),
            ("Time.CurrentClocksource", snapshot.current_clocksource),
            ("Time.AvailableClocksources", snapshot.available_clocksources),
        ):
            self._update(path, value, snapshot.quality, stamp)
        self._apply_quality("Time", snapshot.quality, snapshot.diagnostic, stamp)

    def _apply_host(self, snapshot: HostSnapshot) -> None:
        stamp = snapshot.observed_at
        for path, value in (
            ("OperatingSystem.Hostname", snapshot.hostname),
            ("OperatingSystem.PrettyName", snapshot.os_pretty_name),
            ("OperatingSystem.VersionId", snapshot.os_version_id),
            ("OperatingSystem.KernelRelease", snapshot.kernel_release),
            ("OperatingSystem.KernelVersion", snapshot.kernel_version),
            ("OperatingSystem.Architecture", snapshot.architecture),
            ("OperatingSystem.LogicalCpuCount", snapshot.logical_cpu_count),
            (
                "OperatingSystem.CpuUtilizationPercent",
                snapshot.cpu_utilization_percent,
            ),
            ("OperatingSystem.Load1Minute", snapshot.load_1m),
            ("OperatingSystem.Load5Minute", snapshot.load_5m),
            ("OperatingSystem.Load15Minute", snapshot.load_15m),
            ("OperatingSystem.MemoryTotalBytes", snapshot.memory_total_bytes),
            (
                "OperatingSystem.MemoryAvailableBytes",
                snapshot.memory_available_bytes,
            ),
            ("OperatingSystem.MemoryUsedBytes", snapshot.memory_used_bytes),
            ("OperatingSystem.SwapTotalBytes", snapshot.swap_total_bytes),
            ("OperatingSystem.SwapFreeBytes", snapshot.swap_free_bytes),
            ("OperatingSystem.ProcessCount", snapshot.process_count),
            (
                "OperatingSystem.RunningProcessCount",
                snapshot.running_process_count,
            ),
            (
                "OperatingSystem.EntropyAvailableBits",
                snapshot.entropy_available_bits,
            ),
            (
                "OperatingSystem.OpenFileHandles",
                snapshot.open_file_handles,
            ),
        ):
            self._update(path, value, snapshot.quality, stamp)
        self._apply_quality(
            "OperatingSystem", snapshot.quality, snapshot.diagnostic, stamp
        )

    def _apply_network(self, snapshot: NetworkSnapshot) -> None:
        stamp = snapshot.observed_at
        observed_names = {interface.name for interface in snapshot.interfaces}
        for name, points in self._interface_points.items():
            if name not in observed_names:
                self._update(points["Present"], False, QUALITY_UNCERTAIN, stamp)
        for interface in snapshot.interfaces:
            points = self._ensure_interface_points(interface.name)
            values = {
                "Name": interface.name,
                "Present": True,
                "Index": interface.index,
                "MacAddress": interface.mac_address,
                "PermanentMacAddress": interface.permanent_mac_address,
                "MacAssignmentCode": interface.mac_assignment_code,
                "MacAddressSource": interface.mac_address_source,
                "AdministrativeUp": interface.administrative_up,
                "Carrier": interface.carrier,
                "OperationalState": interface.operational_state,
                "Mtu": interface.mtu,
                "SpeedMegabitPerSecond": interface.speed_mbps,
                "Duplex": interface.duplex,
                "Driver": interface.driver,
                "Addresses": interface.addresses,
                "RxBytes": interface.rx_bytes,
                "RxPackets": interface.rx_packets,
                "RxErrors": interface.rx_errors,
                "RxDropped": interface.rx_dropped,
                "TxBytes": interface.tx_bytes,
                "TxPackets": interface.tx_packets,
                "TxErrors": interface.tx_errors,
                "TxDropped": interface.tx_dropped,
                "Collisions": interface.collisions,
            }
            for field, value in values.items():
                self._update(points[field], value, snapshot.quality, stamp)
        self._update(
            "Network.ExpectedInterfaces",
            snapshot.expected_interfaces,
            QUALITY_GOOD,
            stamp,
        )
        self._update(
            "Network.MissingInterfaces",
            snapshot.missing_interfaces,
            QUALITY_GOOD,
            stamp,
        )
        self._update("Network.Routes", snapshot.routes, snapshot.quality, stamp)
        self._update(
            "Network.DnsServers", snapshot.dns_servers, snapshot.quality, stamp
        )
        self._update(
            "Network.DomainName", snapshot.domain_name, snapshot.quality, stamp
        )
        self._apply_quality("Network", snapshot.quality, snapshot.diagnostic, stamp)

    def _apply_storage(self, snapshot: StorageSnapshot) -> None:
        stamp = snapshot.observed_at
        for filesystem in snapshot.filesystems:
            points = self._ensure_filesystem_points(filesystem.key)
            values = {
                "MountPoint": filesystem.mount_point,
                "Source": filesystem.source,
                "Type": filesystem.filesystem_type,
                "TotalBytes": filesystem.total_bytes,
                "UsedBytes": filesystem.used_bytes,
                "AvailableBytes": filesystem.available_bytes,
                "UsedPercent": filesystem.used_percent,
                "ReadOnly": filesystem.read_only,
                "Quality": filesystem.quality,
            }
            for field, value in values.items():
                self._update(points[field], value, filesystem.quality, stamp)
        self._apply_quality("Storage", snapshot.quality, snapshot.diagnostic, stamp)

    def _apply_firmware(self, snapshot: FirmwareSnapshot) -> None:
        stamp = snapshot.observed_at
        for path, value in (
            ("Firmware.RuntimeVersion", snapshot.runtime_version),
            ("Firmware.OverlayName", snapshot.overlay_name),
            ("Firmware.OverlayInstalled", snapshot.overlay_installed),
            ("Firmware.OverlayLoaded", snapshot.overlay_loaded),
            ("Firmware.OverlayState", snapshot.overlay_state),
            ("Firmware.OverlayPath", snapshot.overlay_path),
            (
                "Firmware.OverlayBitstreamSha256",
                snapshot.overlay_bitstream_sha256,
            ),
            ("Firmware.DeviceTreeOverlay", snapshot.device_tree_overlay),
            (
                "Firmware.DeviceTreeOverlaySha256",
                snapshot.device_tree_overlay_sha256,
            ),
            ("Firmware.ShellName", snapshot.shell_name),
            ("Firmware.ExpectedDevices", snapshot.expected_devices),
            ("Firmware.MissingDevices", snapshot.missing_devices),
            ("Firmware.BoardModel", snapshot.board_model),
            ("Firmware.BoardSerialNumber", snapshot.board_serial_number),
            ("Firmware.CarrierManufacturer", snapshot.carrier_manufacturer),
            ("Firmware.CarrierProductName", snapshot.carrier_product_name),
            ("Firmware.CarrierPartNumber", snapshot.carrier_part_number),
            ("Firmware.CarrierSerialNumber", snapshot.carrier_serial_number),
            ("Firmware.CarrierRevision", snapshot.carrier_revision),
            (
                "Firmware.FactoryMacAddresses",
                snapshot.factory_mac_addresses,
            ),
        ):
            self._update(path, value, snapshot.quality, stamp)
        self._apply_quality("Firmware", snapshot.quality, snapshot.diagnostic, stamp)

    def _apply_services(self, snapshot: ServiceInventorySnapshot) -> None:
        stamp = snapshot.observed_at
        for service in snapshot.services:
            if service.unit == "gizmo-zmon.service":
                self._zmon_pid = service.main_pid
            points = self._ensure_service_points(service.unit)
            values = {
                "Unit": service.unit,
                "Description": service.description,
                "LoadState": service.load_state,
                "ActiveState": service.active_state,
                "SubState": service.sub_state,
                "Result": service.result,
                "MainPid": service.main_pid,
                "RestartCount": service.restart_count,
                "ExitStatus": service.exit_status,
                "ActiveSince": service.active_since,
                "StatusText": service.status_text,
                "Required": service.required,
                "Quality": service.quality,
            }
            for field, value in values.items():
                self._update(points[field], value, service.quality, stamp)
        self._apply_quality("Services", snapshot.quality, snapshot.diagnostic, stamp)

    def _apply_calibration(self, snapshot: CalibrationSnapshot) -> None:
        stamp = snapshot.observed_at
        for path, value in (
            ("Calibration.State", snapshot.state),
            (
                "Calibration.ConfiguredThresholdOhm",
                snapshot.configured_threshold_ohm,
            ),
            (
                "Calibration.MeasurementsPerCalculation",
                snapshot.measurements_per_calculation,
            ),
            (
                "Calibration.MagnitudeNormalizationPending",
                snapshot.magnitude_normalization_pending,
            ),
            ("Calibration.LastCalibrationTime", snapshot.last_calibration_at),
        ):
            self._update(path, value, snapshot.quality, stamp)
        self.legacy_resistance_calibration.set_value(
            self.csv_as_text("Rcalibration_ph.csv")
        )
        self.legacy_capacitance_calibration.set_value(
            self.csv_as_text("Ccalibration_ph.csv")
        )
        for table in snapshot.tables:
            points = self._calibration_points[table.key]
            quality = QUALITY_GOOD if table.state == "Valid" else QUALITY_BAD
            values = {
                "Kind": table.kind,
                "Path": table.path,
                "State": table.state,
                "Sha256": table.sha256,
                "ModifiedTime": table.modified_at,
                "RowCount": table.row_count,
                "InputMinimum": table.input_min,
                "InputMaximum": table.input_max,
                "InputUnit": table.input_unit,
                "Format": table.format,
            }
            for field, value in values.items():
                self._update(points[field], value, quality, stamp)
        self._apply_quality("Calibration", snapshot.quality, snapshot.diagnostic, stamp)

    def _apply_sdr(self, result: tuple[dt.datetime, list[int]]) -> None:
        stamp, frame = result
        self._sdr_sequence += 1
        self.legacy_sdr.set_value(ua.Variant(frame, ua.VariantType.Int32))
        for path, value in (
            ("SDR.Available", True),
            ("SDR.FrameSequence", self._sdr_sequence),
            ("SDR.SampleTime", stamp),
            ("SDR.LatestFrame", frame),
        ):
            self._update(path, value, QUALITY_GOOD, stamp)
        self._apply_quality("SDR", QUALITY_GOOD, "", stamp)

    def _apply_quality(
        self, prefix: str, quality: str, diagnostic: str, stamp: dt.datetime
    ) -> None:
        self._update(f"{prefix}.Quality", quality, QUALITY_GOOD, stamp)
        self._update(f"{prefix}.Diagnostic", diagnostic, QUALITY_GOOD, stamp)
        self._update(f"{prefix}.LastUpdate", stamp, QUALITY_GOOD, stamp)
        self._has_data.add(prefix)
        self._category_quality[prefix] = quality
        self._update_health()

    def _update_health(self) -> None:
        categories = (
            "Measurement",
            "Thermal",
            "Time",
            "OperatingSystem",
            "Network",
            "Storage",
            "Firmware",
            "Services",
            "Calibration",
            "SDR",
        )
        essential = {
            "Measurement",
            "Time",
            "Firmware",
            "Services",
            "Calibration",
        }
        states = {
            category: self._category_quality.get(category, QUALITY_NOT_AVAILABLE)
            for category in categories
        }
        if self._attempted < set(categories):
            overall = "Starting"
        elif any(
            states[category] in {QUALITY_BAD, QUALITY_NOT_AVAILABLE}
            for category in essential
        ):
            overall = "Failed"
        elif any(value != QUALITY_GOOD for value in states.values()):
            overall = "Degraded"
        else:
            overall = "OK"
        stamp = utc_now()
        for category, quality in states.items():
            self._update(
                f"Health.{category}",
                quality if category in self._attempted else "Starting",
                QUALITY_GOOD,
                stamp,
            )
        self._update("Health.Overall", overall, QUALITY_GOOD, stamp)
        self._update("Health.LastUpdate", stamp, QUALITY_GOOD, stamp)
        if self._ready_sent and overall != self._last_notified_health:
            notify_systemd(f"STATUS=Canonical OPC UA namespace ready; {overall}")
            self._last_notified_health = overall

    def _job_specs(
        self,
    ) -> dict[str, tuple[float, Callable[[], object], Callable[[object], None]]]:
        return {
            "Measurement": (
                MEASUREMENT_INTERVAL,
                self._collect_measurement,
                self._apply_measurement,
            ),
            "Thermal": (
                MEASUREMENT_INTERVAL,
                self._collect_thermal,
                self._apply_thermal,
            ),
            "SDR": (SDR_INTERVAL, self._collect_sdr, self._apply_sdr),
            "Time": (
                MEASUREMENT_INTERVAL,
                self._collectors.collect_time,
                self._apply_time,
            ),
            "OperatingSystem": (
                PLATFORM_INTERVAL,
                self._collectors.collect_host,
                self._apply_host,
            ),
            "Network": (
                PLATFORM_INTERVAL,
                self._collectors.collect_network,
                self._apply_network,
            ),
            "Storage": (
                INVENTORY_INTERVAL,
                self._collectors.collect_storage,
                self._apply_storage,
            ),
            "Firmware": (
                INVENTORY_INTERVAL,
                self._collectors.collect_firmware,
                self._apply_firmware,
            ),
            "Services": (
                PLATFORM_INTERVAL,
                self._collectors.collect_services,
                self._apply_services,
            ),
            "Calibration": (
                INVENTORY_INTERVAL,
                self._collectors.collect_calibration,
                self._apply_calibration,
            ),
        }

    def _schedule_jobs(self, now: float) -> None:
        for name, (interval, collector, _) in self._job_specs().items():
            if name in self._jobs:
                continue
            if now < self._next_run.get(name, 0.0):
                continue
            self._jobs[name] = self._executor.submit(collector)
            self._next_run[name] = now + max(0.1, interval)

    def _finish_jobs(self) -> None:
        specs = self._job_specs()
        for name, future in list(self._jobs.items()):
            if not future.done():
                continue
            del self._jobs[name]
            self._attempted.add(name)
            try:
                result = future.result()
                specs[name][2](result)
            except Exception as error:  # noqa: BLE001 - isolate collector failures
                print(f"{name} collection failed: {error}", flush=True)
                self._mark_prefix_failed(name, error)

    def _forward_configuration_writes(self) -> None:
        active = self._active_command
        requested_thresholds = (
            int(self.configuration_threshold.get_value()),
            int(self.legacy_threshold.get_value()),
        )
        requested_threshold = next(
            (value for value in requested_thresholds if value != self._last_threshold),
            self._last_threshold,
        )
        if requested_threshold != self._last_threshold:
            if not THRESHOLD_MIN_OHM <= requested_threshold <= THRESHOLD_MAX_OHM:
                self.configuration_threshold.set_value(self._last_threshold)
                self.legacy_threshold.set_value(self._last_threshold)
                raise ValueError(
                    f"threshold must be between {THRESHOLD_MIN_OHM} "
                    f"and {THRESHOLD_MAX_OHM}"
                )
            try:
                result = self.request(f"set_th {requested_threshold}")
            except (OSError, RuntimeError, zmq.ZMQError):
                self.configuration_threshold.set_value(self._last_threshold)
                self.legacy_threshold.set_value(self._last_threshold)
                raise
            self._last_threshold = requested_threshold
            self.configuration_threshold.set_value(requested_threshold)
            self.legacy_threshold.set_value(requested_threshold)
            self._update(
                "Configuration.LastCommandResult",
                result,
                QUALITY_GOOD,
                utc_now(),
            )
            if active is not None and active.name == "ThresholdOhm":
                self._set_command_running(
                    active.command_id,
                    "threshold",
                    f"Accepted: {self._require_accepted_reply(result)}; awaiting measurement readback",
                    timeout_seconds=self._configured_timeout(
                        "GIZMO_OPCUA_COMMAND_TIMEOUT_SECONDS", 60
                    ),
                    expected_value=requested_threshold,
                )
        elif (
            active is not None
            and active.name == "ThresholdOhm"
            and active.verification == "pending-write"
        ):
            self._finish_command(
                active.command_id,
                "Succeeded",
                f"Threshold already read back as {self._last_threshold} ohm; no restart required",
            )

        requested_intervals = (
            int(self.configuration_interval.get_value()),
            int(self.legacy_measurements.get_value()),
        )
        requested_interval = next(
            (
                value
                for value in requested_intervals
                if value != self._last_run_interval
            ),
            self._last_run_interval,
        )
        if requested_interval != self._last_run_interval:
            if not 1 <= requested_interval <= 1_000_000:
                self.configuration_interval.set_value(self._last_run_interval)
                self.legacy_measurements.set_value(self._last_run_interval)
                raise ValueError(
                    "averages per calculation must be between 1 and 1000000"
                )
            try:
                result = self.request(f"run {requested_interval}")
            except (OSError, RuntimeError, zmq.ZMQError):
                self.configuration_interval.set_value(self._last_run_interval)
                self.legacy_measurements.set_value(self._last_run_interval)
                raise
            self._last_run_interval = requested_interval
            self.configuration_interval.set_value(requested_interval)
            self.legacy_measurements.set_value(requested_interval)
            self._update(
                "Configuration.LastCommandResult",
                result,
                QUALITY_GOOD,
                utc_now(),
            )
            if active is not None and active.name == "AveragesPerCalculation":
                self._set_command_running(
                    active.command_id,
                    "averages",
                    f"Accepted: {self._require_accepted_reply(result)}; awaiting measurement readback",
                    timeout_seconds=self._configured_timeout(
                        "GIZMO_OPCUA_COMMAND_TIMEOUT_SECONDS", 60
                    ),
                    expected_value=requested_interval,
                )
        elif (
            active is not None
            and active.name == "AveragesPerCalculation"
            and active.verification == "pending-write"
        ):
            self._finish_command(
                active.command_id,
                "Succeeded",
                f"Averaging count already read back as {self._last_run_interval}; no restart required",
            )

        requested_time = self.legacy_set_time.get_value()
        if requested_time != self._last_time:
            try:
                result = self.request(f"set_time {requested_time}")
            except (OSError, RuntimeError, zmq.ZMQError):
                self.legacy_set_time.set_value(self._last_time)
                raise
            print(result, flush=True)
            self._last_time = requested_time

        if self.legacy_clear_latch.get_value() == "clear_latch":
            self.legacy_clear_latch.set_value("")
            print(self.request("clear_latch"), flush=True)

        if self.legacy_calibrate.get_value() == 1:
            self.legacy_calibrate.set_value(0)
            print(self.request(f"CAL {self._last_run_interval}"), flush=True)

        if self.legacy_read_adc.get_value() == 1:
            self.legacy_read_adc.set_value(0)
            self.legacy_csv_data.set_value("")
            print(self.request("read_adc"), flush=True)
            self._adc_ready_at = time.monotonic() + 5

        if self.legacy_normalize.get_value() == 1:
            atomic_write(
                state_path("normalizeMagFlag.env"),
                "normalizeMagFlag=1\n",
            )
            self.legacy_normalize.set_value(0)

    def _fresh_measurement_for(self, command: ActiveCommand) -> bool:
        return bool(
            self._last_good_measurement_sequence > command.start_sequence
            and self._last_good_measurement_at is not None
            and self._last_good_measurement_at >= command.requested_at
        )

    def _normal_mode_verified(self, command: ActiveCommand) -> tuple[bool, str]:
        if not self._fresh_measurement_for(command):
            return False, "waiting for a fresh good measurement"
        if self._zmon_pid <= 0:
            return False, "waiting for an active measurement-engine PID"
        if command.start_pid > 0 and self._zmon_pid == command.start_pid:
            return False, "waiting for a new measurement-engine PID"
        observed_hash = self._sha256_file(ZMON_BINARY)
        if not command.expected_hash:
            return False, "expected measurement-engine hash is unavailable"
        if observed_hash != command.expected_hash:
            return False, "measurement-engine executable hash changed"
        evidence = (
            f"PID {self._zmon_pid}, SHA-256 {observed_hash}, "
            f"fresh sequence {self._last_good_measurement_sequence}"
        )
        return True, evidence

    def _advance_active_command(self, now: float) -> None:
        with self._command_lock:
            command = self._active_command
            if command is None or command.verification in {
                "dispatching",
                "pending-write",
            }:
                return

            if command.verification == "clear-latch":
                if self._fresh_measurement_for(command):
                    snapshot = self._last_measurement_snapshot
                    if snapshot is not None and not snapshot.alarm_latched:
                        self._finish_command(
                            command.command_id,
                            "Succeeded",
                            (
                                "Latch clear verified by fresh measurement "
                                f"sequence {snapshot.sequence}"
                            ),
                        )
                        return
                    if snapshot is not None and snapshot.alarm_active:
                        self._finish_command(
                            command.command_id,
                            "Succeeded",
                            (
                                "Latch clear was accepted; the active physical alarm "
                                f"remains asserted at sequence {snapshot.sequence}"
                            ),
                        )
                        return

            verified, evidence = self._normal_mode_verified(command)
            if verified:
                snapshot = self._last_measurement_snapshot
                if command.verification == "threshold":
                    if (
                        snapshot is not None
                        and snapshot.threshold_ohm is not None
                        and command.expected_value is not None
                        and int(snapshot.threshold_ohm)
                        == int(command.expected_value)
                    ):
                        self._finish_command(
                            command.command_id,
                            "Succeeded",
                            f"Threshold readback {int(command.expected_value)} ohm verified; {evidence}",
                        )
                        return
                elif command.verification == "averages":
                    if snapshot is not None and int(
                        snapshot.averages_per_calculation
                    ) == int(command.expected_value):
                        self._finish_command(
                            command.command_id,
                            "Succeeded",
                            f"Averaging readback {int(command.expected_value)} verified; {evidence}",
                        )
                        return
                elif command.verification == "restart":
                    self._finish_command(
                        command.command_id,
                        "Succeeded",
                        f"Measurement-engine restart verified; {evidence}",
                    )
                    return
                elif command.verification == "calibration":
                    result = (
                        "Calibration sweep completed and normal acquisition resumed; "
                        "new tables remain subject to independent validation and activation; "
                        f"{evidence}"
                    )
                    self._set_calibration_operation(
                        "Completed",
                        progress=100.0,
                        result=result,
                        restoration="Verified",
                        terminal=True,
                    )
                    self._next_run["Calibration"] = 0.0
                    self._finish_command(command.command_id, "Succeeded", result)
                    return
                elif command.verification == "calibration-timeout-restore":
                    result = f"Calibration timed out; normal acquisition restoration verified; {evidence}"
                    self._set_calibration_operation(
                        "Failed",
                        progress=None,
                        result=result,
                        restoration="Verified",
                        terminal=True,
                    )
                    self._finish_command(command.command_id, "TimedOut", result)
                    return
                elif command.verification == "restore":
                    terminal = command.terminal_calibration_state or "Idle"
                    result = f"Normal acquisition restoration verified; {evidence}"
                    self._command_fault_locked = False
                    self._set_calibration_operation(
                        terminal,
                        progress=None,
                        result=result,
                        restoration="Verified",
                        terminal=True,
                    )
                    self._finish_command(command.command_id, "Succeeded", result)
                    return
                elif command.verification == "set-time":
                    observed = self._latest_time_snapshot
                    if observed is not None and abs(
                        observed.observed_at.timestamp() - float(command.expected_value)
                    ) <= 5:
                        self._finish_command(
                            command.command_id,
                            "Succeeded",
                            f"System-time and acquisition readback verified; {evidence}",
                        )
                        return
                elif command.verification == "capture-adc":
                    try:
                        status = state_path("adc.csv").stat()
                    except OSError:
                        status = None
                    max_bytes = int(
                        os.environ.get("GIZMO_OPCUA_ADC_MAX_BYTES", str(16 * 1024**2))
                    )
                    if (
                        status is not None
                        and status.st_mtime >= float(command.expected_value) - 1
                        and 0 < status.st_size <= max_bytes
                    ):
                        self._finish_command(
                            command.command_id,
                            "Succeeded",
                            f"ADC capture {status.st_size} bytes and normal acquisition verified; {evidence}",
                        )
                        return

            if now < command.deadline_monotonic:
                return

            if command.verification == "calibration" and not command.restore_attempted:
                try:
                    reply = self._require_accepted_reply(
                        self.request(f"run {self._last_run_interval}")
                    )
                    command.restore_attempted = True
                    command.verification = "calibration-timeout-restore"
                    command.start_sequence = self._last_good_measurement_sequence
                    command.start_pid = self._zmon_pid
                    command.deadline_monotonic = now + self._configured_timeout(
                        "GIZMO_OPCUA_RESTORE_TIMEOUT_SECONDS", 90
                    )
                    self._audit["result"] = self._bounded(
                        f"Calibration timed out; restoration commanded: {reply}",
                        COMMAND_RESULT_LIMIT,
                    )
                    self._set_calibration_operation(
                        "Restoring",
                        progress=None,
                        result=self._audit["result"],
                        restoration="Commanded",
                    )
                    self._publish_audit()
                    self._persist_command_state()
                    self._next_run["Measurement"] = 0.0
                    self._next_run["Services"] = 0.0
                    return
                except Exception as error:
                    evidence = f"automatic normal-state restoration failed: {error}"

            fault = command.verification in {
                "calibration",
                "calibration-timeout-restore",
                "restore",
                "restart",
            }
            result = f"Command verification timed out: {evidence}"
            if command.verification in {
                "calibration",
                "calibration-timeout-restore",
                "restore",
            }:
                self._set_calibration_operation(
                    "Failed",
                    progress=None,
                    result=result,
                    restoration="Failed",
                    terminal=True,
                )
            self._finish_command(
                command.command_id, "TimedOut", result, fault_locked=fault
            )

    def _finish_adc_capture(self, now: float) -> None:
        if self._adc_ready_at is not None and now >= self._adc_ready_at:
            self.legacy_csv_data.set_value(self.csv_as_text("adc.csv"))
            self._adc_ready_at = None

    def stop(self, signum: int, frame: object) -> None:
        del signum, frame
        self._running = False

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.server.start()
        print(
            f"GIZMo OPC UA server listening at {ENDPOINT}; "
            f"canonical namespace {MODEL_NAMESPACE_URI}",
            flush=True,
        )
        notify_systemd(
            f"STATUS=OPC UA listening at {ENDPOINT}; assembling initial state"
        )
        last_watchdog = 0.0
        specs = self._job_specs()
        try:
            while self._running:
                now = time.monotonic()
                self._finish_jobs()
                self._schedule_jobs(now)
                self._finish_adc_capture(now)
                try:
                    self._forward_configuration_writes()
                except (
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                    zmq.ZMQError,
                ) as error:
                    print(f"OPC UA write forwarding failed: {error}", flush=True)
                    self._update(
                        "Configuration.LastCommandResult",
                        f"Command failed: {error}",
                        QUALITY_BAD,
                        utc_now(),
                    )
                    active = self._active_command
                    if active is not None and active.verification == "pending-write":
                        self._finish_command(
                            active.command_id,
                            "Failed",
                            f"Configuration write failed: {error}",
                        )

                self._advance_active_command(now)

                if not self._ready_sent and self._attempted >= set(specs):
                    overall = self._points["Health.Overall"][0].get_value()
                    notify_systemd(
                        "READY=1",
                        (
                            "STATUS=Canonical OPC UA namespace ready; "
                            f"{overall}"
                        ),
                    )
                    self._ready_sent = True
                    self._last_notified_health = str(overall)
                if now - last_watchdog >= 5:
                    notify_systemd("WATCHDOG=1")
                    last_watchdog = now
                time.sleep(0.05)
        finally:
            notify_systemd("STOPPING=1", "STATUS=Stopping GIZMo OPC UA server")
            self._executor.shutdown(wait=False, cancel_futures=True)
            self.server.stop()


def main() -> None:
    GizmoOpcUaServer().run()


if __name__ == "__main__":
    main()
