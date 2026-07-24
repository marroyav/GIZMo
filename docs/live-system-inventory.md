# Recovered live-system inventory

Captured from the working Kria image in July 2026. The board's real-time clock
was stale during inspection, so timestamps reported by the target are not
authoritative.

## Platform

- Ubuntu 22.04.4 LTS, ARM64
- kernel `5.15.0-1027-xilinx-zynqmp`
- overlay manager: `xmutil`
- overlay application: `GIZMo_Kria_3_7_25`
- actual overlay directory:
  `/lib/firmware/xilinx/GIZMo_Kria_3_7_25` (lowercase `xilinx`)

The supplied handoff mentioned `/lib/firmware/Xilinx`; that spelling does not
match the live filesystem.

## Legacy startup

`rc.startup.service` runs `/etc/rc.startup` as root. It:

1. waits for `/dev/i2c-6`;
2. unloads and loads the overlay twice;
3. assigns `<redacted-private-ip>/24` to `eth0`;
4. assigns `<redacted-private-ip>/24` to `eth1`;
5. grants world access to `/dev/i2c-7`;
6. creates `/dev/shm/rc.startup.done`.

The generated `rc-local.service` runs `/etc/rc.local`. All application
processes remain in its cgroup. It starts:

- `/home/ubuntu/Software/ZMon.12.16`
- `/home/ubuntu/Software/EVE-main_Kria/EVE-main_Kria`
- `zmqServer.py`
- `Temperature.MCP9808.py`
- `SDR2.py`
- `OPC-UA-Server-Bridge.py`

The exact custom unit and both scripts are retained under `legacy/live-root`.

## Interfaces and listeners

Only `eth0` and `eth1`, the two PS ports, are functional in this release.
Observed listeners:

| Port | Protocol/component |
|---:|---|
| 4840 | OPC-UA |
| 5005 | temperature TCP |
| 5055 | ZMon TCP |
| 5555 | ZeroMQ REP |
| 5556 | SDR TCP |

## Hardware addresses

The recovered software directly maps:

- DAC GPIO: `0xA0000000`
- relay GPIO: `0xA0044000`
- relay controller: `0xA0060000`
- DAC BRAM: `0xA0040000`
- ADC BRAM: `0xA0042000`

These constants cannot be regenerated or verified against a Vivado address map
because HDL and project sources were not supplied.

## Recovered material

The audit snapshot includes current C/Python sources, display sources and the
previously missing Raspberry Pi platform port, configuration, calibration
tables, startup files, and compiled overlay assets. The maintained calibration
defaults match the live board by SHA-256.

Excluded from the maintained package:

- historical ZMon executables and object files;
- large transient `/dev/shm` logs;
- editor backups and unrelated experiments;
- HDL/Vivado sources, which were not present.

The original ZMon source produced multiple out-of-bounds and undefined-behavior
compiler warnings. The maintained copy fixes the mechanically clear array,
format, mmap-check, and boundary defects. Measurement-algorithm changes still
require instrument-level validation.
