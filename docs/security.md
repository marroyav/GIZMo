# Security notes

This release preserves the instrument's existing wire protocols. They were
designed for an isolated controls network, not an untrusted LAN.

- ZeroMQ, temperature, ZMon, SDR, and OPC-UA endpoints have no transport
  authentication or encryption.
- A ZeroMQ client can request ZMon restarts and system-time changes through the
  compatibility API. The root helper validates and allow-lists the operation,
  but the external API remains unauthenticated.
- ZMon and SDR access physical memory directly. The display uses legacy GPIO
  sysfs access.
- The supplied user manual contains a default operating-system credential.

Place PS interfaces on a restricted controls VLAN, filter ports 4840, 5005,
5055, 5555, and 5556 at the network boundary, and rotate the image's default
credential before production use.

Future protocol work should add authenticated ZeroMQ/OPC-UA transport without
changing the package/process ownership model.
