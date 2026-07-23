# Security Policy

## Deployment boundary

Sensorius is designed for a trusted private LAN. Its web UI is not a complete
authentication boundary: several settings, onboarding, calibration, switch,
and maintenance routes can change hub or device state without a login session.
`SAI_WEB_API_KEY` protects selected API and OTA operations when configured; it
does not place the entire web application behind authentication.

The factory environment binds HTTP to `0.0.0.0`, which makes the UI reachable
through every network interface allowed by the host firewall. Do not expose a
Sensorius HTTP or MQTT port directly to the Internet, configure router port
forwarding to it, or place it behind a public reverse proxy. Use all of the
following controls:

- Keep the hub, broker, and Nodus devices on a trusted LAN or isolated IoT VLAN.
- Restrict inbound HTTP and MQTT with the host and network firewalls.
- Set `SENSORIUS_HTTP_HOST=127.0.0.1` when only local access is needed.
- Use a VPN or another authenticated private-access layer for remote access.
- Configure MQTT authentication and TLS when the broker crosses a trusted
  network boundary.
- Set a strong, unique `SAI_WEB_API_KEY`, while recognizing its limited scope.

## Secrets at rest

Do not commit runtime settings or `.env` files. Sensorius settings-manager
obfuscation is not encryption and must not be treated as a security boundary.
Nodus Wi-Fi credentials are stored in plaintext on the device filesystem, and
integration credentials may be recoverable by anyone who can read the hub's
runtime files. Protect the host account, filesystem, backups, and diagnostic
exports accordingly.

## Reporting a vulnerability

If you discover a security issue, please report it privately.

- Email: mot.yelraf@gmail.com

Please include:

- A clear description of the issue
- Steps to reproduce
- Any relevant logs or screenshots

We will acknowledge reports within 7 days and provide a timeline for fixes when possible.
