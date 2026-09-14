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
