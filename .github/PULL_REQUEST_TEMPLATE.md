## Summary

Describe what changed and why.

## Verification

List the commands run and their results. Include the operating system, Python
version, and any relevant MQTT broker or hardware assumptions.

## Contributor checklist

- [ ] This pull request is focused on one logical change.
- [ ] I added or updated tests for behavior changes.
- [ ] I updated the canonical documentation when behavior or configuration changed.
- [ ] I did not commit credentials, private runtime configuration, databases, or logs.
- [ ] I preserved compatibility-sensitive MQTT, settings, switch identity, and persistence behavior, or documented an intentional break.

## Maintainer verification

- [ ] Required remote checks pass.
- [ ] `npm run validate:pr` passes on a trusted host when the change can affect rendered UI behavior.
- [ ] Hardware-, broker-, onboarding-, and platform-specific behavior not covered remotely is recorded below.

## Residual risk or unverified areas

Describe anything that was not exercised, or write `None`.
