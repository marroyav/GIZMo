# Security notes

This release preserves the instrument's existing wire protocols. They were
designed for an isolated controls network, not an untrusted LAN.

- ZeroMQ, temperature, ZMon, and SDR retain legacy unauthenticated transports
  and require network isolation. The public OPC UA configuration fails closed
  until the controlled site security configuration is installed.
- The dashboard on TCP 8080 is read-only and has no third-party browser assets,
  but its HTTP transport has no authentication or encryption and exposes
  current and historical instrument, network, firmware, and OS inventory.
- The SQLite historian is not a network listener. Its mode-`0660` query socket
  is private to the `gizmo` account, and the dashboard forwards only bounded
  read-only routes. The retained database still contains operational history
  and must be protected in backups and disk images.
- The canonical OPC UA namespace includes validated configuration writes and
  explicit methods. With `SecurityPolicy None`, any client that can reach TCP
  4840 can invoke them.
- A ZeroMQ client can request ZMon restarts and system-time changes through the
  compatibility API. The root helper validates and allow-lists the operation,
  but the external API remains unauthenticated.
- ZMon and SDR access physical memory directly. The display uses legacy GPIO
  sysfs access.
- Any inherited/default operating-system credential from the controlled device
  image must be rotated before network use.

Place PS interfaces on a restricted controls VLAN, filter ports 8080, 4840,
5005, 5055, 5555, and 5556 at the network boundary, and rotate the image's
default credential before production use. If the dashboard must cross that
boundary, use a site-managed authenticated TLS reverse proxy.

The OPC UA server code can configure `Basic256Sha256` signed/encrypted
channels. The controlled build must add and validate cryptography support, a
site trust list, and operator-role policy before enabling the service;
certificate paths alone are not a complete security boundary. An insecure
bench-mode opt-in must remain isolated. The compatibility ZeroMQ interface
should use CURVE or be firewalled away once all clients have migrated.
