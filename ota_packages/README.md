# Nodus OTA Packages

These directories are versioned CircuitPython application payloads consumed by
Sensorius's Nodus OTA flow. See `THIRD_PARTY_NOTICES.md` for redistribution,
provenance, integrity, and credential-review requirements.

Each `manifest.json` is authoritative for package target, deployed paths,
sizes, and SHA-256 digests. Do not edit a packaged file without regenerating
the manifest. New package builds should also record the exact source commit so
the compiled `.mpy` modules can be traced to reviewable source. When distributing
GPLv3-covered compiled payloads, provide complete Corresponding Source,
including build and installation scripts, through a distribution method allowed
by section 6 of [LICENSE](../LICENSE). A source commit reference alone is not
sufficient. The existing packages' source provenance and availability must be
resolved before redistributing them under GPLv3.
