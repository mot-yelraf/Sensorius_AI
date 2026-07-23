# Third-Party And Binary Notices

Sensorius source code is distributed under the MIT License in `LICENSE`.
`package.json` is private tooling metadata and uses the same MIT designation;
it is not a separately published npm package.

## JPL DE421 Ephemeris

`data/skyfield/de421.bsp` is the JPL DE421 planetary ephemeris used by
Skyfield for offline astronomical calculations.

- Authoritative source:
  `https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de421.bsp`
- Local and upstream size: `16,788,480` bytes
- SHA-256: `a20a7139da04cbc462454634918e9a9ca69127044e2cc9d4f9c16e238d2deedc`
- Verification date: 2026-07-22
- Verification result: byte-for-byte identical to the authoritative source

NASA/JPL NAIF permits redistribution of SPICE kernels it distributes when the
files remain unmodified and encourages attribution. Preserve this notice and
the kernel bytes when redistributing Sensorius. The NAIF archival copy includes
an additional comment record and therefore has a different size and checksum;
the Sensorius file matches the JPL Solar System Dynamics download above.

Skyfield itself is an MIT-licensed Python dependency. Its package license and
other installed dependency notices remain governed by their respective
distributions.

## Nodus OTA Payloads

`ota_packages/` contains project-maintained CircuitPython application update
payloads for cPyNodus II and cPyNodus III. Most modules are compiled MicroPython
`.mpy` files, accompanied by a readable `code.py` entry point and a manifest
that records each deployed path, byte size, and SHA-256 digest.

These are application payloads, not CircuitPython firmware images. No
third-party CircuitPython runtime binary is bundled in the package directories.
The repository's MIT license applies to the Sensorius-maintained payloads to
the extent the project owns the corresponding source.

Before publishing a new OTA package:

1. Build it only from source the publisher owns or is authorized to distribute.
2. Record the source repository and exact source commit in release notes or the
   manifest build metadata.
3. Preserve notices for any copied third-party source included in the payload.
4. Verify every packaged file against the manifest before release.
5. Scan readable and compiled payloads for Wi-Fi, MQTT, API, and other runtime
   credentials; packages must contain only defaults or placeholders.

The three packages present on 2026-07-22 passed manifest size/SHA-256 checks and
a static string scan found no committed runtime credentials. Their manifests
do not record a source commit or license provenance, so reproducible source
traceability remains a release-process requirement for future packages.
