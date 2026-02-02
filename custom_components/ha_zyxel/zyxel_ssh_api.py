"""API client for Zyxel NWA50AX via SSH - Optimized for V7.10(ABYW.3)."""
import logging
import asyncio
import re
import time
from typing import Any, Optional
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

# Essayez d'importer asyncssh, sinon fallback sur paramiko
try:
    import asyncssh
    HAS_ASYNCSSH = True
except ImportError:
    HAS_ASYNCSSH = False
    try:
        import paramiko
        HAS_PARAMIKO = True
    except ImportError:
        HAS_PARAMIKO = False


class ZyxelSSHAPI:
    """Class to communicate with Zyxel NWA50AX via SSH."""

    def __init__(self, host: str, username: str, password: str, port: int = 22) -> None:
        """Initialize the API."""
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self._conn = None
        
        if not HAS_ASYNCSSH and not HAS_PARAMIKO:
            raise ImportError(
                "Ni asyncssh ni paramiko n'est installé. "
                "Installez l'un d'eux : pip install asyncssh ou pip install paramiko"
            )

    async def async_connect(self) -> bool:
        """Connect to the device via SSH."""
        try:
            if HAS_ASYNCSSH:
                # Utiliser asyncssh (préféré - asynchrone)
                self._conn = await asyncio.wait_for(
                    asyncssh.connect(
                        self.host,
                        port=self.port,
                        username=self.username,
                        password=self.password,
                        known_hosts=None,  # Accepter tous les hosts
                    ),
                    timeout=10.0
                )
                _LOGGER.info("Successfully connected via SSH (asyncssh)")
            else:
                # Fallback sur paramiko (synchrone, moins optimal)
                _LOGGER.info("Using paramiko (sync mode)")
                
            return True
            
        except Exception as err:
            _LOGGER.error("SSH connection failed: %s", err)
            return False

    async def async_disconnect(self) -> None:
        """Disconnect from the device."""
        if self._conn and HAS_ASYNCSSH:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None

    async def async_execute_command(self, command: str) -> Optional[str]:
        """Execute a command on the device."""
        try:
            if HAS_ASYNCSSH and self._conn:
                # Mode asynchrone avec channel interactif pour éviter les timeouts
                return await self._execute_interactive_asyncssh(command)
            elif HAS_PARAMIKO:
                # Mode synchrone
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._execute_command_sync, command
                )
            else:
                _LOGGER.error("No SSH library available")
                return None
                
        except Exception as err:
            _LOGGER.error("Error executing command '%s': %s", command, err)
            return None

    async def _execute_interactive_asyncssh(self, command: str) -> Optional[str]:
        """Execute command using interactive channel (fixes timeout issues)."""
        try:
            # Créer un channel interactif
            process = await self._conn.create_process()
            
            # Envoyer la commande
            process.stdin.write(command + '\n')
            await process.stdin.drain()
            
            # Lire la sortie avec timeout
            output = ""
            try:
                # Lire avec timeout de 10 secondes
                output = await asyncio.wait_for(
                    process.stdout.read(),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                # Si timeout, récupérer ce qu'on a déjà
                _LOGGER.debug("Command '%s' timed out, using partial output", command)
            
            # Fermer proprement
            process.stdin.write('exit\n')
            await process.stdin.drain()
            process.close()
            await process.wait_closed()
            
            return output
            
        except Exception as err:
            _LOGGER.error("Interactive command failed: %s", err)
            return None

    def _execute_command_sync(self, command: str) -> Optional[str]:
        """Execute command synchronously with paramiko using shell."""
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
            
            # Utiliser un shell interactif au lieu d'exec_command
            shell = ssh.invoke_shell()
            time.sleep(1)  # Attendre le prompt
            
            # Vider le buffer initial
            if shell.recv_ready():
                shell.recv(4096)
            
            # Envoyer la commande
            shell.send(command + '\n')
            time.sleep(2)  # Attendre la réponse
            
            # Lire la sortie
            output = ""
            max_attempts = 10
            attempts = 0
            
            while attempts < max_attempts:
                if shell.recv_ready():
                    chunk = shell.recv(4096).decode('utf-8', errors='ignore')
                    output += chunk
                    time.sleep(0.1)
                else:
                    if output:  # On a reçu quelque chose, on peut sortir
                        break
                    time.sleep(0.5)
                    attempts += 1
            
            # Fermer le shell
            shell.send('exit\n')
            shell.close()
            ssh.close()
            
            # Nettoyer la sortie (enlever la commande echo et le prompt)
            lines = output.split('\n')
            # Enlever la première ligne (echo de la commande) et les prompts
            clean_lines = [l for l in lines[1:] if not l.strip().startswith('Router')]
            
            return '\n'.join(clean_lines).strip()
            
        except Exception as err:
            _LOGGER.error("Paramiko command failed: %s", err)
            if ssh:
                ssh.close()
            return None

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
            version_output = await self.async_execute_command("show version")
            if version_output:
                data["device_info"] = self._parse_version(version_output)
            
            # 2. Uptime (show system uptime)
            uptime_output = await self.async_execute_command("show system uptime")
            if uptime_output:
                data["status"]["uptime"] = self._parse_uptime(uptime_output)
            
            # 3. CPU (show cpu all)
            cpu_output = await self.async_execute_command("show cpu all")
            if cpu_output:
                data["status"]["cpu"] = self._parse_cpu(cpu_output)
            
            # 4. Mémoire (show mem status)
            mem_output = await self.async_execute_command("show mem status")
            if mem_output:
                data["status"]["memory"] = self._parse_memory(mem_output)
            
            # 5. Clients WiFi (show wireless-hal station info) - LA PLUS IMPORTANTE
            clients_output = await self.async_execute_command("show wireless-hal station info")
            if clients_output:
                data["clients"] = self._parse_clients(clients_output)
            
            # 6. Interfaces (show interface all)
            interface_output = await self.async_execute_command("show interface all")
            if interface_output:
                data["network"] = self._parse_interfaces(interface_output)
            
            # 7. Info WLAN (show wlan all)
            wlan_output = await self.async_execute_command("show wlan all")
            if wlan_output:
                data["radio"] = self._parse_wlan(wlan_output)
            
            # 8. Port status (show port status)
            port_output = await self.async_execute_command("show port status")
            if port_output:
                data["network"]["port"] = self._parse_port_status(port_output)
                
        except Exception as err:
            _LOGGER.error("Error fetching device data: %s", err)
        
        return data

    def _parse_version(self, output: str) -> dict[str, Any]:
        """Parse 'show version' output.
        
        Example:
        model           : NWA50AX
        firmware version: V7.10(ABYW.3)
        build date      : 2025-06-29 01:00:28
        """
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
        """Parse 'show system uptime' output.
        
        Example: system uptime: 1 days 05:34:40
        Returns: uptime in seconds
        """
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
            # Format alternatif: HH:MM:SS (sans jours)
            match = re.search(r'(\d+):(\d+):(\d+)', output)
            if match:
                hours = int(match.group(1))
                minutes = int(match.group(2))
                seconds = int(match.group(3))
                uptime_seconds = hours * 3600 + minutes * 60 + seconds
        
        return uptime_seconds

    def _parse_cpu(self, output: str) -> dict[str, Any]:
        """Parse 'show cpu all' output.
        
        Example:
        CPU core 0 utilization: 5 %
        CPU core 0 utilization for 1 min: 3 %
        CPU core 0 utilization for 5 min: 3 %
        """
        cpu_data = {
            "current": 0,
            "avg_1min": 0,
            "avg_5min": 0,
            "cores": [],
        }
        
        # Extraire les valeurs de chaque core
        core_pattern = r'CPU core (\d+) utilization:\s*(\d+)\s*%'
        core_1min_pattern = r'CPU core (\d+) utilization for 1 min:\s*(\d+)\s*%'
        core_5min_pattern = r'CPU core (\d+) utilization for 5 min:\s*(\d+)\s*%'
        
        cores_current = re.findall(core_pattern, output)
        cores_1min = re.findall(core_1min_pattern, output)
        cores_5min = re.findall(core_5min_pattern, output)
        
        # Calculer la moyenne de tous les cores
        if cores_current:
            cpu_data["current"] = sum(int(c[1]) for c in cores_current) // len(cores_current)
            cpu_data["cores"] = [int(c[1]) for c in cores_current]
        
        if cores_1min:
            cpu_data["avg_1min"] = sum(int(c[1]) for c in cores_1min) // len(cores_1min)
        
        if cores_5min:
            cpu_data["avg_5min"] = sum(int(c[1]) for c in cores_5min) // len(cores_5min)
        
        return cpu_data

    def _parse_memory(self, output: str) -> int:
        """Parse 'show mem status' output.
        
        Example: memory usage: 53%
        Returns: percentage as integer
        """
        match = re.search(r'memory usage:\s*(\d+)\s*%', output)
        if match:
            return int(match.group(1))
        return 0

    def _parse_clients(self, output: str) -> list[dict[str, Any]]:
        """Parse 'show wireless-hal station info' output.
        
        Example:
        index: 1
          MAC: a4:e5:7c:a3:38:8a
          IPv4: 10.0.30.248
          Slot: 1
          SSID: 6fer_IoT
          Security: WPA2-PSK
          TxRate: 72M
          RxRate: 54M
          RSSI: 98
          RSSI dBm: -51
          Time: 06:32:31 2026/01/30
          VapIdx: 3
          Capability: 802.11b/g/n
          DOT11 features: N/A
          Display SSID: 6fer_IoT
          Band: 2.4GHz
        """
        clients = []
        
        # Découper par "index:"
        client_blocks = re.split(r'index:\s*\d+', output)
        
        for block in client_blocks[1:]:  # Skip le premier qui est vide
            client = {}
            
            # Extraire chaque champ
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
            
            if client.get("mac"):  # On ajoute seulement si on a au moins le MAC
                clients.append(client)
        
        return clients

    def _parse_interfaces(self, output: str) -> dict[str, Any]:
        """Parse 'show interface all' output.
        
        Example:
        No. Name            Status              IP Address      Mask            IP Assignment
        ===============================================================================
        2   lan             Up                  10.0.20.2       255.255.255.0   DHCP client
        """
        network = {
            "ip_address": "Unknown",
            "netmask": "Unknown",
            "interfaces": [],
        }
        
        # Chercher l'interface 'lan' principale
        lan_match = re.search(r'lan\s+Up\s+([\d.]+)\s+([\d.]+)', output)
        if lan_match:
            network["ip_address"] = lan_match.group(1)
            network["netmask"] = lan_match.group(2)
        
        # Lister toutes les interfaces
        interface_lines = re.findall(r'(\d+)\s+(\S+)\s+(Up|Down|n/a)\s+([\d.]+|n/a)', output)
        for iface in interface_lines:
            network["interfaces"].append({
                "name": iface[1],
                "status": iface[2],
                "ip": iface[3] if iface[3] != "n/a" else None,
            })
        
        return network

    def _parse_wlan(self, output: str) -> dict[str, Any]:
        """Parse 'show wlan all' output.
        
        Example:
        slot: slot1
         Role: ap
         Band: 2.4G
         SSID_profile_1: Home
         Activate: yes
        """
        radio = {
            "slot1_active": False,
            "slot1_band": "Unknown",
            "slot1_ssids": [],
            "slot2_active": False,
            "slot2_band": "Unknown",
            "slot2_ssids": [],
        }
        
        # Slot 1 (2.4GHz)
        slot1_match = re.search(r'slot: slot1.*?Activate: (\w+).*?Band: ([\dG.]+)', output, re.DOTALL)
        if slot1_match:
            radio["slot1_active"] = slot1_match.group(1).lower() == "yes"
            radio["slot1_band"] = slot1_match.group(2)
        
        # SSIDs du slot1
        slot1_block = re.search(r'slot: slot1(.*?)(?:slot: slot2|$)', output, re.DOTALL)
        if slot1_block:
            ssids = re.findall(r'SSID_profile_\d+:\s*(\S+)', slot1_block.group(1))
            radio["slot1_ssids"] = [s for s in ssids if s]
        
        # Slot 2 (5GHz)
        slot2_match = re.search(r'slot: slot2.*?Activate: (\w+).*?Band: ([\dG.]+)', output, re.DOTALL)
        if slot2_match:
            radio["slot2_active"] = slot2_match.group(1).lower() == "yes"
            radio["slot2_band"] = slot2_match.group(2)
        
        # SSIDs du slot2
        slot2_block = re.search(r'slot: slot2(.*?)$', output, re.DOTALL)
        if slot2_block:
            ssids = re.findall(r'SSID_profile_\d+:\s*(\S+)', slot2_block.group(1))
            radio["slot2_ssids"] = [s for s in ssids if s]
        
        return radio

    def _parse_port_status(self, output: str) -> dict[str, Any]:
        """Parse 'show port status' output.
        
        Example:
        Port Status       TxPkts     RxPkts     TxBcast    RxBcast    Colli.  TxB/s      RxB/s      Up Time      PVID       TxBytes              RxBytes
        1    1000M/Full   2937780    5799031    3176       139355     0       8616       15312      29:33:11     20         796587774            5569274515
        """
        port = {
            "status": "Unknown",
            "speed": "Unknown",
            "tx_bytes": 0,
            "rx_bytes": 0,
            "tx_rate": 0,
            "rx_rate": 0,
            "uptime": "Unknown",
        }
        
        # Extraire la ligne de données du port 1
        port_match = re.search(
            r'1\s+(\S+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)\s+(\d+)\s+([\d:]+)\s+\d+\s+(\d+)\s+(\d+)',
            output
        )
        
        if port_match:
            port["status"] = port_match.group(1)
            port["tx_rate"] = int(port_match.group(2))  # B/s
            port["rx_rate"] = int(port_match.group(3))  # B/s
            port["uptime"] = port_match.group(4)
            port["tx_bytes"] = int(port_match.group(5))
            port["rx_bytes"] = int(port_match.group(6))
            
            # Extraire la vitesse (1000M/Full -> 1000M)
            if "/" in port["status"]:
                port["speed"] = port["status"].split("/")[0]
        
        return port

    async def async_reboot(self) -> bool:
        """Reboot the device."""
        try:
            # La commande reboot sur Zyxel
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
                # Désactiver le planning (SSID toujours actif)
                commands = [
                    "configure terminal",
                    "wlan-ssid-profile Guest",
                    "no ssid-schedule",
                    "exit",
                    "write",
                ]
            else:
                # Activer le planning (SSID suit le planning configuré)
                commands = [
                    "configure terminal",
                    "wlan-ssid-profile Guest",
                    "ssid-schedule",
                    "exit",
                    "write",
                ]
            
            for cmd in commands:
                await self.async_execute_command(cmd)
                await asyncio.sleep(0.5)  # Petite pause entre commandes
            
            _LOGGER.info("Guest SSID schedule %s", "disabled (always on)" if enable else "enabled")
            return True
            
        except Exception as err:
            _LOGGER.error("Error toggling guest SSID: %s", err)
            return False
