"""Typed, transport-independent state model for the GIZMo runtime.

The maintained OPC UA server is the public machine interface.  This module
keeps Linux and legacy-record parsing independent from the OPC UA library so
the semantics can be tested without a running server or target hardware.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from gizmo_common import read_exported_int, state_path

MODEL_NAMESPACE_URI = "urn:fnal:gizmo"
LEGACY_NAMESPACE_URI = "SimpleOPCUAServer"
MODEL_VERSION = "1.4.0"
MODEL_PUBLICATION_DATE = dt.datetime(2026, 8, 17, tzinfo=dt.timezone.utc)
THRESHOLD_MIN_OHM = 0
THRESHOLD_MAX_OHM = 1_000_000

QUALITY_GOOD = "Good"
QUALITY_UNCERTAIN = "Uncertain"
QUALITY_BAD = "Bad"
QUALITY_NOT_AVAILABLE = "NotAvailable"

RANGE_UNKNOWN = "Unknown"
RANGE_IN_RANGE = "InRange"
RANGE_OUT_OF_RANGE = "OutOfRange"
RANGE_INVALID = "Invalid"

STIMULUS_FREQUENCY_HZ = float(os.environ.get("GIZMO_STIMULUS_FREQUENCY_HZ", "1436"))
RESISTANCE_OVER_RANGE_SENTINEL = float(
    os.environ.get("GIZMO_RESISTANCE_OVER_RANGE_SENTINEL", "1050")
)
RESISTANCE_SENTINEL_TOLERANCE = float(
    os.environ.get("GIZMO_RESISTANCE_SENTINEL_TOLERANCE", "0.05")
)
RESISTANCE_VALID_MAX_OHM = float(
    os.environ.get("GIZMO_RESISTANCE_VALID_MAX_OHM", "500")
)
HARDWARE_CONFIG = Path(
    os.environ.get("GIZMO_HARDWARE_CONFIG", "/etc/gizmo/hardware.env")
)
OVERLAY_NAME_DEFAULT = os.environ.get("GIZMO_OVERLAY_NAME", "GIZMo_Kria_3_7_25")
EXPECTED_NETWORK_INTERFACES = {
    item.strip()
    for item in os.environ.get("GIZMO_EXPECTED_NETWORK_INTERFACES", "eth0,eth1").split(
        ","
    )
    if item.strip()
}
VERSION_PATH = Path(os.environ.get("GIZMO_VERSION_FILE", "/usr/share/gizmo/VERSION"))
RUNTIME_DIR = Path(os.environ.get("GIZMO_RUNTIME_DIR", "/run/gizmo"))
XMUTIL_LISTAPPS_SNAPSHOT = RUNTIME_DIR / "xmutil-listapps.txt"
XMUTIL_BOARDID_SNAPSHOT = RUNTIME_DIR / "xmutil-boardid.txt"

SYSTEMD_UNITS = (
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
REQUIRED_UNITS = {
    "gizmo.target",
    "gizmo-network.service",
    "gizmo-hardware.service",
    "gizmo-control.socket",
    "gizmo-zmon.service",
    "gizmo-opcua.service",
    "gizmo-historian.service",
    "gizmo-dashboard.service",
}

_FIELD_PATTERN = re.compile(r"(?:^|,)\s*([A-Za-z][A-Za-z0-9_]*)=([^,]*)")
_MAC_PATTERN = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")
_SERVICE_PROPERTIES = (
    "Id",
    "Description",
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "MainPID",
    "NRestarts",
    "ExecMainStatus",
    "ActiveEnterTimestampUSec",
    "StatusText",
)


@dataclass
class MeasurementSnapshot:
    sequence: int
    sampled_at: dt.datetime
    raw_record: str
    quality: str = QUALITY_GOOD
    diagnostic: str = ""
    resistance_ohm: float | None = None
    resistance_range: str = RANGE_UNKNOWN
    capacitance_nf: float | None = None
    capacitance_range: str = RANGE_UNKNOWN
    threshold_ohm: float | None = None
    stimulus_frequency_hz: float = STIMULUS_FREQUENCY_HZ
    magnitude_count: float | None = None
    phase_atan_deg: float | None = None
    phase_atan2_deg: float | None = None
    phase_interpolated_deg: float | None = None
    in_phase_count: float | None = None
    quadrature_count: float | None = None
    averages_per_calculation: int = 0
    alarm_active: bool = False
    alarm_latched: bool = False
    latch_time: dt.datetime | None = None
    alarm_reason: str = ""


@dataclass
class ThermalSnapshot:
    sampled_at: dt.datetime
    sensors_celsius: dict[str, float | None]
    quality: str = QUALITY_GOOD
    diagnostic: str = ""
    raw_record: str = ""


@dataclass
class TimeSnapshot:
    observed_at: dt.datetime
    boot_time: dt.datetime
    uptime_seconds: float
    monotonic_ns: int
    timezone_name: str
    utc_offset_seconds: int
    ntp_synchronized: bool
    ntp_service_active: bool
    ntp_service: str
    rtc_present: bool
    rtc_device: str
    current_clocksource: str
    available_clocksources: list[str]
    quality: str = QUALITY_GOOD
    diagnostic: str = ""


@dataclass
class HostSnapshot:
    observed_at: dt.datetime
    hostname: str
    os_pretty_name: str
    os_version_id: str
    kernel_release: str
    kernel_version: str
    architecture: str
    logical_cpu_count: int
    cpu_utilization_percent: float | None
    load_1m: float | None
    load_5m: float | None
    load_15m: float | None
    memory_total_bytes: int
    memory_available_bytes: int
    memory_used_bytes: int
    swap_total_bytes: int
    swap_free_bytes: int
    process_count: int
    running_process_count: int
    entropy_available_bits: int | None
    open_file_handles: int | None
    quality: str = QUALITY_GOOD
    diagnostic: str = ""


@dataclass
class NetworkInterfaceSnapshot:
    name: str
    index: int
    mac_address: str
    permanent_mac_address: str
    mac_assignment_code: int
    mac_address_source: str
    administrative_up: bool
    carrier: bool
    operational_state: str
    mtu: int
    speed_mbps: int | None
    duplex: str
    driver: str
    addresses: list[str]
    rx_bytes: int
    rx_packets: int
    rx_errors: int
    rx_dropped: int
    tx_bytes: int
    tx_packets: int
    tx_errors: int
    tx_dropped: int
    collisions: int


@dataclass
class NetworkSnapshot:
    observed_at: dt.datetime
    interfaces: list[NetworkInterfaceSnapshot]
    expected_interfaces: list[str]
    missing_interfaces: list[str]
    routes: list[str]
    dns_servers: list[str]
    domain_name: str
    quality: str = QUALITY_GOOD
    diagnostic: str = ""


@dataclass
class FilesystemSnapshot:
    key: str
    mount_point: str
    source: str
    filesystem_type: str
    total_bytes: int
    used_bytes: int
    available_bytes: int
    used_percent: float
    read_only: bool
    quality: str


@dataclass
class StorageSnapshot:
    observed_at: dt.datetime
    filesystems: list[FilesystemSnapshot]
    quality: str = QUALITY_GOOD
    diagnostic: str = ""


@dataclass
class FirmwareSnapshot:
    observed_at: dt.datetime
    runtime_version: str
    overlay_name: str
    overlay_installed: bool
    overlay_loaded: bool | None
    overlay_state: str
    overlay_path: str
    overlay_bitstream_sha256: str
    device_tree_overlay: str
    device_tree_overlay_sha256: str
    shell_name: str
    expected_devices: list[str]
    missing_devices: list[str]
    board_model: str
    board_serial_number: str
    carrier_manufacturer: str
    carrier_product_name: str
    carrier_part_number: str
    carrier_serial_number: str
    carrier_revision: str
    factory_mac_addresses: list[str]
    quality: str = QUALITY_GOOD
    diagnostic: str = ""


@dataclass
class ServiceSnapshot:
    unit: str
    description: str
    load_state: str
    active_state: str
    sub_state: str
    result: str
    main_pid: int
    restart_count: int
    exit_status: int
    active_since: dt.datetime | None
    status_text: str
    required: bool
    quality: str


@dataclass
class ServiceInventorySnapshot:
    observed_at: dt.datetime
    services: list[ServiceSnapshot]
    quality: str = QUALITY_GOOD
    diagnostic: str = ""


@dataclass
class CalibrationTableSnapshot:
    key: str
    kind: str
    path: str
    state: str
    sha256: str
    modified_at: dt.datetime | None
    row_count: int
    input_min: float | None
    input_max: float | None
    input_unit: str
    format: str


@dataclass
class CalibrationSnapshot:
    observed_at: dt.datetime
    state: str
    configured_threshold_ohm: float
    measurements_per_calculation: int
    magnitude_normalization_pending: bool
    last_calibration_at: dt.datetime | None
    tables: list[CalibrationTableSnapshot] = field(default_factory=list)
    quality: str = QUALITY_GOOD
    diagnostic: str = ""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _read_text(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def _read_bytes(path: str | Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def _sha256(path: Path) -> str:
    data = _read_bytes(path)
    return hashlib.sha256(data).hexdigest() if data else ""


def _read_uint(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="ascii").strip())
        return value if value >= 0 else None
    except (OSError, ValueError):
        return None


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _finite_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _read_key_value_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        if separator:
            values[key.strip()] = value.strip().strip("\"'")
    return values


def _run(command: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _localize_timestamp(value: str) -> dt.datetime | None:
    if not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(dt.timezone.utc)


def parse_legacy_measurement(
    record: str,
    *,
    sequence: int,
    averages_per_calculation: int,
    sampled_at: dt.datetime | None = None,
    over_range_sentinel: float = RESISTANCE_OVER_RANGE_SENTINEL,
    sentinel_tolerance: float = RESISTANCE_SENTINEL_TOLERANCE,
    valid_max_ohm: float = RESISTANCE_VALID_MAX_OHM,
) -> MeasurementSnapshot:
    """Convert the legacy ZMon comma record into explicit physical fields."""
    raw = record.strip()
    if raw.startswith("Data from C-server:"):
        raw = raw.partition(":")[2].strip()
    fields = {
        match.group(1): match.group(2).strip() for match in _FIELD_PATTERN.finditer(raw)
    }
    result = MeasurementSnapshot(
        sequence=sequence,
        sampled_at=(sampled_at or utc_now()).astimezone(dt.timezone.utc),
        raw_record=raw,
        averages_per_calculation=max(0, averages_per_calculation),
    )
    definitions = {
        "Res": "resistance_ohm",
        "Cap": "capacitance_nf",
        "Th": "threshold_ohm",
        "Mag": "magnitude_count",
        "Phase": "phase_atan_deg",
        "Phase2": "phase_atan2_deg",
        "PhaseRX": "phase_interpolated_deg",
        "I": "in_phase_count",
        "Q": "quadrature_count",
    }
    missing: list[str] = []
    for legacy_name, attribute in definitions.items():
        parsed = _finite_float(fields.get(legacy_name))
        if parsed is None:
            missing.append(legacy_name)
        else:
            setattr(result, attribute, parsed)

    if result.resistance_ohm is None:
        result.resistance_range = RANGE_INVALID
    elif result.resistance_ohm < 0:
        result.resistance_ohm = None
        result.resistance_range = RANGE_INVALID
        result.quality = QUALITY_BAD
        result.diagnostic = "ZMon returned a negative resistance estimate"
    elif abs(result.resistance_ohm - over_range_sentinel) <= sentinel_tolerance:
        # The recovered ZMon program returns this magic number when its
        # reverse interpolation has no numeric result.  Do not leak that
        # implementation detail as a physical resistance or lower bound.
        result.resistance_ohm = None
        result.resistance_range = RANGE_OUT_OF_RANGE
        result.diagnostic = (
            "HIGH Z: ZMon reported its non-numeric out-of-range sentinel; "
            "ResistanceOhm has no finite value"
        )
    elif result.resistance_ohm > valid_max_ohm:
        result.resistance_ohm = None
        result.resistance_range = RANGE_OUT_OF_RANGE
        result.diagnostic = (
            f"HIGH Z: resistance exceeds the validated {valid_max_ohm:g} ohm "
            "presentation range; ResistanceOhm has no finite value"
        )
    else:
        result.resistance_range = RANGE_IN_RANGE

    if result.capacitance_nf is None:
        result.capacitance_range = RANGE_INVALID
    elif result.capacitance_nf < 0:
        result.capacitance_nf = None
        result.capacitance_range = RANGE_INVALID
        if result.quality == QUALITY_GOOD:
            result.quality = QUALITY_UNCERTAIN
        result.diagnostic = (
            f"{result.diagnostic}; " if result.diagnostic else ""
        ) + "ZMon returned a negative capacitance estimate"
    else:
        result.capacitance_range = RANGE_IN_RANGE

    active = fields.get("Alarm", "").strip().lower()
    if "Alarm" not in fields:
        missing.append("Alarm")
    elif active not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
        result.quality = QUALITY_BAD
        result.diagnostic = (
            f"{result.diagnostic}; " if result.diagnostic else ""
        ) + f"invalid Alarm Boolean: {fields['Alarm']}"
    result.alarm_active = active in {"1", "true", "yes", "on"}

    if "AlarmReason" not in fields:
        missing.append("AlarmReason")
    result.alarm_reason = fields.get("AlarmReason", "").strip()

    latched = fields.get("latched", "").strip().lower()
    if "latched" not in fields:
        missing.append("latched")
    result.alarm_latched = latched in {"1", "true", "yes", "on"}
    latch_text = fields.get("LatchStamp", "").strip()
    result.latch_time = _localize_timestamp(latch_text)
    if latch_text and result.latch_time is None:
        result.quality = (
            QUALITY_UNCERTAIN if result.quality == QUALITY_GOOD else result.quality
        )
        result.diagnostic = (
            f"{result.diagnostic}; " if result.diagnostic else ""
        ) + f"invalid latch timestamp: {latch_text}"

    if missing:
        result.quality = QUALITY_BAD
        detail = f"missing ZMon fields: {', '.join(missing)}"
        result.diagnostic = (
            f"{result.diagnostic}; {detail}" if result.diagnostic else detail
        )
    return result


def parse_legacy_thermals(
    record: str, *, sampled_at: dt.datetime | None = None
) -> ThermalSnapshot:
    raw = record.strip()
    fields = {
        match.group(1): match.group(2).strip() for match in _FIELD_PATTERN.finditer(raw)
    }
    names = ("Chassis", "CPU1", "CPU2", "CPU3")
    sensors = {name: _finite_float(fields.get(name)) for name in names}
    available = sum(value is not None for value in sensors.values())
    quality = (
        QUALITY_GOOD
        if available == len(names)
        else QUALITY_UNCERTAIN
        if available
        else QUALITY_BAD
    )
    diagnostic = "" if quality == QUALITY_GOOD else "temperature record is incomplete"
    return ThermalSnapshot(
        sampled_at=(sampled_at or utc_now()).astimezone(dt.timezone.utc),
        sensors_celsius=sensors,
        quality=quality,
        diagnostic=diagnostic,
        raw_record=raw,
    )


def runtime_version() -> str:
    configured = os.environ.get("GIZMO_RUNTIME_VERSION", "").strip()
    if configured:
        return configured
    value = _read_text(VERSION_PATH)
    if value:
        return value
    source_version = Path(__file__).resolve().parents[2] / "VERSION"
    return _read_text(source_version, "unknown")


class SystemCollectors:
    """Collect lower-rate Linux, network, firmware, and package status."""

    def __init__(self) -> None:
        self._previous_cpu: tuple[int, int] | None = None
        self._factory_macs: list[str] | None = None

    def collect_time(self) -> TimeSnapshot:
        local_now = dt.datetime.now().astimezone()
        observed = local_now.astimezone(dt.timezone.utc)
        try:
            uptime_seconds = float(_read_text("/proc/uptime", "0").split()[0])
        except (ValueError, IndexError):
            uptime_seconds = 0.0
        boot_time = dt.datetime.fromtimestamp(
            time.time() - uptime_seconds, tz=dt.timezone.utc
        )
        clocksource_root = Path("/sys/devices/system/clocksource/clocksource0")
        rtc_devices = sorted(Path("/sys/class/rtc").glob("rtc*"))
        result = TimeSnapshot(
            observed_at=observed,
            boot_time=boot_time,
            uptime_seconds=uptime_seconds,
            monotonic_ns=time.monotonic_ns(),
            timezone_name=str(local_now.tzinfo or ""),
            utc_offset_seconds=int(
                (local_now.utcoffset() or dt.timedelta()).total_seconds()
            ),
            ntp_synchronized=False,
            ntp_service_active=False,
            ntp_service="",
            rtc_present=bool(rtc_devices),
            rtc_device=str(rtc_devices[0]) if rtc_devices else "",
            current_clocksource=_read_text(clocksource_root / "current_clocksource"),
            available_clocksources=_read_text(
                clocksource_root / "available_clocksource"
            ).split(),
        )
        if shutil.which("timedatectl"):
            try:
                completed = _run(
                    [
                        "timedatectl",
                        "show",
                        "--property=Timezone",
                        "--property=NTPSynchronized",
                        "--property=NTP",
                    ],
                    timeout=2,
                )
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr.strip() or "timedatectl failed")
                properties = {}
                for line in completed.stdout.splitlines():
                    key, separator, value = line.partition("=")
                    if separator:
                        properties[key] = value
                result.timezone_name = properties.get("Timezone", result.timezone_name)
                result.ntp_synchronized = (
                    properties.get("NTPSynchronized", "").lower() == "yes"
                )
                result.ntp_service_active = properties.get("NTP", "").lower() == "yes"
                result.ntp_service = "systemd timedate control"
            except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
                result.quality = QUALITY_UNCERTAIN
                result.diagnostic = f"NTP status unavailable: {error}"
        else:
            result.quality = QUALITY_UNCERTAIN
            result.diagnostic = "timedatectl is unavailable"

        if local_now.year < 2024:
            result.quality = QUALITY_BAD
            result.diagnostic = f"system clock is invalid: {local_now.isoformat()}"
        elif not result.ntp_synchronized and result.quality == QUALITY_GOOD:
            result.quality = QUALITY_UNCERTAIN
            result.diagnostic = "system clock is not confirmed synchronized"
        return result

    def _cpu_status(self) -> tuple[float | None, tuple[float | None, ...]]:
        utilization: float | None = None
        try:
            lines = Path("/proc/stat").read_text(encoding="ascii").splitlines()
            values = [int(value) for value in lines[0].split()[1:]]
            total = sum(values)
            idle = sum(values[index] for index in (3, 4) if index < len(values))
            if self._previous_cpu is not None:
                previous_total, previous_idle = self._previous_cpu
                delta_total = total - previous_total
                delta_idle = idle - previous_idle
                if delta_total > 0:
                    utilization = max(
                        0.0,
                        min(100.0, 100.0 * (delta_total - delta_idle) / delta_total),
                    )
            self._previous_cpu = (total, idle)
        except (OSError, ValueError, IndexError):
            pass
        try:
            load: tuple[float | None, ...] = tuple(os.getloadavg())
        except OSError:
            load = (None, None, None)
        return utilization, load

    @staticmethod
    def _memory_status() -> dict[str, int]:
        values: dict[str, int] = {}
        try:
            lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
        except OSError:
            return values
        for line in lines:
            key, separator, remainder = line.partition(":")
            fields = remainder.strip().split()
            if separator and fields:
                values[key] = _safe_int(fields[0]) * 1024
        return values

    def collect_host(self) -> HostSnapshot:
        observed = utc_now()
        os_release = _read_key_value_file(Path("/etc/os-release"))
        utilization, load = self._cpu_status()
        memory = self._memory_status()
        total = memory.get("MemTotal", 0)
        available = memory.get("MemAvailable", memory.get("MemFree", 0))
        try:
            process_dirs = [
                item
                for item in Path("/proc").iterdir()
                if item.name.isdigit() and item.is_dir()
            ]
        except OSError:
            process_dirs = []
        running = 0
        try:
            running = _safe_int(
                next(
                    line.split()[1]
                    for line in Path("/proc/stat")
                    .read_text(encoding="ascii")
                    .splitlines()
                    if line.startswith("procs_running ")
                )
            )
        except (OSError, StopIteration, IndexError):
            pass
        entropy = _read_uint(Path("/proc/sys/kernel/random/entropy_avail"))
        try:
            open_files = _safe_int(_read_text("/proc/sys/fs/file-nr").split()[0])
        except IndexError:
            open_files = None
        quality = QUALITY_GOOD
        diagnostic = ""
        if total == 0:
            quality = QUALITY_UNCERTAIN
            diagnostic = "host memory counters are unavailable"
        elif available / total < 0.05:
            quality = QUALITY_UNCERTAIN
            diagnostic = "available host memory is below five percent"
        return HostSnapshot(
            observed_at=observed,
            hostname=socket.gethostname(),
            os_pretty_name=os_release.get("PRETTY_NAME", platform.system()),
            os_version_id=os_release.get("VERSION_ID", ""),
            kernel_release=platform.release(),
            kernel_version=platform.version(),
            architecture=platform.machine(),
            logical_cpu_count=os.cpu_count() or 0,
            cpu_utilization_percent=utilization,
            load_1m=load[0],
            load_5m=load[1],
            load_15m=load[2],
            memory_total_bytes=total,
            memory_available_bytes=available,
            memory_used_bytes=max(0, total - available),
            swap_total_bytes=memory.get("SwapTotal", 0),
            swap_free_bytes=memory.get("SwapFree", 0),
            process_count=len(process_dirs),
            running_process_count=running,
            entropy_available_bits=entropy,
            open_file_handles=open_files,
            quality=quality,
            diagnostic=diagnostic,
        )

    @staticmethod
    def _ip_inventory() -> tuple[dict[str, dict], list[dict], str]:
        if not shutil.which("ip"):
            return {}, [], "ip command is unavailable"
        try:
            addresses_result = _run(["ip", "-j", "address", "show"], timeout=3)
            routes_result = _run(
                ["ip", "-j", "route", "show", "table", "all"], timeout=3
            )
            if addresses_result.returncode != 0:
                return {}, [], addresses_result.stderr.strip()
            addresses = {
                item.get("ifname", ""): item
                for item in json.loads(addresses_result.stdout)
            }
            routes = (
                json.loads(routes_result.stdout)
                if routes_result.returncode == 0
                else []
            )
            return addresses, routes, routes_result.stderr.strip()
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            return {}, [], str(error)

    def _read_factory_macs(self) -> list[str]:
        if self._factory_macs is not None:
            return self._factory_macs
        macs: list[str] = []
        snapshot = _read_text(XMUTIL_BOARDID_SNAPSHOT)
        if snapshot:
            _, macs = self._parse_boardid(snapshot)
        if shutil.which("xmutil"):
            if not macs:
                try:
                    completed = _run(["xmutil", "boardid"], timeout=5)
                    _, macs = self._parse_boardid(completed.stdout)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        self._factory_macs = macs
        return macs

    @staticmethod
    def _permanent_mac(interface_path: Path, inventory: dict) -> str:
        permanent = str(inventory.get("permaddr", "")).lower()
        if permanent:
            return permanent
        try:
            completed = _run(["ethtool", "-P", interface_path.name], timeout=2)
            if completed.returncode == 0 and ":" in completed.stdout:
                candidate = completed.stdout.rpartition(": ")[2].strip().lower()
                if _MAC_PATTERN.fullmatch(candidate):
                    return candidate
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ""

    @staticmethod
    def _mac_source(
        current: str,
        permanent: str,
        assignment_code: int,
        factory_macs: list[str],
    ) -> str:
        normalized = current.lower()
        if normalized and normalized in factory_macs:
            return "FRU EEPROM (verified)"
        if assignment_code == 0:
            return "Permanent hardware address"
        if assignment_code == 1:
            return "Random"
        if assignment_code == 2:
            return "Stolen from another interface"
        if assignment_code == 3:
            if permanent and normalized == permanent:
                return "Software set to permanent address"
            return "Software set"
        return "Unknown"

    def collect_network(self) -> NetworkSnapshot:
        observed = utc_now()
        address_inventory, route_inventory, error = self._ip_inventory()
        factory_macs = self._read_factory_macs()
        sys_root = Path("/sys/class/net")
        try:
            paths = sorted(sys_root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            paths = []
            error = str(exc)
        interfaces: list[NetworkInterfaceSnapshot] = []
        for path in paths:
            name = path.name
            inventory = address_inventory.get(name, {})
            current_mac = _read_text(path / "address").lower()
            permanent_mac = self._permanent_mac(path, inventory)
            assignment_code = _read_uint(path / "addr_assign_type")
            assignment_code = assignment_code if assignment_code is not None else -1
            addresses = []
            for address in inventory.get("addr_info", []):
                local = address.get("local", "")
                prefix = int(address.get("prefixlen", 0))
                family = address.get("family", "")
                scope = address.get("scope", "")
                flags = ",".join(sorted(address.get("flags", [])))
                suffix = f" [{family}; scope={scope}"
                if flags:
                    suffix += f"; flags={flags}"
                addresses.append(f"{local}/{prefix}{suffix}]")
            driver = ""
            try:
                driver = (path / "device" / "driver").resolve().name
            except OSError:
                pass
            counters = {
                name: _read_uint(path / "statistics" / name) or 0
                for name in (
                    "rx_bytes",
                    "rx_packets",
                    "rx_errors",
                    "rx_dropped",
                    "tx_bytes",
                    "tx_packets",
                    "tx_errors",
                    "tx_dropped",
                    "collisions",
                )
            }
            speed = _safe_int(_read_text(path / "speed"), -1)
            interfaces.append(
                NetworkInterfaceSnapshot(
                    name=name,
                    index=_safe_int(_read_text(path / "ifindex")),
                    mac_address=current_mac,
                    permanent_mac_address=permanent_mac,
                    mac_assignment_code=assignment_code,
                    mac_address_source=self._mac_source(
                        current_mac, permanent_mac, assignment_code, factory_macs
                    ),
                    administrative_up="UP" in inventory.get("flags", []),
                    carrier=_read_text(path / "carrier") == "1",
                    operational_state=_read_text(path / "operstate"),
                    mtu=_safe_int(_read_text(path / "mtu")),
                    speed_mbps=speed if speed >= 0 else None,
                    duplex=_read_text(path / "duplex"),
                    driver=driver,
                    addresses=addresses,
                    **counters,
                )
            )
        routes = []
        for item in route_inventory:
            fields = [
                item.get("dst", "default"),
                f"via {item['gateway']}" if item.get("gateway") else "",
                f"dev {item['dev']}" if item.get("dev") else "",
                f"metric {item['metric']}" if "metric" in item else "",
                f"proto {item['protocol']}" if item.get("protocol") else "",
            ]
            routes.append(" ".join(field for field in fields if field))
        dns_servers: list[str] = []
        domain_name = ""
        for line in _read_text("/etc/resolv.conf").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                dns_servers.append(fields[1])
            elif len(fields) >= 2 and fields[0] in {"domain", "search"}:
                domain_name = " ".join(fields[1:])
        observed_names = {interface.name for interface in interfaces}
        missing_interfaces = sorted(EXPECTED_NETWORK_INTERFACES - observed_names)
        diagnostics = [error] if error else []
        if missing_interfaces:
            diagnostics.append(
                f"expected interfaces missing: {', '.join(missing_interfaces)}"
            )
        return NetworkSnapshot(
            observed_at=observed,
            interfaces=interfaces,
            expected_interfaces=sorted(EXPECTED_NETWORK_INTERFACES),
            missing_interfaces=missing_interfaces,
            routes=routes,
            dns_servers=dns_servers,
            domain_name=domain_name,
            quality=(QUALITY_UNCERTAIN if diagnostics else QUALITY_GOOD),
            diagnostic="; ".join(diagnostics),
        )

    @staticmethod
    def _mount_inventory() -> list[tuple[str, str, str, set[str]]]:
        mounts: list[tuple[str, str, str, set[str]]] = []
        try:
            lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
        except OSError:
            return mounts
        for line in lines:
            fields = line.split()
            if len(fields) >= 4:
                mounts.append(
                    (fields[0], fields[1], fields[2], set(fields[3].split(",")))
                )
        return mounts

    def collect_storage(self) -> StorageSnapshot:
        observed = utc_now()
        mounts = self._mount_inventory()
        requested = (
            ("Root", Path("/")),
            ("State", state_path(".")),
            ("Run", Path("/run/gizmo")),
        )
        filesystems: list[FilesystemSnapshot] = []
        seen: set[str] = set()
        diagnostics: list[str] = []
        for key, requested_path in requested:
            existing = requested_path
            while not existing.exists() and existing != existing.parent:
                existing = existing.parent
            resolved = str(existing.resolve())
            candidates = [
                mount
                for mount in mounts
                if resolved == mount[1]
                or mount[1] == "/"
                or resolved.startswith(mount[1].rstrip("/") + "/")
            ]
            if not candidates:
                continue
            source, mount_point, fs_type, options = max(
                candidates, key=lambda mount: len(mount[1])
            )
            if mount_point in seen:
                continue
            seen.add(mount_point)
            try:
                stat = os.statvfs(mount_point)
            except OSError as error:
                diagnostics.append(f"{mount_point}: {error}")
                continue
            total = stat.f_blocks * stat.f_frsize
            available = stat.f_bavail * stat.f_frsize
            used = max(0, total - stat.f_bfree * stat.f_frsize)
            percent = 100.0 * used / total if total else 0.0
            quality = (
                QUALITY_BAD
                if percent >= 95
                else QUALITY_UNCERTAIN
                if percent >= 85
                else QUALITY_GOOD
            )
            filesystems.append(
                FilesystemSnapshot(
                    key=key,
                    mount_point=mount_point,
                    source=source,
                    filesystem_type=fs_type,
                    total_bytes=total,
                    used_bytes=used,
                    available_bytes=available,
                    used_percent=percent,
                    read_only="ro" in options,
                    quality=quality,
                )
            )
        quality = QUALITY_GOOD
        if not filesystems:
            quality = QUALITY_BAD
        elif diagnostics or any(item.quality != QUALITY_GOOD for item in filesystems):
            quality = QUALITY_UNCERTAIN
        return StorageSnapshot(
            observed_at=observed,
            filesystems=filesystems,
            quality=quality,
            diagnostic="; ".join(diagnostics),
        )

    @staticmethod
    def _parse_boardid(output: str) -> tuple[dict[str, str], list[str]]:
        values: dict[str, str] = {}
        macs: list[str] = []
        for line in output.splitlines():
            key, separator, value = line.partition(":")
            if not separator:
                continue
            normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            stripped = value.strip()
            match = _MAC_PATTERN.fullmatch(stripped)
            if match and "mac" in normalized:
                mac = match.group(0).lower()
                if mac not in macs:
                    macs.append(mac)
                continue
            values[normalized] = stripped
        aliases = {
            "manufacturer": ("fru_board_manufacturer",),
            "product_name": ("fru_board_product_name",),
            "serial_number": ("fru_board_serial_number",),
            "part_number": ("fru_board_part_number",),
            "revision": ("fru_board_revision",),
        }
        for alias, candidates in aliases.items():
            if alias in values:
                continue
            for candidate in candidates:
                if candidate in values:
                    values[alias] = values[candidate]
                    break
        return values, macs

    @staticmethod
    def _overlay_is_active(output: str, overlay_name: str) -> bool | None:
        matching_rows = [
            line for line in output.splitlines() if overlay_name in line.split()
        ]
        if not matching_rows:
            return False if output.strip() else None
        for line in matching_rows:
            try:
                active_slot = int(line.split()[-1].rstrip(","))
            except (IndexError, ValueError):
                continue
            if active_slot >= 0:
                return True
        return False

    def collect_firmware(self) -> FirmwareSnapshot:
        observed = utc_now()
        hardware = _read_key_value_file(HARDWARE_CONFIG)
        overlay_name = hardware.get("GIZMO_OVERLAY_NAME", OVERLAY_NAME_DEFAULT)
        overlay_root = Path("/lib/firmware/xilinx") / overlay_name
        expected_devices = [
            hardware.get("GIZMO_READY_DEVICE", "/dev/i2c-6"),
            hardware.get("GIZMO_I2C_DEVICE", "/dev/i2c-7"),
        ]
        missing = []
        for name in expected_devices:
            path = Path(name)
            visible_path = (
                Path("/sys/class/i2c-dev") / path.name
                if path.parent == Path("/dev") and path.name.startswith("i2c-")
                else path
            )
            if not visible_path.exists():
                missing.append(name)
        overlay_installed = overlay_root.exists()
        overlay_loaded: bool | None = None
        diagnostics: list[str] = []
        listapps_snapshot = _read_text(XMUTIL_LISTAPPS_SNAPSHOT)
        if listapps_snapshot:
            overlay_loaded = self._overlay_is_active(
                listapps_snapshot, overlay_name
            )
        elif shutil.which("xmutil"):
            try:
                completed = _run(["xmutil", "listapps"], timeout=5)
                if completed.returncode == 0:
                    overlay_loaded = self._overlay_is_active(
                        completed.stdout, overlay_name
                    )
                else:
                    diagnostics.append(
                        completed.stderr.strip() or completed.stdout.strip()
                    )
            except (OSError, subprocess.TimeoutExpired) as error:
                diagnostics.append(str(error))
        shell_name = ""
        try:
            shell_name = str(
                json.loads(
                    (overlay_root / "shell.json").read_text(encoding="utf-8")
                ).get("shell_type", "")
            )
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        bitstreams = sorted(overlay_root.glob("*.bin"))
        dtbos = sorted(overlay_root.glob("*.dtbo"))
        board_values: dict[str, str] = {}
        factory_macs = self._read_factory_macs()
        boardid_snapshot = _read_text(XMUTIL_BOARDID_SNAPSHOT)
        if boardid_snapshot:
            board_values, board_macs = self._parse_boardid(boardid_snapshot)
            if "checksum invalid" in boardid_snapshot.lower():
                diagnostics.append(
                    "FRU inventory reports an invalid multirecord checksum"
                )
            if board_macs:
                factory_macs = board_macs
                self._factory_macs = board_macs
        elif shutil.which("xmutil"):
            try:
                completed = _run(["xmutil", "boardid"], timeout=5)
                board_values, board_macs = self._parse_boardid(completed.stdout)
                if board_macs:
                    factory_macs = board_macs
                    self._factory_macs = board_macs
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not overlay_installed:
            state = "Missing"
            quality = QUALITY_BAD
            diagnostics.append(f"overlay is not installed at {overlay_root}")
        elif overlay_loaded is False or missing:
            state = "Degraded"
            quality = QUALITY_UNCERTAIN
            if overlay_loaded is False:
                diagnostics.append("overlay manager reports the overlay is not loaded")
            if missing:
                diagnostics.append(f"missing devices: {', '.join(missing)}")
        else:
            state = "Running" if overlay_loaded else "Installed; load state unconfirmed"
            quality = QUALITY_GOOD if overlay_loaded else QUALITY_UNCERTAIN
        return FirmwareSnapshot(
            observed_at=observed,
            runtime_version=runtime_version(),
            overlay_name=overlay_name,
            overlay_installed=overlay_installed,
            overlay_loaded=overlay_loaded,
            overlay_state=state,
            overlay_path=str(overlay_root),
            overlay_bitstream_sha256=_sha256(bitstreams[0]) if bitstreams else "",
            device_tree_overlay=str(dtbos[0]) if dtbos else "",
            device_tree_overlay_sha256=_sha256(dtbos[0]) if dtbos else "",
            shell_name=shell_name,
            expected_devices=expected_devices,
            missing_devices=missing,
            board_model=_read_text("/sys/firmware/devicetree/base/model").strip("\0"),
            board_serial_number=_read_text(
                "/sys/firmware/devicetree/base/serial-number"
            ).strip("\0"),
            carrier_manufacturer=board_values.get("manufacturer", ""),
            carrier_product_name=board_values.get("product_name", ""),
            carrier_part_number=board_values.get("part_number", ""),
            carrier_serial_number=board_values.get("serial_number", ""),
            carrier_revision=board_values.get("revision", ""),
            factory_mac_addresses=factory_macs,
            quality=quality,
            diagnostic="; ".join(item for item in diagnostics if item),
        )

    @staticmethod
    def _parse_systemd_records(output: str) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in output.splitlines() + [""]:
            if not line.strip():
                if current:
                    records.append(current)
                    current = {}
                continue
            key, separator, value = line.partition("=")
            if separator:
                current[key] = value
        return records

    def collect_services(self) -> ServiceInventorySnapshot:
        observed = utc_now()
        if not shutil.which("systemctl"):
            return ServiceInventorySnapshot(
                observed_at=observed,
                services=[],
                quality=QUALITY_NOT_AVAILABLE,
                diagnostic="systemctl is unavailable",
            )
        try:
            completed = _run(
                [
                    "systemctl",
                    "show",
                    *SYSTEMD_UNITS,
                    f"--property={','.join(_SERVICE_PROPERTIES)}",
                    "--no-pager",
                ],
                timeout=5,
            )
            records = self._parse_systemd_records(completed.stdout)
            error = completed.stderr.strip() if completed.returncode else ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            records = []
            error = str(exc)
        services: list[ServiceSnapshot] = []
        observed_units: set[str] = set()
        for record in records:
            unit = record.get("Id", "")
            if not unit:
                continue
            observed_units.add(unit)
            active_state = record.get("ActiveState", "")
            required = unit in REQUIRED_UNITS
            quality = (
                QUALITY_GOOD
                if active_state == "active"
                else QUALITY_UNCERTAIN
                if active_state == "activating"
                else QUALITY_BAD
                if required
                else QUALITY_UNCERTAIN
                if active_state in {"deactivating", "inactive"}
                else QUALITY_BAD
            )
            active_since_us = _safe_int(record.get("ActiveEnterTimestampUSec"))
            active_since = (
                dt.datetime.fromtimestamp(
                    active_since_us / 1_000_000, tz=dt.timezone.utc
                )
                if active_since_us > 0
                else None
            )
            services.append(
                ServiceSnapshot(
                    unit=unit,
                    description=record.get("Description", ""),
                    load_state=record.get("LoadState", ""),
                    active_state=active_state,
                    sub_state=record.get("SubState", ""),
                    result=record.get("Result", ""),
                    main_pid=max(0, _safe_int(record.get("MainPID"))),
                    restart_count=max(0, _safe_int(record.get("NRestarts"))),
                    exit_status=_safe_int(record.get("ExecMainStatus")),
                    active_since=active_since,
                    status_text=record.get("StatusText", ""),
                    required=required,
                    quality=quality,
                )
            )
        missing = REQUIRED_UNITS - observed_units
        quality = QUALITY_GOOD
        if missing or error:
            quality = QUALITY_UNCERTAIN
        if any(
            service.required and service.quality == QUALITY_BAD for service in services
        ):
            quality = QUALITY_BAD
        elif any(
            service.required and service.quality != QUALITY_GOOD for service in services
        ) or any(service.quality == QUALITY_BAD for service in services):
            quality = QUALITY_UNCERTAIN
        diagnostics = [error] if error else []
        if missing:
            diagnostics.append(
                f"required units not returned: {', '.join(sorted(missing))}"
            )
        return ServiceInventorySnapshot(
            observed_at=observed,
            services=services,
            quality=quality,
            diagnostic="; ".join(diagnostics),
        )

    @staticmethod
    def _calibration_definitions() -> tuple[tuple[str, str, str], ...]:
        return (
            ("Rcalibration.csv", "Resistance", "Ohm"),
            ("Rcalibration_ph.csv", "ResistancePhase", "Ohm"),
            ("Ccalibration.csv", "Capacitance", "nF"),
            ("Ccalibration_ph.csv", "CapacitancePhase", "nF"),
        )

    def collect_calibration(self) -> CalibrationSnapshot:
        observed = utc_now()
        tables: list[CalibrationTableSnapshot] = []
        newest_mtime = 0.0
        diagnostics: list[str] = []
        for name, kind, input_unit in self._calibration_definitions():
            path = state_path(name)
            try:
                data = path.read_bytes()
                stat = path.stat()
                numeric_inputs: list[float] = []
                with path.open(
                    newline="", encoding="utf-8", errors="replace"
                ) as stream:
                    for row in csv.reader(stream):
                        if not row:
                            continue
                        try:
                            numeric_inputs.append(float(row[0].strip()))
                        except (ValueError, IndexError):
                            continue
                state = "Valid" if numeric_inputs else "Invalid"
                modified = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc)
                newest_mtime = max(newest_mtime, stat.st_mtime)
                tables.append(
                    CalibrationTableSnapshot(
                        key=kind,
                        kind=kind,
                        path=str(path),
                        state=state,
                        sha256=hashlib.sha256(data).hexdigest(),
                        modified_at=modified,
                        row_count=len(numeric_inputs),
                        input_min=min(numeric_inputs) if numeric_inputs else None,
                        input_max=max(numeric_inputs) if numeric_inputs else None,
                        input_unit=input_unit,
                        format="CSV: input,magnitude,phase_atan,phase_atan2",
                    )
                )
            except OSError as error:
                diagnostics.append(f"{name}: {error}")
                tables.append(
                    CalibrationTableSnapshot(
                        key=kind,
                        kind=kind,
                        path=str(path),
                        state="Missing",
                        sha256="",
                        modified_at=None,
                        row_count=0,
                        input_min=None,
                        input_max=None,
                        input_unit=input_unit,
                        format="CSV: input,magnitude,phase_atan,phase_atan2",
                    )
                )
        valid = all(table.state == "Valid" for table in tables)
        state = "Valid" if valid else "Invalid"
        return CalibrationSnapshot(
            observed_at=observed,
            state=state,
            configured_threshold_ohm=float(
                read_exported_int("setThreshold.env", "threshold", 100)
            ),
            measurements_per_calculation=max(
                0, read_exported_int("setRunInterval.env", "runInterval", 100)
            ),
            magnitude_normalization_pending=bool(
                read_exported_int("normalizeMagFlag.env", "normalizeMagFlag", 0)
            ),
            last_calibration_at=(
                dt.datetime.fromtimestamp(newest_mtime, tz=dt.timezone.utc)
                if newest_mtime
                else None
            ),
            tables=tables,
            quality=QUALITY_GOOD if valid else QUALITY_BAD,
            diagnostic="; ".join(diagnostics),
        )
