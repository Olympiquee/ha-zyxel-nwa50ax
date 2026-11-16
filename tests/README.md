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
- `test_nwa130be.py` - the same parsers against the NWA130BE captures
  (three radios, 6 GHz clients, two Ethernet ports), plus: CLI prompt
  handling (`Router>` / `Router#`), the Guest switch condition, the device
  name logic, `tools/make_fixtures.py` on synthetic input, and a check that a
  two-radio NWA50AX still gets exactly its former entity unique_ids and
  names.
- `fixtures/v712/` - real, unedited `show ...` command output captured on
  firmware `7.12(ABYW.0)`. One file per command (SSID-specific commands get
  one file per SSID). If you're on a different firmware and hit a parsing
  issue, capturing your own output the same way and adding it as a new
  `fixtures/v<version>/` directory is the most useful thing you can
  contribute - see the module docstring in `zyxel_ssh_api.py` for the list of
  commands used.
- `fixtures/nwa130be_v730/` - NWA130BE output on firmware `V7.30(ACIL.4)`,
  one file per command of `tools/capture_cli.sh` (including commands the
  integration does not use yet), anonymised by `tools/make_fixtures.py`:
  client MACs `02:00:00:00:00:NN`, IPs `10.0.20.N`, SSIDs `Home`, factory
  values (`Zyxel-0000`, `Unconfigured`) kept.

## Adding a new firmware version

1. Run `tools/capture_cli.sh <ap-host> [user]`. It sends read-only `show`
   commands and saves the raw transcript to `captures/<host>.txt`. Do not
   commit that file: it contains your clients' MAC/IP addresses.
2. Run `python3 tools/make_fixtures.py captures/<host>.txt
   tests/fixtures/<model>_v<version>/`. It writes one anonymised `.txt` file
   per command (see the script's docstring for what is replaced). Read the
   result before committing it.
3. Duplicate `test_parsers.py`'s test functions for the new version (or
   parametrize over both), asserting the values you'd expect from your
   capture.
4. If a test fails, that's exactly the point - it means this firmware changed
   something in the output format, and you've found it via a regression test
   instead of a live production failure.
