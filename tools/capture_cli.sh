#!/bin/bash
# Captures the read-only CLI output this integration parses from a Zyxel NWA
# access point, so it can be turned into test fixtures (tests/fixtures/).
#
# Usage:   tools/capture_cli.sh <ap-host> [ssh-user]
# Example: tools/capture_cli.sh 192.0.2.10 admin
#
# Only "show ..." commands are sent; nothing on the AP is changed. You type
# the SSH password yourself at the prompt. The raw transcript is written to
# captures/<host>.txt, which is NOT meant to be committed as is: it contains
# the MAC/IP addresses of your WiFi clients and must be anonymised first.
set -u

host="${1:?usage: $0 <ap-host> [ssh-user]}"
user="${2:-admin}"
dir="$(cd "$(dirname "$0")/.." && pwd)/captures"
out="$dir/$host.txt"
mkdir -p "$dir"

commands=(
  "show version"
  "show serial-number"
  "show mac"
  "show system uptime"
  "show cpu all"
  "show cpu status"
  "show mem status"
  "show wlan all"
  "show wlan radio macaddr"
  "show wlan-radio-profile all"
  "show wlan-ssid-profile all"
  "show wireless-hal current channel"
  "show wireless-hal station number"
  "show wireless-hal station info"
  "show wireless-hal statistic"
  "show interface all"
  "show port status"
  "show port setting"
)

# Each command is preceded by a marker line so the transcript can be split
# reliably afterwards ("!" starts a comment in the Zyxel CLI).
{
  sleep 4
  for c in "${commands[@]}"; do
    printf '! ===== %s =====\n' "$c"
    printf '%s\n' "$c"
    sleep 2
  done
  printf 'exit\n'
  sleep 2
} | ssh -tt -o StrictHostKeyChecking=accept-new -o PreferredAuthentications=password,keyboard-interactive \
        -o PubkeyAuthentication=no "$user@$host" | tee "$out"

echo
echo "Saved to $out ($(wc -l < "$out") lines)"
