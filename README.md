# Zyxel NWA50AX Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
![Version](https://img.shields.io/badge/version-1.1.0-blue.svg)

Home Assistant custom integration for **Zyxel NWA50AX** WiFi Access Point using SSH.

## ✨ Features

### 📊 18 Sensors
- **System**: Uptime (formatted), Firmware version, Last Seen timestamp
- **Performance**: CPU usage (current + 1min/5min avg), Memory usage
- **WiFi Clients**: Total clients, 2.4GHz clients, 5GHz clients
  - **NEW**: Client hostnames via reverse DNS
  - Detailed info: MAC, IP, hostname, SSID, RSSI, band
- **Ethernet Port**: Status, TX/RX rates, Total bytes transferred
- **Radio**: 2.4GHz and 5GHz radio status with active SSIDs

### 🎛️ Controls
- **Switch**: Guest SSID on/off control
- **NEW Switch**: Radio 2.4GHz on/off control
- **NEW Switch**: Radio 5GHz on/off control
- **Button**: Reboot access point

### 🆕 Version 1.1.0 Features
- ✅ **Client hostnames** - See device names via reverse DNS
- ✅ **Configurable update interval** - Choose 30-300 seconds (default: 60s)
- ✅ **Radio control switches** - Turn 2.4GHz/5GHz radios on/off
- ✅ **Formatted uptime** - Display as "1d 5h 34m" instead of seconds
- ✅ **Last Seen sensor** - Track last successful AP communication
- ✅ **Batch command execution** - Fixed "Socket is closed" errors

### 📱 Detailed Client Information
Each client includes:
- MAC address
- IP address
- **Hostname** (via reverse DNS) ← NEW
- SSID name
- WiFi band (2.4GHz / 5GHz)
- Signal strength (RSSI in dBm)
- TX/RX rates
- Security type
- Connection time

## 📋 Requirements

- Zyxel NWA50AX access point
- Firmware V7.10(ABYW.3) or compatible
- SSH access enabled on the AP
- Home Assistant 2024.1.0 or newer
- Python package: `paramiko>=2.12.0`

## 🚀 Installation

### Option 1: Manual Installation

1. Download this repository
2. Copy the `custom_components/ha_zyxel` folder to your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant
4. Go to **Configuration** → **Integrations**
5. Click **+ Add Integration**
6. Search for **"Zyxel"**
7. Enter your NWA50AX details:
   - Host: IP address of your AP (e.g., `192.168.1.2`)
   - Port: `22` (SSH port)
   - Username: `admin`
   - Password: Your admin password
   - **Update Interval**: 30-300 seconds (default: 60s) ← NEW

### Option 2: HACS (Recommended)

1. Open HACS
2. Go to **Integrations**
3. Click the three dots in the top right corner
4. Select **Custom repositories**
5. Add this repository URL: `https://github.com/Olympiquee/ha-zyxel-nwa50ax`
6. Select **Integration** as category
7. Click **Install**
8. Restart Home Assistant
9. Follow steps 4-7 from Manual Installation above

## ⚙️ Configuration

### Enable SSH on NWA50AX

1. Log in to your NWA50AX web interface
2. Go to **Management** → **Services**
3. Enable **SSH**
4. Set port to `22`
5. Allow access from your local network
6. Save settings

### Radio Control

The integration includes switches to control WiFi radios:

- **Radio 2.4GHz Switch**: Turn the 2.4GHz radio on/off (affects all SSIDs on this band)
- **Radio 5GHz Switch**: Turn the 5GHz radio on/off (affects all SSIDs on this band)
- **Guest SSID Switch**: Control Guest SSID schedule
  - ON = Guest SSID always active (ignores schedule)
  - OFF = Guest SSID follows configured schedule

## 📊 Example Dashboard

```yaml
type: vertical-stack
title: 🌐 Zyxel NWA50AX
cards:
  # Controls
  - type: entities
    title: WiFi Control
    entities:
      - entity: switch.zyxel_nwa50ax_radio_2_4ghz
        name: Radio 2.4GHz
      - entity: switch.zyxel_nwa50ax_radio_5_ghz
        name: Radio 5GHz
      - entity: switch.zyxel_nwa50ax_guest_ssid
        name: Guest SSID
      - entity: button.zyxel_nwa50ax_reboot

  # Information
  - type: entities
    title: System
    entities:
      - sensor.zyxel_nwa50ax_uptime
      - sensor.zyxel_nwa50ax_last_seen
      - sensor.zyxel_nwa50ax_firmware
      - sensor.zyxel_nwa50ax_connected_clients

  # Performance
  - type: horizontal-stack
    cards:
      - type: gauge
        entity: sensor.zyxel_nwa50ax_cpu_usage
        name: CPU
        min: 0
        max: 100
      - type: gauge
        entity: sensor.zyxel_nwa50ax_memory_usage
        name: Memory
        min: 0
        max: 100

  # WiFi Clients with Hostnames
  - type: markdown
    content: |
      ## Connected Clients
      {% set clients = state_attr('sensor.zyxel_nwa50ax_connected_clients', 'client_list') %}
      {% if clients %}
        {% for client in clients %}
          - **{{ client.hostname if client.hostname else 'Unknown' }}**
            - IP: {{ client.ip }}
            - SSID: {{ client.ssid }} ({{ client.band }})
            - Signal: {{ client.rssi_dbm }} dBm
        {% endfor %}
      {% else %}
        No clients connected
      {% endif %}
```

## 🤖 Example Automations

### Night Mode (Disable Guest + 2.4GHz)

```yaml
automation:
  - alias: "WiFi Night Mode"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: switch.turn_off
        target:
          entity_id:
            - switch.zyxel_nwa50ax_guest_ssid
            - switch.zyxel_nwa50ax_radio_2_4ghz

  - alias: "WiFi Day Mode"
    trigger:
      - platform: time
        at: "06:00:00"
    action:
      - service: switch.turn_on
        target:
          entity_id:
            - switch.zyxel_nwa50ax_guest_ssid
            - switch.zyxel_nwa50ax_radio_2_4ghz
```

### Alert on AP Offline

```yaml
automation:
  - alias: "Alert AP Offline"
    trigger:
      - platform: template
        value_template: >
          {{ (as_timestamp(now()) - as_timestamp(states('sensor.zyxel_nwa50ax_last_seen'))) > 300 }}
    action:
      - service: notify.mobile_app
        data:
          title: "⚠️ AP Offline"
          message: "Last seen {{ ((as_timestamp(now()) - as_timestamp(states('sensor.zyxel_nwa50ax_last_seen'))) / 60) | round(1) }} minutes ago"
```

### Alert on High CPU

```yaml
automation:
  - alias: "Alert NWA50AX High CPU"
    trigger:
      - platform: numeric_state
        entity_id: sensor.zyxel_nwa50ax_cpu_usage
        above: 80
        for:
          minutes: 5
    action:
      - service: notify.mobile_app
        data:
          title: "⚠️ NWA50AX High CPU"
          message: "CPU at {{ states('sensor.zyxel_nwa50ax_cpu_usage') }}% for 5 minutes"
```

## 🔧 SSH Commands Used

The integration uses the following validated SSH commands:

### Data Collection
- `show version` - Model, firmware, build date
- `show system uptime` - Uptime since last reboot
- `show cpu all` - CPU usage per core + averages
- `show mem status` - Memory usage percentage
- `show wireless-hal station info` - Connected WiFi clients (detailed)
- `show interface all` - Network interfaces status
- `show wlan all` - Radio status and SSIDs
- `show port status` - Ethernet port statistics

### Control Commands

**Radio 2.4GHz (slot1 / profile default)**:
```bash
configure terminal
wlan-radio-profile default
activate          # Turn on
# or
no activate       # Turn off
exit
write
```

**Radio 5GHz (slot2 / profile default2)**:
```bash
configure terminal
wlan-radio-profile default2
activate          # Turn on
# or
no activate       # Turn off
exit
write
```

**Guest SSID Schedule**:
```bash
configure terminal
wlan-ssid-profile Guest
ssid-schedule     # Enable schedule
# or
no ssid-schedule  # Disable schedule (always on)
exit
write
```

## 🐛 Troubleshooting

### Integration won't connect

1. Verify SSH is enabled on the NWA50AX
2. Test SSH manually: `ssh admin@<your_ap_ip>`
3. Check the logs: `tail -f /config/home-assistant.log | grep ha_zyxel`
4. Enable debug logging:
   ```yaml
   logger:
     logs:
       custom_components.ha_zyxel: debug
   ```

### Switches not working / "Socket is closed" errors

This was fixed in v1.1.0. Update to the latest version.

### No hostnames for clients

- Requires working reverse DNS on your network
- If reverse DNS fails, hostname will be `null`
- May slightly slow data collection (1s timeout per client)

### Radio switches take time to apply

- Changes take **30-120 seconds** to apply on the AP
- Switch state refreshes automatically after action
- Disabling a radio disables **all SSIDs** on that band

## ⚙️ Configuration Options

### Update Interval

Configurable during setup: 30-300 seconds (default: 60s)

- **Minimum**: 30 seconds (avoid AP overload)
- **Maximum**: 300 seconds (5 minutes)
- **Recommended**: 60 seconds (balance between responsiveness and load)

## 📝 Notes

- All configuration changes are saved to the AP (persistent across reboots)
- Radio control affects all SSIDs on that band
- Guest SSID switch only affects schedule activation
- Hostname lookup may add 1-2 seconds to data collection

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 👏 Credits

- Original inspiration from [ha-zyxel](https://github.com/zulufoxtrot/ha-zyxel) (for NR7101 router)
- Optimized for NWA50AX standalone mode

## 📞 Support

If you encounter issues:
1. Check the [Troubleshooting](#-troubleshooting) section
2. Enable debug logging
3. Open an issue with logs and details

## 🔄 Changelog

### Version 1.1.0 (2026-02-04)

**✨ New Features**:
- Client hostnames via reverse DNS
- Configurable update interval (30-300s)
- Radio 2.4GHz control switch
- Radio 5GHz control switch
- Last Seen timestamp sensor
- Formatted uptime display (Xd Xh Xm)

**🔧 Fixes**:
- Fixed "Socket is closed" errors with batch command execution
- Stable switch operations for radios and Guest SSID

**📦 Entities**:
- 18 sensors (including new Last Seen)
- 3 switches (Guest SSID + 2 radios)
- 1 button
- Total: 20 entities

### Version 1.0.2 (2026-02-02)
- Increased data fetch timeout from 30s to 50s
- Fixed entities not appearing due to timeout

### Version 1.0.1 (2026-02-02)
- Fixed SSH timeout issues using paramiko
- Switched from asyncssh to paramiko for better compatibility

### Version 1.0.0 (2026-02-02)
- Initial release
- 17 sensors for comprehensive monitoring
- Guest SSID control switch
- Reboot button
- Optimized for NWA50AX V7.10(ABYW.3)
