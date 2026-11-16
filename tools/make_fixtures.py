#!/usr/bin/env python3
"""Split a raw Zyxel AP CLI capture into test fixtures and anonymise them.

Usage: python3 tools/make_fixtures.py captures/<file> tests/fixtures/<dir>

The capture is only read, never modified. Every command block, i.e. the
"Router> ! ===== <command> =====" marker, the echoed command and the output
up to the next prompt, becomes one fixture file named after the command
("show wlan-ssid-profile all" -> show_wlan_ssid_profile_all.txt). Fixtures
contain the output only (no prompt, no command line), with "\\n" line ends
and a trailing newline. Each profile of "show wlan-ssid-profile all" is also
written to show_wlan_ssid_profile_<name>.txt, keeping its "ssid profile: ..."
header line.

Anonymisation is deterministic. Values are numbered in order of first
appearance in the capture. No private value is hard-coded in this file.

  MAC    AP's own MACs (from "show mac" and "show wlan radio macaddr") keep
         their last byte and become 00:11:22:33:44:xx. All other MACs become
         02:00:00:00:00:NN. Separator and letter case are kept.
  IPv4   0.0.0.0 and netmasks (255.x.x.x) are kept. Others become 10.0.20.N,
         N starting at 2.
  IPv6   become fe80::N, N starting at 1.
         In column-aligned tables (a line with a run of two or more spaces),
         a shorter IPv4/IPv6 replacement is padded with spaces to the width
         of the original value, so the following columns stay in place.
  Serial the value of "serial number:" becomes S000000000000.
  SSID   values of "SSID:" and "Display SSID:" become Home, Net2, Net3, ... in
         order of appearance. Factory names Zyxel-XXXX become Zyxel-0000. The
         factory placeholder "Unconfigured" is kept as is. The same
         replacement is applied to every occurrence in the text.
  Host   client names ("Hostname:", "Device name:", "Client name:") become
         client-N.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROMPT = "Router>"
BLOCK_START = "Router> ! ===== "
BLOCK_END = " ====="
OWN_MAC_COMMANDS = ("show mac", "show wlan radio macaddr")
SSID_PROFILE_COMMAND = "show wlan-ssid-profile all"

ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"              # CSI sequences
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC sequences
    r"|\x1b[()][0-9A-Za-z]"                 # charset selection
    r"|\x1b[=>78]"                          # keypad / save-restore
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

MAC_RE = re.compile(
    r"(?<![0-9A-Za-z])[0-9A-Fa-f]{2}(?P<sep>[:-])"
    r"(?:[0-9A-Fa-f]{2}(?P=sep)){4}[0-9A-Fa-f]{2}(?![0-9A-Za-z])"
)
OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IPV4_RE = re.compile(rf"(?<![\d.])(?:{OCTET}\.){{3}}{OCTET}(?!\.?\d)")
IPV6_RE = re.compile(
    r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![0-9A-Za-z:])"
    r"|(?<![0-9A-Za-z:])(?=[0-9A-Fa-f:]*::)[0-9A-Fa-f:]+(?![0-9A-Za-z:])"
)
SERIAL_RE = re.compile(r"^[ \t]*serial number:[ \t]*(\S+)", re.M | re.I)
SSID_RE = re.compile(r"^[ \t]*(?:Display SSID|SSID):[ \t]*(\S.*?)[ \t]*$", re.M)
HOST_RE = re.compile(
    r"^([ \t]*(?:host ?name|device ?name|client ?name)[ \t]*:[ \t]*)(\S.*?)[ \t]*$",
    re.M | re.I,
)
FACTORY_SSID_RE = re.compile(r"^Zyxel-[0-9A-Fa-f]{4}$", re.I)
# Factory default SSID of unused profiles; not private, kept verbatim.
KEPT_SSIDS = {"unconfigured"}
PROFILE_HEADER_RE = re.compile(r"^ssid profile: (.*)$")


def normalize(raw: str) -> str:
    """Return the capture with \\n line ends and without ANSI/control chars."""
    # The terminal sends CRLF line ends and also a bare CR at the start of
    # many lines; the CRs carry no content, so they are dropped.
    text = raw.replace("\r", "")
    text = ANSI_RE.sub("", text)
    return CONTROL_RE.sub("", text)


def parse_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Return (command, output lines) for each command block.

    Lines before the first block (the echo of the sent input) are ignored.
    """
    blocks: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    echo_seen = False
    for line in text.split("\n"):
        if line.startswith(BLOCK_START) and line.endswith(BLOCK_END):
            if current is not None:
                blocks.append(current)
            command = line[len(BLOCK_START):-len(BLOCK_END)].strip()
            current = (command, [])
            echo_seen = False
        elif line.startswith(PROMPT):
            if current is not None and not echo_seen:
                echo_seen = True  # the echoed command line itself
            elif current is not None:
                blocks.append(current)
                current = None
        elif current is not None and echo_seen:
            current[1].append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def mac_key(mac: str) -> str:
    return mac.lower().replace("-", ":")


