# Time synchronization

The Kria uses the Ubuntu `systemd-timesyncd.service`; it is enabled at boot by
the runtime package. The packaged drop-in is:

```text
/usr/lib/systemd/timesyncd.conf.d/60-gizmo.conf
```

The active maintenance link has no default route or DNS. While that link is in
use, the Kria therefore queries the directly connected workstation at
`192.168.191.90`. The workstation Windows Time service is configured by:

```text
deploy/windows/enable-gizmo-ntp.ps1
```

That script permits UDP/123 only from `192.168.191.53` and synchronizes Windows
from Fermilab's canonical `ntp.fnal.gov` pool, with `nsntp1.fnal.gov` through
`nsntp5.fnal.gov` retained as fallback peers. The source list is maintained in:

```text
https://github.com/fermilab-context-rpms/fermilab-conf_timesync
```

The direct Fermilab names are also configured as Kria fallback sources for
when its registered interface has carrier, DNS, and a route. The root-distance
ceiling is 15 seconds because Windows Time advertises its upstream uncertainty
above systemd-timesyncd's five-second default even when the measured local-link
offset is small.

Verify the Kria with:

```sh
timedatectl show \
  --property=Timezone \
  --property=NTP \
  --property=NTPSynchronized
timedatectl timesync-status
systemctl is-enabled systemd-timesyncd.service
```

The expected state is `Timezone=America/Denver`, `NTP=yes`,
`NTPSynchronized=yes`, and an enabled service. OPC UA publishes the same state
under `Time.NtpSynchronized`, `Time.NtpServiceActive`, and `Time.NtpService`.
