## Copilot Instructions (Sensorius)

### Project context
- This project is cross-platform (Raspberry Pi, macOS, Windows, Linux).
- It was originally designed for Raspberry Pi; only the Pi supports directly connected sensors/relay hardware.
- All platforms support Nodus (MQTT) sensors and switches.
- Prefer changes that are safe for low-power / low-RAM devices.
- Use absolute paths when referring to on-device files.

### Workflow preferences
- If the user asks for a change, proceed to edit without asking for per-edit confirmation.
- Keep edits minimal and targeted; avoid reformatting unrelated code.
- When switching repos, confirm which repo you will edit before making changes.

### Python standards
- Avoid heavy dependencies unless necessary.
- Prefer existing utilities in `saiUtils.py`, `saiDataLogger.py`, and `saiMQTTIngest.py`.
- Keep logging lightweight; use `printDM` and existing debug flags.
- In Python-rendered JS/HTML (for example `yield "..."` blocks in `saiHtml.py`), do **not** emit JavaScript `//` comments anywhere inside emitted strings (including trailing inline forms like `"}; //end"`). Use Python comments (`# ...`) outside emitted strings instead, because inline `//` in these builders has historically caused front-end rendering regressions (including missing gauges).

### MQTT & DB conventions
- Switch events should be written via `saiDataLogger.log_switch_event`.
- Use canonical switch keys: `<switch_id>::<channel_id>` when available.
- Preserve legacy topics/payloads unless the user explicitly opts in to breaking changes.

### Home Assistant integration (workflow)
- Sensorius publishes MQTT discovery/config via the Home Assistant bridge.
- Typical flow: configure broker + HA settings → start MQTT ingest → HA bridge advertises entities → HA controls/observes via MQTT topics.
- Prefer non-breaking changes to discovery payloads and entity IDs; preserve existing topics unless explicitly asked to change them.

### Collaboration intent
- Act as a high-functioning lab partner: propose meaningful improvements, surface risks, and offer practical next steps.

### Safety
- Don’t run destructive commands without explicit user request.
- Prefer idempotent operations and guard against missing config files.

### Testing / verification
- If tests are requested, prefer running the smallest relevant command.
- Suggest restart steps for systemd services only when needed.