def apply_case(value: str, original: str) -> str:
    if re.search(r"[A-F]", original):
        return value.upper()
    if re.search(r"[a-f]", original):
        return value.lower()
    return value


class Anonymiser:
    """Builds the value mappings from the capture and applies them."""

    def __init__(self, text: str) -> None:
        self.text = text
        blocks = parse_blocks(text)

        own = set()
        for command, lines in blocks:
            if command in OWN_MAC_COMMANDS:
                for line in lines:
                    own.update(mac_key(m.group(0)) for m in MAC_RE.finditer(line))

        self.mac_map: dict[str, str] = {}
        clients = 0
        for m in MAC_RE.finditer(text):
            key = mac_key(m.group(0))
            if key in self.mac_map:
                continue
            if key in own:
                self.mac_map[key] = f"00:11:22:33:44:{key[-2:]}"
            else:
                clients += 1
                self.mac_map[key] = f"02:00:00:00:00:{clients:02x}"
        own_fakes = [v for k, v in self.mac_map.items() if k in own]
        if len(set(own_fakes)) != len(own_fakes):
            sys.exit("own MAC addresses do not have distinct last bytes")

        self.ipv4_map: dict[str, str] = {}
        for m in IPV4_RE.finditer(text):
            ip = m.group(0)
            if ip == "0.0.0.0" or ip.startswith("255.") or ip in self.ipv4_map:
                continue
            self.ipv4_map[ip] = f"10.0.20.{len(self.ipv4_map) + 2}"

        self.ipv6_map: dict[str, str] = {}
        for m in IPV6_RE.finditer(text):
            addr = m.group(0).lower()
            if addr not in self.ipv6_map:
                self.ipv6_map[addr] = f"fe80::{len(self.ipv6_map) + 1}"

        self.serial_values = {m.group(1).lower() for m in SERIAL_RE.finditer(text)}

        self.ssid_map: dict[str, str] = {}
        normal = 0
        for m in SSID_RE.finditer(text):
            value = m.group(1)
            key = value.lower()
            if key in self.ssid_map or key in KEPT_SSIDS:
                continue
            if FACTORY_SSID_RE.match(value):
                self.ssid_map[key] = "Zyxel-0000"
            else:
                self.ssid_map[key] = "Home" if normal == 0 else f"Net{normal + 1}"
                normal += 1

        self.host_map: dict[str, str] = {}
        for m in HOST_RE.finditer(text):
            key = m.group(2).lower()
            if key not in self.host_map:
                self.host_map[key] = f"client-{len(self.host_map) + 1}"

        self.counts = {
            "mac_own": len(own),
            "mac_client": len(self.mac_map) - len(own),
            "ipv4": len(self.ipv4_map),
            "ipv6": len(self.ipv6_map),
            "serial": len(self.serial_values),
            "ssid": len(self.ssid_map),
            "host": len(self.host_map),
        }

    def _word_pattern(self, values) -> re.Pattern:
        alternatives = sorted((re.escape(v) for v in values), key=len, reverse=True)
        return re.compile(
            r"(?<![0-9A-Za-z_-])(?:" + "|".join(alternatives) + r")(?![0-9A-Za-z_-])",
            re.I,
        )

    def apply(self) -> str:
        text = self.text

        if self.serial_values:
            pattern = self._word_pattern(self.serial_values)
            text = pattern.sub("S000000000000", text)

        if self.ssid_map:
            pattern = self._word_pattern(self.ssid_map)
            text = pattern.sub(lambda m: self.ssid_map[m.group(0).lower()], text)

        if self.host_map:
            text = HOST_RE.sub(
                lambda m: m.group(1) + self.host_map[m.group(2).lower()], text
            )

        def repl_mac(m: re.Match) -> str:
            fake = self.mac_map[mac_key(m.group(0))].replace(":", m.group("sep"))
            return apply_case(fake, m.group(0))

        text = MAC_RE.sub(repl_mac, text)
        text = IPV4_RE.sub(
            lambda m: keep_column_width(m, self.ipv4_map.get(m.group(0), m.group(0))), text
        )
        text = IPV6_RE.sub(
            lambda m: keep_column_width(m, self.ipv6_map.get(m.group(0).lower(), m.group(0))),
            text,
        )
        return text


