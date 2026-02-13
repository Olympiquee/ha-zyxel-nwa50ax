"""API client for Zyxel NWA50AX via SSH - Optimized for V7.10(ABYW.3)."""
import re
import time
import socket
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
        self._ssh_queue: asyncio.PriorityQueue[tuple[int, int, str, Any, asyncio.Future]] = asyncio.PriorityQueue()
        self._queue_counter = 0
        self._queue_task: Optional[asyncio.Task] = None
        self._refresh_coalesce_lock = asyncio.Lock()
        self._pending_refresh_task: Optional[asyncio.Task] = None
        
        if not HAS_PARAMIKO:
            raise ImportError(
                "paramiko is not installed. "
                "Install it with: pip install paramiko"
            )

    async def _ensure_queue_worker(self) -> None:
        """Ensure the SSH queue worker is running."""
        if self._queue_task is None or self._queue_task.done():
            self._queue_task = asyncio.create_task(self._process_ssh_queue())

    async def _process_ssh_queue(self) -> None:
        """Process SSH operations sequentially by priority."""
        while True:
            priority, _, operation_name, operation_coro, future = await self._ssh_queue.get()
            try:
                if not future.cancelled():
                    _LOGGER.debug("Executing SSH operation '%s' (priority=%d)", operation_name, priority)
                    result = await operation_coro()
                    future.set_result(result)
            except Exception as err:
                if not future.cancelled():
                    future.set_exception(err)
                _LOGGER.error("SSH queue operation '%s' failed: %s", operation_name, err)
            finally:
                self._ssh_queue.task_done()

    async def _queue_ssh_operation(self, priority: int, operation_name: str, operation_coro: Any) -> Any:
        """Queue an SSH operation and await its result."""
        await self._ensure_queue_worker()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._queue_counter += 1
        await self._ssh_queue.put((priority, self._queue_counter, operation_name, operation_coro, future))
        return await future

    async def async_shutdown(self) -> None:
        """Shutdown queue worker cleanly."""
        if self._queue_task and not self._queue_task.done():
            self._queue_task.cancel()
            try:
                await self._queue_task
            except asyncio.CancelledError:
                pass

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
        await self.async_shutdown()

    async def _async_execute_command_direct(self, command: str) -> Optional[str]:
        """Execute a command directly, without queueing."""
        return await asyncio.get_event_loop().run_in_executor(
            None, self._execute_command_sync, command
        )

    async def async_execute_command(self, command: str) -> Optional[str]:
        """Execute a command on the device."""
        try:
            return await self._queue_ssh_operation(
                20,
                f"command:{command}",
                lambda: self._async_execute_command_direct(command),
            )
        except Exception as err:
            _LOGGER.error("Error executing command '%s': %s", command, err)
            return None

    async def _async_execute_command_batch_direct(self, commands: list[str]) -> bool:
        """Execute a command batch directly, without queueing."""
        return await asyncio.get_event_loop().run_in_executor(
            None, self._execute_command_batch_sync, commands
        )

    def _execute_command_sync(self, command: str, keep_connection: bool = False) -> Optional[str]:
        """Execute command synchronously with paramiko using interactive shell.
        
        Args:
            command: Command to execute
            keep_connection: If True, reuse existing connection if available
        """
        ssh = None
        shell = None
        
        try:
            # Connexion SSH
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            ssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10,
                look_for_keys=False,
                allow_agent=False
            )
            
