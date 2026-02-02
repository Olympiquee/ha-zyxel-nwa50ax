"""API client for Zyxel NWA50AX via SSH - Optimized for V7.10(ABYW.3)."""
import logging
import asyncio
import re
import time
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

# Importer paramiko uniquement (plus stable avec NWA50AX)
try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False
    _LOGGER.error("paramiko not installed. Please install: pip install paramiko")


class ZyxelSSHAPI:
    """Class to communicate with Zyxel NWA50AX via SSH."""

    def __init__(self, host: str, username: str, password: str, port: int = 22) -> None:
        """Initialize the API."""
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        
        if not HAS_PARAMIKO:
            raise ImportError(
                "paramiko is not installed. "
                "Install it with: pip install paramiko"
            )

    async def async_connect(self) -> bool:
        """Test SSH connection to the device."""
        try:
            # Test simple de connexion
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._test_connection
            )
            if result:
                _LOGGER.info("Successfully tested SSH connection to %s", self.host)
            return result
        except Exception as err:
            _LOGGER.error("SSH connection test failed: %s", err)
            return False

    def _test_connection(self) -> bool:
        """Test SSH connection synchronously."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            ssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10,
                look_for_keys=False,
                allow_agent=False
            )
            ssh.close()
            return True
        except Exception as err:
            _LOGGER.error("Connection test failed: %s", err)
            return False

    async def async_disconnect(self) -> None:
        """Disconnect - not needed with paramiko (connections are per-command)."""
        pass

    async def async_execute_command(self, command: str) -> Optional[str]:
        """Execute a command on the device."""
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._execute_command_sync, command
            )
        except Exception as err:
            _LOGGER.error("Error executing command '%s': %s", command, err)
            return None

    def _execute_command_sync(self, command: str) -> Optional[str]:
        """Execute command synchronously with paramiko using interactive shell."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Connexion SSH
            ssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10,
                look_for_keys=False,
                allow_agent=False
            )
            
            # Créer un shell interactif
            shell = ssh.invoke_shell()
            time.sleep(1)  # Attendre le prompt initial
            
            # Vider le buffer initial (prompt, message de bienvenue, etc.)
            if shell.recv_ready():
                initial_output = shell.recv(8192).decode('utf-8', errors='ignore')
                _LOGGER.debug("Initial prompt: %s", initial_output[:200])
            
            # Envoyer la commande
            shell.send(command + '\n')
            time.sleep(2)  # Attendre que la commande s'exécute
            
            # Lire la sortie
            output = ""
            max_attempts = 15
            attempts = 0
            
            while attempts < max_attempts:
                if shell.recv_ready():
                    chunk = shell.recv(8192).decode('utf-8', errors='ignore')
                    output += chunk
                    time.sleep(0.2)  # Petite pause pour laisser le buffer se remplir
                else:
                    if output and len(output) > 50:  # On a reçu suffisamment de données
                        break
                    time.sleep(0.3)
                    attempts += 1
            
            # Fermer le shell
            shell.send('exit\n')
            time.sleep(0.5)
            shell.close()
            ssh.close()
            
            # Nettoyer la sortie
            # Enlever l'écho de la commande et les prompts
            clean_output = self._clean_output(output, command)
            
            _LOGGER.debug("Command '%s' returned %d characters", command, len(clean_output))
            
            return clean_output
            
        except Exception as err:
            _LOGGER.error("Paramiko command '%s' failed: %s", command, err)
            if ssh:
                try:
                    ssh.close()
                except:
                    pass
            return None

    def _clean_output(self, output: str, command: str) -> str:
        """Clean command output by removing prompts and echoed command."""
        if not output:
            return ""
        
        lines = output.split('\n')
        clean_lines = []
        
        for line in lines:
            # Ignorer les lignes qui contiennent des prompts
            if any(prompt in line for prompt in ['Router(config)#', 'Router#', 'Router>']):
                continue
            # Ignorer l'écho de la commande
            if line.strip() == command.strip():
                continue
            # Ignorer les lignes vides au début
            if not clean_lines and not line.strip():
                continue
            
            clean_lines.append(line)
        
        # Enlever les lignes vides à la fin
        while clean_lines and not clean_lines[-1].strip():
            clean_lines.pop()
        
        result = '\n'.join(clean_lines)
        return result.strip()

    async def async_get_data(self) -> dict[str, Any]:
        """Get all device data using validated commands."""
        data = {
            "device_info": {},
            "status": {},
            "clients": [],
            "network": {},
            "radio": {},
        }
        
        try:
            # 1. Version et modèle (show version)
            _LOGGER.debug("Fetching version...")
            version_output = await self.async_execute_command("show version")
            if version_output:
                data["device_info"] = self._parse_version(version_output)
            else:
                _LOGGER.warning("No output from 'show version'")
            
            # 2. Uptime (show system uptime)
            _LOGGER.debug("Fetching uptime...")
            uptime_output = await self.async_execute_command("show system uptime")
            if uptime_output:
                data["status"]["uptime"] = self._parse_uptime(uptime_output)
            
            # 3. CPU (show cpu all)
            _LOGGER.debug("Fetching CPU...")
            cpu_output = await self.async_execute_command("show cpu all")
            if cpu_output:
                data["status"]["cpu"] = self._parse_cpu(cpu_output)
            
            # 4. Mémoire (show mem status)
            _LOGGER.debug("Fetching memory...")
            mem_output = await self.async_execute_command("show mem status")
            if mem_output:
                data["status"]["memory"] = self._parse_memory(mem_output)
            
            # 5. Clients WiFi (show wireless-hal station info)
            _LOGGER.debug("Fetching WiFi clients...")
            clients_output = await self.async_execute_command("show wireless-hal station info")
            if clients_output:
                data["clients"] = self._parse_clients(clients_output)
            
            # 6. Interfaces (show interface all)
            _LOGGER.debug("Fetching interfaces...")
            interface_output = await self.async_execute_command("show interface all")
            if interface_output:
                data["network"] = self._parse_interfaces(interface_output)
            
            # 7. Info WLAN (show wlan all)
            _LOGGER.debug("Fetching WLAN info...")
            wlan_output = await self.async_execute_command("show wlan all")
            if wlan_output:
                data["radio"] = self._parse_wlan(wlan_output)
            
            # 8. Port status (show port status)
            _LOGGER.debug("Fetching port status...")
            port_output = await self.async_execute_command("show port status")
            if port_output:
                data["network"]["port"] = self._parse_port_status(port_output)
            
            _LOGGER.info("Successfully fetched all data from NWA50AX")
                
        except Exception as err:
            _LOGGER.error("Error fetching device data: %s", err)
        
        return data

    def _parse_version(self, output: str) -> dict[str, Any]:
        """Parse 'show version' output."""
        info = {
            "model": "Unknown",
            "firmware": "Unknown",
            "build_date": "Unknown",
        }
        
        model_match = re.search(r'model\s*:\s*(.+)', output)
        if model_match:
            info["model"] = model_match.group(1).strip()
        
        firmware_match = re.search(r'firmware version\s*:\s*(.+)', output)
        if firmware_match:
            info["firmware"] = firmware_match.group(1).strip()
        
        build_match = re.search(r'build date\s*:\s*(.+)', output)
        if build_match:
            info["build_date"] = build_match.group(1).strip()
        
        return info

    def _parse_uptime(self, output: str) -> int:
        """Parse 'show system uptime' output. Returns uptime in seconds."""
        uptime_seconds = 0
        
        # Format: X days HH:MM:SS
        match = re.search(r'(\d+)\s+days?\s+(\d+):(\d+):(\d+)', output)
        if match:
            days = int(match.group(1))
            hours = int(match.group(2))
            minutes = int(match.group(3))
            seconds = int(match.group(4))
            uptime_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
        else:
            # Format alternatif: HH:MM:SS
            match = re.search(r'(\d+):(\d+):(\d+)', output)
            if match:
                hours = int(match.group(1))
                minutes = int(match.group(2))
                seconds = int(match.group(3))
                uptime_seconds = hours * 3600 + minutes * 60 + seconds
        
        return uptime_seconds

    def _parse_cpu(self, output: str) -> dict[str, Any]:
        """Parse 'show cpu all' output."""
        cpu_data = {
            "current": 0,
            "avg_1min": 0,
            "avg_5min": 0,
            "cores": [],
        }
        
        core_pattern = r'CPU core (\d+) utilization:\s*(\d+)\s*%'
        core_1min_pattern = r'CPU core (\d+) utilization for 1 min:\s*(\d+)\s*%'
        core_5min_pattern = r'CPU core (\d+) utilization for 5 min:\s*(\d+)\s*%'
        
        cores_current = re.findall(core_pattern, output)
        cores_1min = re.findall(core_1min_pattern, output)
        cores_5min = re.findall(core_5min_pattern, output)
        
        if cores_current:
            cpu_data["current"] = sum(int(c[1]) for c in cores_current) // len(cores_current)
            cpu_data["cores"] = [int(c[1]) for c in cores_current]
        
        if cores_1min:
            cpu_data["avg_1min"] = sum(int(c[1]) for c in cores_1min) // len(cores_1min)
        
        if cores_5min:
            cpu_data["avg_5min"] = sum(int(c[1]) for c in cores_5min) // len(cores_5min)
        
        return cpu_data

    def _parse_memory(self, output: str) -> int:
        """Parse 'show mem status' output. Returns percentage."""
        match = re.search(r'memory usage:\s*(\d+)\s*%', output)
        if match:
            return int(match.group(1))
        return 0

    def _parse_clients(self, output: str) -> list[dict[str, Any]]:
        """Parse 'show wireless-hal station info' output."""
        clients = []
        
        client_blocks = re.split(r'index:\s*\d+', output)
        
        for block in client_blocks[1:]:
            client = {}
            
            mac_match = re.search(r'MAC:\s*([\da-fA-F:]+)', block)
            if mac_match:
                client["mac"] = mac_match.group(1).upper()
            
            ip_match = re.search(r'IPv4:\s*([\d.]+)', block)
            if ip_match:
                client["ip"] = ip_match.group(1)
            
            ssid_match = re.search(r'Display SSID:\s*(.+)', block)
            if ssid_match:
                client["ssid"] = ssid_match.group(1).strip()
            elif re.search(r'SSID:\s*(.+)', block):
                client["ssid"] = re.search(r'SSID:\s*(.+)', block).group(1).strip()
            
            security_match = re.search(r'Security:\s*(.+)', block)
            if security_match:
                client["security"] = security_match.group(1).strip()
            
            rssi_dbm_match = re.search(r'RSSI dBm:\s*(-?\d+)', block)
            if rssi_dbm_match:
                client["rssi_dbm"] = int(rssi_dbm_match.group(1))
            
            rssi_match = re.search(r'RSSI:\s*(\d+)', block)
            if rssi_match:
                client["rssi_percent"] = int(rssi_match.group(1))
            
            band_match = re.search(r'Band:\s*([\dG.Hz]+)', block)
            if band_match:
                client["band"] = band_match.group(1)
            
            slot_match = re.search(r'Slot:\s*(\d+)', block)
            if slot_match:
                client["slot"] = int(slot_match.group(1))
            
            tx_match = re.search(r'TxRate:\s*(\d+)M', block)
            if tx_match:
                client["tx_rate"] = int(tx_match.group(1))
            
            rx_match = re.search(r'RxRate:\s*(\d+)M', block)
            if rx_match:
                client["rx_rate"] = int(rx_match.group(1))
            
            capability_match = re.search(r'Capability:\s*(.+)', block)
            if capability_match:
                client["capability"] = capability_match.group(1).strip()
            
            time_match = re.search(r'Time:\s*(.+)', block)
            if time_match:
                client["connected_since"] = time_match.group(1).strip()
            
            if client.get("mac"):
                clients.append(client)
        
        return clients

    def _parse_interfaces(self, output: str) -> dict[str, Any]:
        """Parse 'show interface all' output."""
        network = {
            "ip_address": "Unknown",
            "netmask": "Unknown",
            "interfaces": [],
        }
        
        lan_match = re.search(r'lan\s+Up\s+([\d.]+)\s+([\d.]+)', output)
        if lan_match:
            network["ip_address"] = lan_match.group(1)
            network["netmask"] = lan_match.group(2)
        
        interface_lines = re.findall(r'(\d+)\s+(\S+)\s+(Up|Down|n/a)\s+([\d.]+|n/a)', output)
        for iface in interface_lines:
            network["interfaces"].append({
                "name": iface[1],
                "status": iface[2],
                "ip": iface[3] if iface[3] != "n/a" else None,
            })
        
        return network

    def _parse_wlan(self, output: str) -> dict[str, Any]:
        """Parse 'show wlan all' output."""
        radio = {
            "slot1_active": False,
            "slot1_band": "Unknown",
            "slot1_ssids": [],
            "slot2_active": False,
            "slot2_band": "Unknown",
            "slot2_ssids": [],
        }
        
        slot1_match = re.search(r'slot: slot1.*?Activate: (\w+).*?Band: ([\dG.]+)', output, re.DOTALL)
        if slot1_match:
            radio["slot1_active"] = slot1_match.group(1).lower() == "yes"
            radio["slot1_band"] = slot1_match.group(2)
        
        slot1_block = re.search(r'slot: slot1(.*?)(?:slot: slot2|$)', output, re.DOTALL)
        if slot1_block:
            ssids = re.findall(r'SSID_profile_\d+:\s*(\S+)', slot1_block.group(1))
            radio["slot1_ssids"] = [s for s in ssids if s]
        
        slot2_match = re.search(r'slot: slot2.*?Activate: (\w+).*?Band: ([\dG.]+)', output, re.DOTALL)
        if slot2_match:
            radio["slot2_active"] = slot2_match.group(1).lower() == "yes"
            radio["slot2_band"] = slot2_match.group(2)
        
        slot2_block = re.search(r'slot: slot2(.*?)$', output, re.DOTALL)
        if slot2_block:
            ssids = re.findall(r'SSID_profile_\d+:\s*(\S+)', slot2_block.group(1))
            radio["slot2_ssids"] = [s for s in ssids if s]
        
        return radio

    def _parse_port_status(self, output: str) -> dict[str, Any]:
        """Parse 'show port status' output."""
        port = {
            "status": "Unknown",
            "speed": "Unknown",
            "tx_bytes": 0,
            "rx_bytes": 0,
            "tx_rate": 0,
            "rx_rate": 0,
            "uptime": "Unknown",
        }
        
        port_match = re.search(
            r'1\s+(\S+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)\s+(\d+)\s+([\d:]+)\s+\d+\s+(\d+)\s+(\d+)',
            output
        )
        
        if port_match:
            port["status"] = port_match.group(1)
            port["tx_rate"] = int(port_match.group(2))
            port["rx_rate"] = int(port_match.group(3))
            port["uptime"] = port_match.group(4)
            port["tx_bytes"] = int(port_match.group(5))
            port["rx_bytes"] = int(port_match.group(6))
            
            if "/" in port["status"]:
                port["speed"] = port["status"].split("/")[0]
        
        return port

    async def async_reboot(self) -> bool:
        """Reboot the device."""
        try:
            result = await self.async_execute_command("reboot")
            if result is not None:
                _LOGGER.info("Reboot command sent")
                return True
            return False
        except Exception as err:
            _LOGGER.error("Error rebooting device: %s", err)
            return False

    async def async_toggle_guest_ssid(self, enable: bool) -> bool:
        """Enable or disable Guest SSID schedule."""
        try:
            if enable:
                commands = [
                    "configure terminal",
                    "wlan-ssid-profile Guest",
                    "no ssid-schedule",
                    "exit",
                    "write",
                ]
            else:
                commands = [
                    "configure terminal",
                    "wlan-ssid-profile Guest",
                    "ssid-schedule",
                    "exit",
                    "write",
                ]
            
            for cmd in commands:
                await self.async_execute_command(cmd)
                await asyncio.sleep(0.5)
            
            _LOGGER.info("Guest SSID schedule %s", "disabled (always on)" if enable else "enabled")
            return True
            
        except Exception as err:
            _LOGGER.error("Error toggling guest SSID: %s", err)
            return False
