# Security notes

This release preserves the instrument's existing wire protocols. They were
designed for an isolated controls network, not an untrusted LAN.

- By default, ZeroMQ, temperature, ZMon, SDR, and OPC UA endpoints have no
  transport authentication or encryption.
- The canonical OPC UA namespace includes validated configuration writes and
  explicit methods. With `SecurityPolicy None`, any client that can reach TCP
  4840 can invoke them.
- A ZeroMQ client can request ZMon restarts and system-time changes through the
  compatibility API. The root helper validates and allow-lists the operation,
  but the external API remains unauthenticated.
- ZMon and SDR access physical memory directly. The display uses legacy GPIO
  sysfs access.
- The supplied user manual contains a default operating-system credential.

Place PS interfaces on a restricted controls VLAN, filter ports 4840, 5005,
5055, 5555, and 5556 at the network boundary, and rotate the image's default
credential before production use.

The OPC UA server code can configure `Basic256Sha256` signed/encrypted
channels, but the recovered offline dependency set does not bundle Python
cryptography support or a site trust-list/operator-role policy. Add and
validate those pieces before disabling `SecurityPolicy None`; certificate
paths alone are not a complete security boundary. The compatibility ZeroMQ
interface should use CURVE or be firewalled away once all clients have
migrated.
