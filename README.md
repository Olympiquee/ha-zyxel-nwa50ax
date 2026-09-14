# Zyxel NWA50AX Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
![Version](https://img.shields.io/badge/version-2.3.0-blue.svg)

Home Assistant custom integration for a **Zyxel NWA50AX** WiFi access point (standalone mode) over SSH.

See [CHANGELOG.md](CHANGELOG.md) for the full version history.

## Supported firmware

| Firmware | Status |
|---|---|
| 7.10(ABYW.3) | Validated (manufacturer CLI reference + real captures) |
| 7.12(ABYW.0) | Validated (real CLI captures, see `tests/fixtures/v712/`) |

No breaking CLI format change was found between these two firmware lines for the commands this integration uses. Real captures are kept as regression-test fixtures so a future firmware update can be checked the same way, without needing physical access to the AP for every code change.

## Features

### Sensors
- System: Uptime, Firmware/model, Last Seen (reactive to all 3 refresh groups)
- Performance: CPU (current + 1/5 min avg, any number of cores), Memory usage
- WiFi clients: total / 2.4GHz / 5GHz counts, with a detailed attribute list (MAC, IP, hostname, SSID, band, RSSI)
- Ethernet port: status, TX/RX rate, TX/RX total bytes
- Radio: 2.4GHz / 5GHz status with active SSIDs

Numeric/boolean sensors report `unknown` (not `0` / `False`) when a value could not be parsed, so a parsing failure is never silently confused with a real zero reading.

### Switches
- Guest SSID (always-on vs. follows its configured schedule)
- One schedule switch per additional SSID, auto-detected from the AP (Home/IoT/etc. - whatever you actually have configured)
- Radio 2.4GHz / 5GHz on-off

### Device tracker
- One entity per WiFi client ever seen, created dynamically as they connect
- Presence uses a grace period (anti-flapping) so a phone's WiFi power-saving doesn't cause spurious home/away flips
- Display name uses the resolved hostname (see MikroTik resolver below); if two known devices share the same generic hostname (e.g. two "iPhone"), a short MAC-based suffix is added automatically to tell them apart
- Attach these to a HA `Person` to get presence-based automations for free

### Button
- Reboot
- One manual refresh button per refresh group (fast/slow/daily) - jumps ahead of the scheduled automatic refresh

### Diagnostics
Available from the integration's device page ("Download diagnostics"). Contains model/firmware, configured intervals, data-group assignment, last-seen timestamp, and connection health counters. Never contains passwords, client MAC/IP addresses, or SSID names.

## Requirements

- Zyxel NWA50AX access point (standalone mode)
- SSH access enabled on the AP
- Home Assistant 2024.1.0 or newer (some options-flow features require a reasonably recent Core version - see Troubleshooting if the options page fails to open)
- Python package: `paramiko>=2.12.0`

## Installation

### HACS (recommended)
1. HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/Olympiquee/ha-zyxel-nwa50ax` as an Integration
3. Install, then restart Home Assistant

### Manual
1. Copy `custom_components/ha_zyxel` into your `config/custom_components/` directory
2. Restart Home Assistant (a config-flow-carrying integration needs a full restart, not just a reload, to pick up code changes)
3. Settings → Devices & Services → Add Integration → search "Zyxel"

## Initial setup

You'll be asked for:
- **Host**: AP IP address (e.g. `10.0.20.2`)
- **Username** / **Password**: SSH admin credentials

The very first successful connection also memorizes the AP's SSH host key fingerprint (see Security below) - nothing to do manually, it happens automatically.

## Configuration (after setup)

Open the integration's **Configure** button to reach a menu with 4 sections:

### Refresh intervals
Three independent intervals, one per refresh group:
- **Fast** (default 2 min): meant for whatever changes often
- **Slow** (default 1h): meant for data that rarely changes minute to minute
- **Daily** (default 24h): meant for near-static data (firmware/model)

### Data assignment
Which family of data (radio state, WiFi clients, CPU, memory, uptime, interfaces, Ethernet port, SSID schedules, model/firmware) belongs to which of the 3 groups above. The default split:

| Group | Data |
|---|---|
| Fast | Radio state, WiFi clients |
| Slow | CPU, memory, uptime, interfaces, Ethernet port, SSID schedules |
| Daily | Model / firmware |

Change any of these freely - a change takes effect immediately (the integration reloads itself), no restart needed. Every read-only command for a given cycle is sent in a single SSH session, so reassigning items doesn't multiply the number of connections to the AP.

### MikroTik hostname resolver (optional)
If your DHCP server is a MikroTik router rather than the AP itself, enable this to resolve WiFi client hostnames from `/ip dhcp-server lease print`. It has its own SSH connection, its own refresh cycle and its own cache - completely decoupled from the Zyxel connection, so a MikroTik outage never affects AP data collection.

Why MikroTik and not a plain reverse-DNS lookup: in a setup where the router itself is the DNS resolver clients talk to (forwarding upstream to something like AdGuard/Pi-hole), reverse DNS on the LAN typically has no records for internal clients, and the upstream resolver never even sees individual client IPs. The DHCP lease table is the actual source of truth for "which device has this name."

### Security - SSH fingerprints
Both the Zyxel and (if enabled) MikroTik SSH connections use Trust On First Use: the host key fingerprint is memorized on first connection and compared on every connection afterward, rather than blindly accepted every time (`AutoAddPolicy`). If the fingerprint ever changes, the connection is refused with a clear error - this could mean the device was legitimately replaced/factory-reset, or it could mean something is intercepting the connection on your LAN.

This screen shows both currently-memorized fingerprints and lets you reset either one (e.g. after a legitimate hardware replacement), which makes the integration trust and re-memorize whatever key it sees on the next connection.

## Reconfigure / Re-authenticate

- **Reconfigure** (from the integration's menu): update host/username/password without removing and re-adding the integration.
- **Reauthenticate**: triggered automatically if the AP rejects the stored credentials (wrong/changed password), prompting for a new password without losing any other configuration.

## SSH commands used

Read-only (grouped per cycle into a single SSH session):
```
show version
show system uptime
show cpu all
show mem status
show wlan all
show wireless-hal station info
show interface all
show port status
show wlan-ssid-profile <name>      # one per detected SSID
```

Actions (radio/SSID toggle send their config command and read back the verification state in the same session; radio *activation* specifically uses separate sessions - see code comments for why):
```
configure terminal
wlan slot1|slot2
[no ]activate
exit
exit

configure terminal
wlan-ssid-profile <name>
[no ]ssid-schedule
exit
write        # Guest only, to match its historical persistent behavior
```

## Testing

`tests/fixtures/` contains real CLI output captured from a physical NWA50AX, used as regression fixtures so a firmware update or a future code change can be checked against known-good output without needing the physical device. See `tests/README.md`.

## Troubleshooting

**Options page fails to open ("500 Internal Server Error")**: usually means Home Assistant Core hasn't picked up the latest integration code - do a full HA restart (not just "reload"), not just replacing the files.

**Integration won't connect**: verify SSH is enabled on the AP, test manually with `ssh admin@<ap_ip>`, then check logs:
```yaml
logger:
  logs:
    custom_components.ha_zyxel: debug
```

**"La clé SSH ... a changé" / SSH host key changed error**: the AP's SSH key no longer matches what was memorized. If you didn't replace/factory-reset the AP, treat this as a potential security issue on your LAN before resetting the fingerprint. If the change is legitimate, go to Configure → Security and reset the corresponding fingerprint.

**No hostnames for WiFi clients**: enable and configure the MikroTik resolver (Configure → MikroTik). Plain reverse DNS was removed - see the "Data assignment" section above for why.

## License

MIT - see LICENSE.

## Credits

Original inspiration from [ha-zyxel](https://github.com/zulufoxtrot/ha-zyxel) (for the NR7101 router). Adapted and substantially rewritten for the NWA50AX in standalone mode.
