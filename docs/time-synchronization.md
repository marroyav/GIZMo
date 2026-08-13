# Time synchronization

The Kria uses `systemd-timesyncd.service`. Time sources are site configuration,
not public repository defaults. The repository therefore ships only:

- `config/60-gizmo-timesyncd.conf.example`, containing reserved
  `example.invalid` names; and
- `deploy/windows/enable-gizmo-ntp.ps1`, which requires the approved upstream
  peers and GIZMo address as explicit parameters.

The controlled build must provide `site-config/60-gizmo-timesyncd.conf` before
an installable package can be produced. When a workstation relay is used, its
firewall rule should allow UDP/123 only from the assigned GIZMo address.

Example Windows invocation:

```powershell
.\enable-gizmo-ntp.ps1 `
  -UpstreamPeers @("time1.example.invalid", "time2.example.invalid") `
  -GizmoAddress "192.0.2.10"
```

The values above are documentation-only and must be replaced. Verify the Kria
with:

```sh
timedatectl show \
  --property=Timezone \
  --property=NTP \
  --property=NTPSynchronized
timedatectl timesync-status
systemctl is-enabled systemd-timesyncd.service
```

The expected production state is `NTP=yes`, `NTPSynchronized=yes`, and an
enabled service. OPC UA publishes the same state under
`Time.NtpSynchronized`, `Time.NtpServiceActive`, and `Time.NtpService`.
