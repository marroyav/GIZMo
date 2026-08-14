# GIZMo OPC UA contract

## Authority, conformance, and transport

The Kria OPC UA implementation in this repository is the authoritative
GIZMo--Slow Controls contract. The supported public machine interface is the
OPC UA namespace:

```text
urn:fnal:gizmo
```

It is served at `opc.tcp://<gizmo-address>:4840`. With the default
configuration, UA service messages and `DataValue` instances use OPC UA Binary
over UA TCP. OPC UA supplies the wire encoding, type identifiers, browsing,
reads, writes, methods, subscriptions, status codes, and timestamps; no
Protobuf schema or telemetry ports are required.

Clients must resolve the namespace URI at connection time. They must not assume
that its runtime namespace index is always `3`.

The generated `schema/gizmo-opcua-contract.json` is the reviewable,
machine-readable form of that Kria model. It contains the required baseline
objects, variables, methods, NodeIds, datatypes, access levels, engineering
metadata, and a canonical SHA-256 digest. Regenerate it after an intentional
model change with:

```sh
python3 tools/generate-opcua-contract.py
```

The ZedBoard OPC UA server consumes and conforms to this contract at a separate
endpoint. It does not define a parallel model, connect to or proxy the Kria,
or start, stop, configure, or otherwise control the Kria implementation. Slow
Controls therefore configures two independent OPC UA connections and uses
device/application identity to distinguish the producers.

A platform limitation never changes a canonical NodeId, datatype, unit,
range, or meaning. A conforming producer keeps required nodes and reports an
appropriate non-good status where it cannot supply a value. Unsupported
methods return `BadNotSupported`. The ZedBoard currently accepts threshold
writes only from 0 through 1023 ohm and returns `BadOutOfRange` above that
implementation limit; the authoritative Kria engineering range remains
0 through 1,000,000 ohm.

The model version is available at the stable string NodeId:

```text
ns=<resolved>;s=GIZMo.Identity.ModelVersion
```

Version `1.x` changes preserve existing NodeIds, datatypes, and physical
meanings. Corrections may replace an implementation sentinel with a non-good
status rather than continue exposing it as a physical value. A breaking model
requires a new major-version namespace URI.

## Address-space organization

The canonical object is `Objects/GIZMo`, with the following subtrees:

| Object | Contents |
|---|---|
| `Identity` | model URI/version, runtime version, hostname, device and boot identity |
| `Measurement` | resistance, capacitance, threshold, stimulus, lock-in I/Q and phase |
| `Alarm` | active condition, persistent latch, reason, and latch time |
| `Thermal` | chassis and CPU temperatures |
| `Time` | UTC/local time, timezone, NTP, uptime, RTC, and clocksource |
| `OperatingSystem` | OS/kernel, CPU, load, memory, processes, entropy, file handles |
| `Network` | interfaces, addresses, routes, counters, and MAC provenance |
| `Storage` | capacity and health of relevant filesystems |
| `Firmware` | runtime, compute-platform identity, FPGA image, hashes, and expected devices |
| `Services` | state, PID, restarts, and result for every owned systemd unit |
| `Calibration` | configuration and metadata/digests for all calibration tables |
| `SDR` | stream state and the latest complete signed `Int32` frame |
| `Configuration` | writable threshold and measurements-per-calculation values |
| `Operations` | explicit latch, calibration, ADC, normalization, and clock methods |
| `Health` | aggregate and per-subsystem quality |

Canonical variables use deterministic string NodeIds. For example:

```text
GIZMo.Measurement.ResistanceOhm
GIZMo.Measurement.ResistanceRange
GIZMo.Alarm.LatchTime
GIZMo.Time.NtpSynchronized
GIZMo.Network.Interfaces.eth1.MacAddressSource
GIZMo.Firmware.OverlayState
GIZMo.Services.Units.gizmo_opcua_service.ActiveState
```

Interface, filesystem, and service object names are derived deterministically
from the Linux identity they represent.

## Value semantics

Every live variable is a normal OPC UA built-in datatype such as `Double`,
`Boolean`, `DateTime`, `UInt64`, `String`, or a one-dimensional typed array.
A client read or subscription receives a `DataValue` containing:

- the typed `Variant` value;
- an OPC UA `StatusCode`;
- the source timestamp.

Unknown values are not silently represented as plausible numeric zero.
Unavailable floating-point values normally carry a non-good status and `NaN`;
unavailable times carry a non-good status. The deliberate exception is the
valid `HIGH Z` range state described below. After a source disconnects, an existing sample
is retained with `UncertainLastUsableValue`. Before the first sample, values use
`BadWaitingForInitialData`.

