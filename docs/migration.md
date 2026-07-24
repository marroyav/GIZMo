# Migration from the legacy image

This procedure is intentionally manual for the first instrument. Installing
the `.deb` does not start or enable the runtime.

## 1. Record and back up

While the legacy stack is still running:

```sh
systemctl status rc.startup.service rc-local.service
sudo tar -C / -czf /home/ubuntu/gizmo-legacy-backup.tgz \
  etc/rc.startup etc/rc.local etc/systemd/system/rc.startup.service \
  home/ubuntu/Software lib/firmware/xilinx/GIZMo_Kria_3_7_25
```

Copy the backup off the instrument.

## 2. Install without activating

```sh
sudo dpkg -i gizmo-runtime_0.1.0_arm64.deb
sudo gizmo-doctor
```

Resolve every failed dependency/device check before proceeding.

The package seeds `/var/lib/gizmo` only when a file is absent. If calibration
has changed since this repository was captured, copy the live files over while
the legacy stack is stopped:

```sh
sudo install -o root -g gizmo -m 0664 \
  /home/ubuntu/Software/Rcalibration.csv \
  /home/ubuntu/Software/Rcalibration_ph.csv \
  /home/ubuntu/Software/Ccalibration.csv \
  /home/ubuntu/Software/Ccalibration_ph.csv \
  /var/lib/gizmo/
```

## 3. Transfer lifecycle ownership

Do not overlap the supervisors.

```sh
sudo systemctl stop rc-local.service
sudo systemctl disable --now rc.startup.service
sudo systemctl mask rc-local.service rc.startup.service
```

Confirm the legacy processes and ports are gone:

```sh
pgrep -af 'ZMon|EVE-main|zmqServer|Temperature.MCP9808|SDR2|OPC-UA'
sudo ss -lntp | grep -E ':(4840|5005|5055|5555|5556)\b' || true
```

Now activate the package:

```sh
sudo systemctl enable --now gizmo.target
sudo gizmo-doctor
systemctl --no-pager --full status gizmo.target
```

## 4. Acceptance checks

At minimum:

1. both expected static addresses are present;
2. `xmutil listapps` shows `GIZMo_Kria_3_7_25`;
3. all five TCP ports listen;
4. `get_data` returns a plausible measurement;
5. temperature and SDR frames are complete;
6. the front-panel display updates;
7. threshold changes restart only `gizmo-zmon.service`;
8. a reboot returns the same state and addresses.

Use `journalctl -u 'gizmo-*' -b` for diagnostics.

Do not run calibration as an initial smoke test; it moves relays, rewrites
device-specific tables, and takes several minutes.

## Rollback

```sh
sudo systemctl disable --now gizmo.target
sudo systemctl unmask rc-local.service rc.startup.service
sudo systemctl enable --now rc.startup.service
sudo systemctl start rc-local.service
```

If package-created network settings prevent management access, use the serial
console, set `GIZMO_NETWORK_MODE=none` in `/etc/gizmo/network.env`, and retry.
