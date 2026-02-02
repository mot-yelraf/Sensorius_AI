## Copilot Instructions (Sensorius)

### Project context
- This project runs on a Raspberry Pi (Linux, systemd).
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

### MQTT & DB conventions
- Switch events should be written via `saiDataLogger.log_switch_event`.
- Use canonical switch keys: `<switch_id>::<channel_id>` when available.
- Preserve legacy topics/payloads unless the user explicitly opts in to breaking changes.

### Safety
- Don’t run destructive commands without explicit user request.
- Prefer idempotent operations and guard against missing config files.

### Testing / verification
- If tests are requested, prefer running the smallest relevant command.
- Suggest restart steps for systemd services only when needed.