def keep_column_width(m: re.Match, replacement: str) -> str:
    """Pad `replacement` to the width of the matched value inside tables.

    A line counts as a table row when it contains a run of two or more
    spaces and the value is followed by a space (not at the end of the line).
    "key: value" lines are left alone.
    """
    original = m.group(0)
    if len(replacement) >= len(original):
        return replacement
    text = m.string
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    line = text[line_start:line_end if line_end != -1 else len(text)]
    in_table = "  " in line and text[m.end():m.end() + 1] == " "
    if not in_table:
        return replacement
    return replacement.ljust(len(original))


def block_text(lines: list[str]) -> str:
    return "".join(f"{line}\n" for line in lines)


def fixture_name(command: str) -> str:
    return command.replace(" ", "_").replace("-", "_") + ".txt"


def split_profiles(lines: list[str]) -> dict[str, list[str]]:
    profiles: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines:
        header = PROFILE_HEADER_RE.match(line)
        if header:
            name = header.group(1).strip()
            current = profiles.setdefault(name, [])
        if current is not None:
            current.append(line)
    return profiles


def profile_file_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    return f"show_wlan_ssid_profile_{slug}.txt"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("capture", type=Path, help="raw capture, e.g. captures/<host>.txt")
    parser.add_argument("outdir", type=Path, help="target directory for the fixtures")
    args = parser.parse_args()

    raw = args.capture.read_bytes().decode("utf-8", errors="replace")
    anonymiser = Anonymiser(normalize(raw))
    blocks = parse_blocks(anonymiser.apply())
    if not blocks:
        sys.exit("no command blocks found in capture")

    args.outdir.mkdir(parents=True, exist_ok=True)
    written = []
    for command, lines in blocks:
        path = args.outdir / fixture_name(command)
        path.write_text(block_text(lines), encoding="utf-8", newline="\n")
        written.append((path.name, len(lines)))
        if command == SSID_PROFILE_COMMAND:
            for name, profile_lines in split_profiles(lines).items():
                path = args.outdir / profile_file_name(name)
                path.write_text(block_text(profile_lines), encoding="utf-8", newline="\n")
                written.append((path.name, len(profile_lines)))

    for name, count in written:
        print(f"{count:5d}  {name}")
    print(f"{len(written)} files written to {args.outdir}")
    print("replaced values: " + ", ".join(f"{k}={v}" for k, v in anonymiser.counts.items()))


if __name__ == "__main__":
    main()