@@ -241,122 +299,152 @@ class ZyxelSSHAPI:
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

    async def _async_get_data_direct(self) -> dict[str, Any]:
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
            version_output = await self._async_execute_command_direct("show version")
            if version_output:
                data["device_info"] = self._parse_version(version_output)
            else:
                _LOGGER.warning("No output from 'show version'")
            
            # 2. Uptime (show system uptime)
            _LOGGER.debug("Fetching uptime...")
            uptime_output = await self._async_execute_command_direct("show system uptime")
            if uptime_output:
                data["status"]["uptime"] = self._parse_uptime(uptime_output)
            
            # 3. CPU (show cpu all)
            _LOGGER.debug("Fetching CPU...")
            cpu_output = await self._async_execute_command_direct("show cpu all")
            if cpu_output:
                data["status"]["cpu"] = self._parse_cpu(cpu_output)
            
            # 4. Mémoire (show mem status)
            _LOGGER.debug("Fetching memory...")
            mem_output = await self._async_execute_command_direct("show mem status")
            if mem_output:
                data["status"]["memory"] = self._parse_memory(mem_output)
            
            # 5. Clients WiFi (show wireless-hal station info)
            _LOGGER.debug("Fetching WiFi clients...")
            clients_output = await self._async_execute_command_direct("show wireless-hal station info")
            if clients_output:
                data["clients"] = self._parse_clients(clients_output)
            
            # 6. Interfaces (show interface all)
            _LOGGER.debug("Fetching interfaces...")
            interface_output = await self._async_execute_command_direct("show interface all")
            if interface_output:
                data["network"] = self._parse_interfaces(interface_output)
            
            # 7. Info WLAN (show wlan all)
            _LOGGER.debug("Fetching WLAN info...")
            wlan_output = await self._async_execute_command_direct("show wlan all")
            if wlan_output:
                data["radio"] = self._parse_wlan(wlan_output)
            
            # 8. Port status (show port status)
            _LOGGER.debug("Fetching port status...")
            port_output = await self._async_execute_command_direct("show port status")
            if port_output:
                data["network"]["port"] = self._parse_port_status(port_output)
            
            _LOGGER.info("Successfully fetched all data from NWA50AX")
            _LOGGER.debug("Data summary: %d clients, CPU: %s%%, Memory: %s%%", 
                         len(data.get("clients", [])),
                         data.get("status", {}).get("cpu", {}).get("current", "N/A"),
                         data.get("status", {}).get("memory", "N/A"))
                
        except Exception as err:
            _LOGGER.error("Error fetching device data: %s", err)
        
        return data

    async def async_get_data(self) -> dict[str, Any]:
        """Get all device data via prioritized SSH queue."""
        try:
            async with self._refresh_coalesce_lock:
                if self._pending_refresh_task and not self._pending_refresh_task.done():
                    task = self._pending_refresh_task
                else:
                    task = asyncio.create_task(
                        self._queue_ssh_operation(
                            30,
                            "refresh:all_data",
                            self._async_get_data_direct,
                        )
                    )
                    self._pending_refresh_task = task

            return await task
        except Exception as err:
            _LOGGER.error("Error queueing device data fetch: %s", err)
            return {
                "device_info": {},
                "status": {},
                "clients": [],
                "network": {},
                "radio": {},
            }
        finally:
            if self._pending_refresh_task and self._pending_refresh_task.done():
                self._pending_refresh_task = None

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
@@ -560,105 +648,152 @@ class ZyxelSSHAPI:
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
            result = await self._queue_ssh_operation(
                0,
                "action:reboot",
                lambda: self._async_execute_command_direct("reboot"),
            )
            if result is not None:
                _LOGGER.info("Reboot command sent")
                return True
            return False
        except Exception as err:
            _LOGGER.error("Error rebooting device: %s", err)
            return False

    async def async_get_radio_state(self, slot: int) -> Optional[bool]:
        """Get radio activation state.

        Returns True if active, False if inactive, None if undetermined.
        """
        try:
            wlan_output = await self._queue_ssh_operation(
                10,
                f"radio:state:slot{slot}",
                lambda: self._async_execute_command_direct("show wlan all"),
            )
            if not wlan_output:
                return None

            slot_pattern = rf"slot: slot{slot}.*?Activate: (\w+)"
            match = re.search(slot_pattern, wlan_output, re.DOTALL)

            if match:
                return match.group(1).lower() == "yes"
            return None
        except Exception as err:
            _LOGGER.error("Error getting radio state for slot %d: %s", slot, err)
            return None

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
            
            # Exécuter toutes les commandes dans une seule session
            success = await self._queue_ssh_operation(
                5,
                "action:guest_ssid",
                lambda: self._async_execute_command_batch_direct(commands),
            )
            
            if success:
                _LOGGER.info("Guest SSID schedule %s", "disabled (always on)" if enable else "enabled")
            
            return success
            
        except Exception as err:
            _LOGGER.error("Error toggling guest SSID: %s", err)
            return False

    async def async_toggle_radio(self, slot: int, enable: bool) -> bool:
        """Enable or disable radio with retry and state verification."""
        try:
            profile = "default" if slot == 1 else "default2"
            action = "activate" if enable else "deactivate"
            
            if enable:
                commands = [
                    "configure terminal",
                    f"wlan-radio-profile {profile}",
                    "activate",
                    "exit",
                    "write",
                ]
            else:
                commands = [
                    "configure terminal",
                    f"wlan-radio-profile {profile}",
                    "no activate",
                    "exit",
                    "write",
                ]
            
            for attempt in range(1, 3):
                _LOGGER.info("Radio slot %d: sending %s command (attempt %d/2)", slot, action, attempt)
                success = await self._queue_ssh_operation(
                    0,
                    f"action:radio:slot{slot}:attempt{attempt}",
                    lambda cmds=commands: self._async_execute_command_batch_direct(cmds),
                )

                if not success:
                    _LOGGER.error("Failed to execute radio command for slot %d", slot)
                    return False

                for delay in (60, 30):
                    await asyncio.sleep(delay)
                    current_state = await self.async_get_radio_state(slot)
                    if current_state is None:
                        _LOGGER.warning("Radio slot %d state undetermined after %ds", slot, delay)
                        continue
                    if current_state == enable:
                        _LOGGER.info("Radio slot %d successfully %sd", slot, action)
                        return True

                if attempt == 1:
                    _LOGGER.warning("Radio slot %d not in expected state, retrying command", slot)

            _LOGGER.error("Radio slot %d failed to reach desired state after all attempts", slot)
            return False
            
        except Exception as err:
            _LOGGER.error("Error toggling radio slot %d: %s", slot, err)
            return False
