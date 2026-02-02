# Zyxel NWA50AX Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)

Home Assistant custom integration for **Zyxel NWA50AX** WiFi Access Point using SSH.

## ✨ Features

### 📊 17 Sensors
- **System**: Uptime, Firmware version
- **Performance**: CPU usage (current + 1min/5min avg), Memory usage
- **WiFi Clients**: Total clients, 2.4GHz clients, 5GHz clients (with detailed info: MAC, IP, SSID, RSSI, band)
- **Ethernet Port**: Status, TX/RX rates, Total bytes transferred
- **Radio**: 2.4GHz and 5GHz radio status with active SSIDs

### 🎛️ Controls
- **Switch**: Guest SSID on/off control
- **Button**: Reboot access point

### 📱 Detailed Client Information
Each client includes:
- MAC address
- IP address
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
- Python package: `asyncssh` or `paramiko`

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

### Option 2: HACS (Recommended)

1. Open HACS
2. Go to **Integrations**
3. Click the three dots in the top right corner
4. Select **Custom repositories**
5. Add this repository URL
6. Select **Integration** as category
7. Click **Install**
8. Restart Home Assistant
9. Follow steps 4-7 from Manual Installation above

## ⚙️ Configuration

### Enable SSH on NWA50AX

1. Log in to your NWA50AX web interface
2. Go to **Management** → **Services** (or **System** → **Remote Management**)
3. Enable **SSH**
4. Set port to `22`
5. Allow access from your local network
6. Save settings

### Guest SSID Control

The integration includes a switch to control your Guest SSID:

- **Switch ON**: Guest SSID is always active (ignores schedule)
- **Switch OFF**: Guest SSID follows the schedule configured in the web interface

## 📊 Example Dashboard

```yaml
type: vertical-stack
title: 🌐 Zyxel NWA50AX
cards:
  # Controls
  - type: entities
    title: Control
    entities:
      - entity: switch.zyxel_nwa50ax_guest_ssid
        name: Guest SSID
      - entity: button.zyxel_nwa50ax_reboot

  # Information
  - type: entities
    title: Information
    entities:
      - sensor.zyxel_nwa50ax_firmware
      - sensor.zyxel_nwa50ax_uptime
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

  # WiFi Clients
  - type: custom:mini-graph-card
    entities:
      - entity: sensor.zyxel_nwa50ax_connected_clients
        name: Total
      - entity: sensor.zyxel_nwa50ax_clients_2_4ghz
        name: 2.4GHz
      - entity: sensor.zyxel_nwa50ax_clients_5ghz
        name: 5GHz
    name: WiFi Clients
    hours_to_show: 24
```

## 🤖 Example Automations

### Disable Guest WiFi at night

```yaml
automation:
  - alias: "Disable Guest WiFi at night"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.zyxel_nwa50ax_guest_ssid

  - alias: "Enable Guest WiFi in the morning"
    trigger:
      - platform: time
        at: "07:00:00"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.zyxel_nwa50ax_guest_ssid
```

### Alert on high CPU

```yaml
automation:
  - alias: "Alert NWA50AX high CPU"
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

### Weekly reboot

```yaml
automation:
  - alias: "Weekly NWA50AX reboot"
    trigger:
      - platform: time
        at: "03:00:00"
    condition:
      - condition: time
        weekday: [sun]
    action:
      - service: button.press
        target:
          entity_id: button.zyxel_nwa50ax_reboot
```

## 🔧 SSH Commands Used

The integration uses the following validated SSH commands:

- `show version` - Model, firmware, build date
- `show system uptime` - Uptime since last reboot
- `show cpu all` - CPU usage per core + averages
- `show mem status` - Memory usage percentage
- `show wireless-hal station info` - Connected WiFi clients (detailed)
- `show interface all` - Network interfaces status
- `show wlan all` - Radio status and SSIDs
- `show port status` - Ethernet port statistics

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

### Guest SSID switch not working

1. Verify SSH access works
2. Test commands manually:
   ```bash
   ssh admin@<your_ap_ip>
   configure terminal
   wlan-ssid-profile Guest
   no ssid-schedule
   exit
   write
   ```
3. Wait 30-120 seconds for changes to apply

### No data from sensors

1. Check that all SSH commands work manually
2. Increase update interval in `const.py` if needed
3. Verify firmware version is compatible (V7.10 tested)

## ⚙️ Configuration Options

### Update Interval

Default: 60 seconds

To change, edit `const.py`:
```python
DEFAULT_SCAN_INTERVAL = 60  # seconds
```

Minimum recommended: 30 seconds (to avoid overloading the AP)

## 📝 Notes

- Changes to Guest SSID via switch may take 30-120 seconds to apply
- Switch state is local to Home Assistant - manual changes via web interface won't be detected
- All configuration changes are saved to the AP (persistent across reboots)

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 👏 Credits

- Original inspiration from [ha-zyxel](https://github.com/zulufoxtrot/ha-zyxel) (for NR7101 router)
- Optimized for NWA50AX standalone mode by the community

## 📞 Support

If you encounter issues:
1. Check the [Troubleshooting](#-troubleshooting) section
2. Enable debug logging
3. Open an issue with logs and details

## 🔄 Changelog

### Version 1.0.0 (2026-02-02)
- Initial release
- 17 sensors for comprehensive monitoring
- Guest SSID control switch
- Reboot button
- Optimized for NWA50AX V7.10(ABYW.3)
- SSH-based communication using asyncssh or paramiko
