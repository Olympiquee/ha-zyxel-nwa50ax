# Changelog

## 2.5.0 - 2026-11-16

### Added
- **Save config (write) button.** Makes the current configuration permanent, with a confirmation step: the first press posts a notification listing what `write` would make permanent (radio, SSID schedules, anything else that differs), the second press within 60 s saves. After the save it waits until the AP answers again and reports how long that took.
- **Reference configuration** used for that comparison: a copy of `show running-config` taken right after a successful `write`, compared block by block (line order ignored). Stored in Home Assistant's storage **with every secret masked** (admin password hash, WiFi keys -> short fingerprints), never logged, removed with the integration entry.
- Honest degraded modes: no reference yet (warns about disabled radios, creates the reference after the save), stale reference (the date of `startup-config.conf`, read with `dir /conf`, changed since - e.g. saved from the web UI), unreadable or truncated configuration (never compared).
- Notifications in French or English following Home Assistant's language.

### Changed
- The Guest switch's `write` now also refreshes the reference configuration in the background (after a `write`, running and saved configurations are identical).
- API: `async_read_config_state`, `async_write_config`, `add_persist_listener`; new `PRIORITY_BASELINE` so the reference is captured before any other pending change.

### Notes
- Nothing about the existing switches or the schedule/radio toggles changes: they are still temporary (no `write`), except Guest.
- Tests: fixtures `show_running_config.txt` (anonymized, fake secrets) and `dir_conf.txt`.

## 2.4.0 - 2026-11-16

### Added
- **Real SSID in switch names.** New *Display* section in the integration's options ("Use the real SSID in switch names", enabled by default). Every SSID switch, Guest included, is named after the SSID your devices see (`SSID MyHome Schedule`, `SSID MyHome_guest Schedule`) rather than the AP profile name (`SSID Home Schedule`, `Guest SSID`). Unchecking the option restores the previous naming.
- `broadcast_ssid` attribute on the schedule and Guest switches.
- **Switch inversion.** New *Invert switches* section in the options: choose, for each SSID, whether its switch is normal (ON = schedule active) or reversed (ON = SSID always on). Reversed switches get a `[reversed]` suffix in their name and a `reversed` attribute. By default only Guest is reversed, which is its historical behaviour, so existing automations keep working unchanged.

### Changed
- `manifest.json`: real documentation and issue-tracker URLs and code owner (were placeholders).
- `hacs.json`: `device_tracker` added to the declared domains.

### Notes
- Only the *displayed* name changes. Unique IDs still use the AP profile name, so existing entities keep their `entity_id` and history.
- No additional SSH command: the SSID name is read from the `show wlan-ssid-profile <profile>` output already fetched for the schedule state.

---

*Entries below are reconstructed from the git history (tags and commit subjects), not written at release time.*

## 2.3.1 - 2026-10-05
- Pending SSH fingerprint handling: the fingerprint is stored safely from the event loop, with error handling if it cannot be persisted, and a notification flow for the MikroTik resolver.
- Docstring pass across the component; translations (`en`, `fr`) and `strings.json` updated.

## 2.3.0 - 2026-09-14
- SSH host key pinning (Trust On First Use) for the AP and the optional MikroTik resolver, with a *Security* section in the options to reset remembered fingerprints.
- SSH layer reworked for performance and error handling: one multi-command SSH session per refresh group, CLI timings centralised in `const.py`.
- Radio switches share a single lock; numeric sensors report `unknown` instead of `0` when a value cannot be parsed.
- Reconfigure and re-authentication flows.

## 2.2.0 - 2026-09-09
- WiFi presence `device_tracker` platform (with anti-flapping grace period).
- Data assignment: each family of data can be moved between the fast / slow / daily refresh groups from the options.
- `_DataItemSpec` refactor (single source of truth for "which command, which parser, which group").

## 2.0.0 - 2026-09-07
- Three independent refresh groups (fast / slow / daily), each with its own interval and coordinator.
- One manual refresh button per group; optional MikroTik hostname resolver settings.
- SSID schedule switches reworked (state read from the AP, verified after each change).

## 1.8.1 - 2026-06-17
- Earlier 1.x releases: initial SSH integration, radio and Guest SSID control, per-SSID schedule switches, configurable refresh interval.
