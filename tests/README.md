# Tests

## Running

```bash
pip install pytest homeassistant paramiko
pytest tests/
```

(or `pytest-homeassistant-custom-component` instead of a full `homeassistant`
install, if you prefer a lighter test-only dependency).

## What's here

- `test_parsers.py` - regression tests for every `_parse_*` method in
  `zyxel_ssh_api.py`, run against real CLI output captured from a physical
  NWA50AX (see `fixtures/`). These exist so a firmware update, or a future
  change to the parsing code, can be checked without needing physical access
  to the AP.
- `fixtures/v712/` - real, unedited `show ...` command output captured on
  firmware `7.12(ABYW.0)`. One file per command (SSID-specific commands get
  one file per SSID). If you're on a different firmware and hit a parsing
  issue, capturing your own output the same way and adding it as a new
  `fixtures/v<version>/` directory is the most useful thing you can
  contribute - see the module docstring in `zyxel_ssh_api.py` for the list of
  commands used.

- `test_ssid_names.py` - naming and inversion rules of the SSID switches (pure
  logic, no Home Assistant needed).
- `test_config_snapshot.py` - reference configuration used by the Save config
  button: secret masking, block-by-block comparison, report statuses and
  messages.
- `test_config_io.py` - the API side of the same feature (reading the
  configuration, `write`, persistence listener) against a simulated SSH session.

### Two fixtures that are NOT raw captures

`fixtures/v712/show_running_config.txt` keeps the real structure of a capture
(including the line-order quirks the comparison has to tolerate) but is
**trimmed and anonymized: every secret in it is a fake value starting with
`FAKE`**, and the tests assert that no `FAKE` value survives masking.
`dir_conf.txt` is a real `dir /conf` listing.

> **Never commit a real `show running-config`.** It contains the admin password
> hash and the encrypted WiFi keys.

## Adding a new firmware version

1. SSH into the AP and run each command listed in `zyxel_ssh_api.py`'s module
   docstring, saving the raw output.
2. Create `tests/fixtures/v<version>/` with one `.txt` file per command
   (see `v712/` for the naming convention).
3. Duplicate `test_parsers.py`'s test functions for the new version (or
   parametrize over both), asserting the values you'd expect from your
   capture.
4. If a test fails, that's exactly the point - it means this firmware changed
   something in the output format, and you've found it via a regression test
   instead of a live production failure.