Physical variables expose the standard OPC UA `EngineeringUnits` property.
Descriptions are AddressSpace attributes and can be browsed by generic clients.

`ResistanceOhm` is a physical value only when
`ResistanceRange = InRange`. A raw result above the validated 500-ohm
presentation range, or the recovered calculation's non-numeric sentinel,
produces `ResistanceRange = OutOfRange`; `ResistanceOhm` is `NaN` with
`Good` status. `HIGH Z` is a valid, good-quality measurement state. The
packaged client renders that `NaN` as JSON `null`, and the front panel displays
`HIGH Z`. No precise numeric resistance should be inferred in this state. The
dashboard represents it as an explicitly clipped
`>500 Ω` trace at the validated-range boundary; that display coordinate is not
written back into `ResistanceOhm`.

`Measurement.LegacyRecord` remains available for audit and compatibility. It
can contain implementation sentinels from the recovered engine, but it is not
a physical-data or parsing contract.

`Alarm.Active` is the authoritative composite Boolean emitted by ZMon at the
same branch that controls the relay and beacon. It includes the engine's
resistance and phase decisions; the OPC UA server, historian, and dashboard do
not reproduce those rules. `Alarm.Reason` is the reason emitted alongside
that decision. `Alarm.Latched` and `Alarm.LatchTime` remain the separate
persistent latch state maintained by the engine.

## Operator client

The package installs `gizmo-opcua-client`:

```sh
gizmo-opcua-client --endpoint opc.tcp://gizmo-device.example.invalid:4840 health
gizmo-opcua-client --endpoint opc.tcp://gizmo-device.example.invalid:4840 measurement
gizmo-opcua-client --endpoint opc.tcp://gizmo-device.example.invalid:4840 snapshot
gizmo-opcua-client --endpoint opc.tcp://gizmo-device.example.invalid:4840 schema
```

The snapshot command omits the large SDR frame unless
`--include-sdr-frame` is supplied.

Validated operations are also available:

```sh
gizmo-opcua-client set-threshold 200
gizmo-opcua-client set-averages 100
gizmo-opcua-client clear-latch
gizmo-opcua-client start-calibration 1000
gizmo-opcua-client capture-adc
gizmo-opcua-client normalize-magnitude
gizmo-opcua-client set-time 2026-07-27T09:15:00-06:00
```

Generic OPC UA clients can browse or subscribe without this utility.

The packaged [web dashboard](dashboard.md) is another read-only client. It
creates one subscription for live instrument data and fans its cached state
out to local browsers. The separate
[persistent historian](historian.md) creates one additional fixed
subscription; historical browser queries do not open OPC UA sessions.

`SetSystemTime` accepts an absolute OPC UA `DateTime`; the packaged client
requires an ISO-8601 UTC offset (or trailing `Z`) to avoid timezone ambiguity.
This is a controlled manual correction through the allow-listed privileged
helper and updates both the Linux wall clock and hardware RTC. It does not
claim NTP synchronization: clients must continue to inspect
`Time.NtpSynchronized`, `Time.RtcPresent`, and `Time.Quality`.

## Compatibility namespace

The recovered `SimpleOPCUAServer/CommandObject` namespace remains present
during migration with the same variables, method, datatypes, and write
behavior. Its comma-delimited `data` and `thermals` strings are deprecated.
New clients must use `urn:fnal:gizmo`.

## Cadence and SDR

Default update intervals are:

| Data | Interval |
|---|---:|
| measurement, thermal, time, latest SDR frame | 1 s |
| OS, network, systemd services | 10 s |
| storage, firmware, calibration | 30 s |

Clients should use subscriptions rather than repeatedly opening sessions.
The client/server `LatestFrame` node is intended for inspection and modest-rate
monitoring. If continuous high-rate waveform distribution is required, add an
OPC UA PubSub UADP mapping while retaining the same public information model.

## Security

The source-only public configuration sets `GIZMO_OPCUA_ALLOW_INSECURE=0`, so
the server fails closed until the controlled site workflow supplies a
certificate, private key, cryptography support, trust list, and operator-role
policy. Changing certificate paths alone is not a complete security boundary.

An explicitly approved, isolated bench test may set
`GIZMO_OPCUA_ALLOW_INSECURE=1` to enable anonymous `SecurityPolicy None`.
Serialization is not encryption; that mode must never be exposed beyond the
restricted bench network.
