"""API client for Zyxel NWA50AX via SSH - Optimized for V7.10(ABYW.3).

Architecture (v2) :
- Toutes les lectures d'un même "groupe" (fast/slow/daily) sont regroupées dans
  UNE SEULE session SSH (`_execute_session_sync`), au lieu d'une connexion par
  commande. C'est le changement le plus impactant en performance.
- Les actions d'écriture rapides (désactivation radio, schedule SSID) envoient
  leur commande de config ET relisent l'état de vérification dans la même
  session SSH. L'activation radio (redémarrage matériel ~20-60s) garde
  volontairement des sessions séparées pour la vérification (voir
  `_toggle_radio_slow_on` pour la justification).
- Toutes les opérations SSH passent par une unique `asyncio.PriorityQueue`
  dépilée par un seul worker : deux opérations ne s'exécutent jamais en même
  temps, quel que soit le nombre de "groupes" ou de coordinators côté HA.
- La résolution de noms d'appareils (hostname) N'EST PLUS faite ici : elle est
  injectée via `set_hostname_resolver()`, un simple callback synchrone et non
  bloquant (lookup de cache), alimenté par un composant totalement séparé
  (voir mikrotik_resolver.py) qui a sa propre connexion et son propre cycle.
"""
import asyncio
import logging
import re
import time
from typing import Any, Callable, Optional

from .const import (
    PRIORITY_ADHOC,
    PRIORITY_FAST,
    PRIORITY_MANUAL,
    PRIORITY_SLOW,
    PRIORITY_DAILY,
    PRIORITY_WRITE,
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# Importer paramiko uniquement (plus stable avec NWA50AX)
try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False
    _LOGGER.error("paramiko not installed. Please install: pip install paramiko")


# Commandes de lecture par groupe. Les schedules SSID sont ajoutés
# dynamiquement au groupe "slow" en fonction des SSIDs détectés par le
# groupe "fast" (voir _async_get_slow_data_direct).
FAST_COMMANDS = ["show wlan all", "show wireless-hal station info"]
SLOW_BASE_COMMANDS = [
    "show cpu all",
    "show mem status",
    "show system uptime",
    "show interface all",
    "show port status",
]
DAILY_COMMANDS = ["show version"]

GROUP_PRIORITIES = {
    "fast": PRIORITY_FAST,
    "slow": PRIORITY_SLOW,
    "daily": PRIORITY_DAILY,
}


class ZyxelConnectionError(Exception):
    """Levée quand une session SSH vers l'AP n'a pas pu être établie/menée à terme."""


class ZyxelSSHAPI:
    """Class to communicate with Zyxel NWA50AX via SSH."""

    def __init__(self, host: str, username: str, password: str, port: int = 22) -> None:
        """Initialize the API."""
        self.host = host
        self.username = username
        self.password = password
        self.port = port

        # File d'attente SSH partagée par toutes les opérations (lecture ET écriture)
        self._ssh_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._queue_counter = 0
        self._queue_task: Optional[asyncio.Task] = None
        self._current_operation_task: Optional[asyncio.Task] = None
        self._current_operation_priority: Optional[int] = None

        # Coalescing : un seul refresh en vol par groupe
        self._pending_group_refresh: dict[str, asyncio.Task] = {}

        # Backoff après échecs consécutifs (ne s'applique qu'aux cycles auto,
        # jamais aux actions explicites de l'utilisateur - voir async_get_group_data)
        self._consecutive_failures = 0
        self._backoff_until = 0.0

        # Cache des SSIDs connus, alimenté par le groupe "fast" (show wlan all).
        # Évite une commande SSH dédiée pour la détection des switches SSID.
        self._known_ssids: list[str] = []

        # Résolveur de nom d'appareil externe (MAC/IP -> hostname), branché par
        # __init__.py si configuré. Doit être synchrone et non bloquant.
        self._hostname_resolver: Callable[[Optional[str], Optional[str]], Optional[str]] = (
            lambda mac, ip: None
        )

        if not HAS_PARAMIKO:
            raise ImportError(
                "paramiko is not installed. "
                "Install it with: pip install paramiko"
            )

    def set_hostname_resolver(
        self, resolver: Optional[Callable[[Optional[str], Optional[str]], Optional[str]]]
    ) -> None:
        """Branche un résolveur de noms externe (mac, ip) -> hostname|None.

        Doit être un simple lookup de cache, sans I/O réseau : la résolution
        elle-même se fait ailleurs, de façon totalement découplée de ce client SSH.
        """
        self._hostname_resolver = resolver or (lambda mac, ip: None)

    # ------------------------------------------------------------------
    # File d'attente SSH
    # ------------------------------------------------------------------

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

                    self._current_operation_priority = priority
                    self._current_operation_task = asyncio.create_task(operation_coro())

                    result = await self._current_operation_task
                    future.set_result(result)

                    self._current_operation_task = None
                    self._current_operation_priority = None
            except asyncio.CancelledError:
                _LOGGER.warning("SSH operation '%s' cancelled", operation_name)
                if not future.cancelled():
                    future.cancel()
                self._current_operation_task = None
                self._current_operation_priority = None
            except Exception as err:
                if not future.cancelled():
                    future.set_exception(err)
                _LOGGER.error("SSH queue operation '%s' failed: %s", operation_name, err)
                self._current_operation_task = None
                self._current_operation_priority = None
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

    # ------------------------------------------------------------------
    # Backoff après échecs consécutifs
    # ------------------------------------------------------------------

    def _is_in_backoff(self) -> bool:
        return time.monotonic() < self._backoff_until

    def _on_session_failure(self) -> None:
        self._consecutive_failures += 1
        delay = min(
            BACKOFF_BASE_SECONDS * (2 ** (self._consecutive_failures - 1)),
            BACKOFF_MAX_SECONDS,
        )
        self._backoff_until = time.monotonic() + delay
        _LOGGER.warning(
            "Échec de communication avec l'AP (%d consécutif(s)) - backoff %ds sur les cycles automatiques",
            self._consecutive_failures,
            delay,
        )

    def _on_session_success(self) -> None:
        if self._consecutive_failures:
            _LOGGER.info("Communication rétablie avec l'AP après %d échec(s)", self._consecutive_failures)
        self._consecutive_failures = 0
        self._backoff_until = 0.0

    # ------------------------------------------------------------------
    # Connexion / primitive SSH bas niveau
    # ------------------------------------------------------------------

    async def async_connect(self) -> bool:
        """Test SSH connection to the device."""
        try:
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
        """Disconnect - not needed with paramiko (connections are per-session)."""
        await self.async_shutdown()

    def _read_available(
        self,
        shell,
        idle_rounds: int = 3,
        idle_pause: float = 0.3,
        max_total_wait: float = 8.0,
    ) -> str:
        """Draine tout ce qui est disponible sur le canal shell.

        S'arrête après `idle_rounds` intervalles consécutifs sans nouvelles
        données (pour laisser le temps aux derniers octets d'arriver), avec un
        plafond dur `max_total_wait` pour ne jamais bloquer indéfiniment si le
        shell distant ne répond plus.
        """
        buf = ""
        idle = 0
        waited = 0.0
        while idle < idle_rounds and waited < max_total_wait:
            if shell.recv_ready():
                buf += shell.recv(8192).decode("utf-8", errors="ignore")
                idle = 0
            else:
                idle += 1
                time.sleep(idle_pause)
                waited += idle_pause
        return buf

    def _execute_session_sync(
        self,
        commands: list[str],
        settle_delays: Optional[list[float]] = None,
        capture: bool = True,
    ) -> Optional[list[str]]:
        """Exécute une série de commandes dans UNE SEULE session SSH.

        C'est la primitive centrale : elle remplace les anciennes
        `_execute_command_sync` (une connexion par commande) et
        `_execute_command_batch_sync` (batch sans capture de sortie).

        `settle_delays[i]` est le délai (s) attendu juste après l'envoi de la
        commande `i`, AVANT même de commencer à lire sa sortie - utile pour
        laisser le temps à un changement de prendre effet côté AP avant une
        commande de vérification qui suit dans la même session.

        Retourne la liste des sorties nettoyées (même longueur que `commands`),
        ou None si la session elle-même n'a jamais pu être établie/menée à
        terme (échec de connexion, exception réseau en cours de route). En cas
        d'échec, on perd la capture des commandes déjà exécutées dans CETTE
        session - le groupe entier sera retenté au cycle suivant (ou via un
        rafraîchissement manuel), ce qui est un compromis délibéré au profit
        d'une session unique bien plus rapide.
        """
        if settle_delays is None:
            settle_delays = [1.0] * len(commands)
        elif len(settle_delays) != len(commands):
            raise ValueError("settle_delays doit avoir la même longueur que commands")

        ssh = None
        shell = None
        outputs: list[str] = [""] * len(commands)

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10,
                look_for_keys=False,
                allow_agent=False,
            )

            shell = ssh.invoke_shell()
            time.sleep(1)
            if shell.recv_ready():
                shell.recv(8192)  # bannière / prompt initial

            for idx, cmd in enumerate(commands):
                _LOGGER.debug("Session SSH - envoi: %s", cmd)
                shell.send(cmd + "\n")
                time.sleep(settle_delays[idx])
                raw = self._read_available(shell)
                outputs[idx] = self._clean_output(raw, cmd) if capture else ""

            shell.send("exit\n")
            time.sleep(0.5)

            return outputs

        except Exception as err:
            _LOGGER.error(
                "Session SSH multi-commandes échouée (%d commande(s)): %s",
                len(commands), err,
            )
            return None
        finally:
            if shell:
                try:
                    shell.close()
                except Exception:
                    pass
            if ssh:
                try:
                    ssh.close()
                except Exception:
                    pass

    async def _async_execute_session_direct(
        self,
        commands: list[str],
        settle_delays: Optional[list[float]] = None,
        capture: bool = True,
    ) -> Optional[list[str]]:
        """Exécute une session multi-commandes dans l'executor, et alimente le backoff."""
        result = await asyncio.get_event_loop().run_in_executor(
            None, self._execute_session_sync, commands, settle_delays, capture
        )
        if result is None:
            self._on_session_failure()
        else:
            self._on_session_success()
        return result

    async def async_execute_command(self, command: str) -> Optional[str]:
        """Execute a single ad-hoc command on the device (low priority)."""
        try:
            outputs = await self._queue_ssh_operation(
                PRIORITY_ADHOC,
                f"command:{command}",
                lambda: self._async_execute_session_direct([command]),
            )
            return outputs[0] if outputs else None
        except Exception as err:
            _LOGGER.error("Error executing command '%s': %s", command, err)
            return None

    def _clean_output(self, output: str, command: str) -> str:
        """Clean command output by removing prompts and echoed command."""
        if not output:
            return ""

        lines = output.split('\n')
        clean_lines = []

        for line in lines:
            if any(prompt in line for prompt in ['Router(config)#', 'Router#', 'Router>']):
                continue
            if line.strip() == command.strip():
                continue
            if not clean_lines and not line.strip():
                continue

            clean_lines.append(line)

        while clean_lines and not clean_lines[-1].strip():
            clean_lines.pop()

        result = '\n'.join(clean_lines)
        return result.strip()

    # ------------------------------------------------------------------
    # Récupération de données par groupe (fast / slow / daily)
    # ------------------------------------------------------------------

    async def async_get_group_data(self, group: str, manual: bool = False) -> dict[str, Any]:
        """Récupère les données d'un groupe via la file SSH prioritaire.

        `manual=True` doit être utilisé uniquement pour un rafraîchissement
        explicitement demandé par l'utilisateur (bouton) : la demande passe
        alors devant les cycles automatiques (priorité 1 au lieu de 2/3/4), et
        n'est PAS soumise au backoff (contrairement aux cycles automatiques).
        """
        fetchers = {
            "fast": self._async_get_fast_data_direct,
            "slow": self._async_get_slow_data_direct,
            "daily": self._async_get_daily_data_direct,
        }
        if group not in fetchers:
            raise ValueError(f"Groupe de rafraîchissement inconnu: {group}")

        if not manual and self._is_in_backoff():
            remaining = self._backoff_until - time.monotonic()
            _LOGGER.debug("Backoff actif (%ds restantes), cycle '%s' automatique sauté", int(remaining), group)
            raise ZyxelConnectionError(f"AP en backoff après échecs consécutifs ({int(remaining)}s restantes)")

        # Coalescing : si un refresh de CE groupe est déjà en vol, on s'y raccroche
        pending = self._pending_group_refresh.get(group)
        if pending and not pending.done():
            return await pending

        priority = PRIORITY_MANUAL if manual else GROUP_PRIORITIES[group]
        task = asyncio.create_task(
            self._queue_ssh_operation(priority, f"refresh:{group}", fetchers[group])
        )
        self._pending_group_refresh[group] = task
        try:
            return await task
        finally:
            if self._pending_group_refresh.get(group) is task:
                del self._pending_group_refresh[group]

    async def _async_get_fast_data_direct(self) -> dict[str, Any]:
        """Groupe rapide : état des radios + clients connectés."""
        data: dict[str, Any] = {"clients": [], "radio": {}}

        outputs = await self._async_execute_session_direct(
            FAST_COMMANDS, settle_delays=[2.5, 2.5]
        )
        if outputs is None:
            raise ZyxelConnectionError("Impossible de contacter l'AP (groupe rapide)")

        wlan_output, clients_output = outputs

        if wlan_output:
            data["radio"] = self._parse_wlan(wlan_output)
            self._known_ssids = sorted(
                {s for s in data["radio"].get("slot1_ssids", []) if s}
                | {s for s in data["radio"].get("slot2_ssids", []) if s}
            )

        if clients_output:
            data["clients"] = self._parse_clients(clients_output)

        return data

    async def _async_get_slow_data_direct(self) -> dict[str, Any]:
        """Groupe lent : CPU/RAM/uptime/interfaces/port + schedules SSID."""
        data: dict[str, Any] = {"status": {}, "network": {}, "ssid_schedules": {}}

        ssid_names = list(self._known_ssids)
        commands = list(SLOW_BASE_COMMANDS) + [
            f"show wlan-ssid-profile {name}" for name in ssid_names
        ]
        settle_delays = [1.5] * len(SLOW_BASE_COMMANDS) + [1.5] * len(ssid_names)

        outputs = await self._async_execute_session_direct(commands, settle_delays=settle_delays)
        if outputs is None:
            raise ZyxelConnectionError("Impossible de contacter l'AP (groupe lent)")

        cpu_out, mem_out, uptime_out, iface_out, port_out, *ssid_outputs = outputs

        if cpu_out:
            data["status"]["cpu"] = self._parse_cpu(cpu_out)
        if mem_out:
            data["status"]["memory"] = self._parse_memory(mem_out)
        if uptime_out:
            data["status"]["uptime"] = self._parse_uptime(uptime_out)
        if iface_out:
            data["network"] = self._parse_interfaces(iface_out)
        if port_out:
            data.setdefault("network", {})["port"] = self._parse_port_status(port_out)

        for name, output in zip(ssid_names, ssid_outputs):
            if output:
                data["ssid_schedules"][name] = self._parse_ssid_schedule_mode(output)

        return data

    async def _async_get_daily_data_direct(self) -> dict[str, Any]:
        """Groupe quotidien : modèle / firmware / build date."""
        data: dict[str, Any] = {"device_info": {}}

        outputs = await self._async_execute_session_direct(DAILY_COMMANDS, settle_delays=[1.5])
        if outputs is None:
            raise ZyxelConnectionError("Impossible de contacter l'AP (groupe quotidien)")

        if outputs[0]:
            data["device_info"] = self._parse_version(outputs[0])

        return data

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

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

        match = re.search(r'(\d+)\s+days?\s+(\d+):(\d+):(\d+)', output)
        if match:
            days = int(match.group(1))
            hours = int(match.group(2))
            minutes = int(match.group(3))
            seconds = int(match.group(4))
            uptime_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
        else:
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
        """Parse 'show wireless-hal station info' output.

        Ne fait plus AUCUNE résolution DNS ici (c'était bloquant sur l'event
        loop HA). Le hostname, s'il est disponible, vient d'un cache externe
        alimenté séparément (voir set_hostname_resolver / mikrotik_resolver.py).
        """
        clients = []

        client_blocks = re.split(r'index:\s*\d+', output)

        for block in client_blocks[1:]:
            client: dict[str, Any] = {}

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
                try:
                    hostname = self._hostname_resolver(client.get("mac"), client.get("ip"))
                except Exception as err:  # le résolveur externe ne doit jamais casser ce parsing
                    _LOGGER.debug("Hostname resolver error for %s: %s", client.get("mac"), err)
                    hostname = None
                client["hostname"] = hostname

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

    def _parse_radio_slot_active(self, output: str, slot: int) -> Optional[bool]:
        """Extrait uniquement l'état Activate: yes/no d'un slot depuis 'show wlan all'."""
        match = re.search(rf"slot: slot{slot}.*?Activate: (\w+)", output, re.DOTALL)
        return (match.group(1).lower() == "yes") if match else None

    def _parse_ssid_schedule_mode(self, output: str) -> Optional[bool]:
        """Extrait SSID_schedule_mode: yes/no depuis 'show wlan-ssid-profile <name>'."""
        match = re.search(r'SSID_schedule_mode:\s*(\w+)', output)
        if not match:
            return None
        return match.group(1).lower() == "yes"

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

    # ------------------------------------------------------------------
    # Actions (écriture)
    # ------------------------------------------------------------------

    async def async_reboot(self) -> bool:
        """Reboot the device."""
        try:
            outputs = await self._queue_ssh_operation(
                PRIORITY_WRITE,
                "action:reboot",
                lambda: self._async_execute_session_direct(["reboot"], settle_delays=[2.0]),
            )
            if outputs is not None:
                _LOGGER.info("Reboot command sent")
                return True
            return False
        except Exception as err:
            _LOGGER.error("Error rebooting device: %s", err)
            return False

    async def async_get_radio_state(self, slot: int) -> Optional[bool]:
        """Get radio activation state. True=actif, False=inactif, None=indéterminé."""
        try:
            outputs = await self._queue_ssh_operation(
                PRIORITY_WRITE,
                f"radio:state:slot{slot}",
                lambda: self._async_execute_session_direct(["show wlan all"], settle_delays=[2.5]),
            )
            if not outputs or not outputs[0]:
                return None
            return self._parse_radio_slot_active(outputs[0], slot)
        except Exception as err:
            _LOGGER.error("Error getting radio state for slot %d: %s", slot, err)
            return None

    async def _is_ap_responsive(self) -> bool:
        """Check if AP responds to commands (lightweight ping).

        Passe désormais PAR la file d'attente (contrairement à la v1 qui
        appelait directement l'exécuteur) : cela évite qu'un cycle de
        rafraîchissement automatique s'exécute EN PARALLÈLE de ce test pendant
        un redémarrage radio, ce qui violerait la contrainte "1 session SSH à
        la fois" documentée sur cet AP.
        """
        try:
            outputs = await asyncio.wait_for(
                self._queue_ssh_operation(
                    PRIORITY_WRITE,
                    "radio:ap_responsive_check",
                    lambda: self._async_execute_session_direct(["show version"], settle_delays=[1.5]),
                ),
                timeout=15,
            )
            return bool(outputs and outputs[0] and len(outputs[0]) > 10)
        except Exception as err:
            _LOGGER.debug("AP not responsive: %s", err)
            return False

    async def async_toggle_radio(self, slot: int, enable: bool) -> bool:
        """Active ou désactive une radio.

        - Désactivation : quasi instantanée (~2-3s). Commande + vérification
          sont fusionnées dans UNE session SSH.
        - Activation : la radio physique redémarre (~20-60s). On garde
          volontairement des sessions séparées et espacées pour la commande
          puis les vérifications, plutôt qu'une session unique tenue ouverte
          pendant tout le redémarrage matériel (risque de canal SSH idle qui
          tombe pendant que le firmware réinitialise la radio).
        """
        if not enable:
            return await self._toggle_radio_fast_off(slot)
        return await self._toggle_radio_slow_on(slot)

    async def _toggle_radio_fast_off(self, slot: int) -> bool:
        """Désactivation radio : commande + vérification dans une seule session."""
        base_commands = [
            "configure terminal",
            f"wlan slot{slot}",
            "no activate",
            "exit",
            "exit",
        ]
        verify_cmd = "show wlan all"
        all_commands = base_commands + [verify_cmd]
        verify_delay = 4.0

        for attempt in (1, 2):
            settle_delays = [1.0, 1.0, 1.0, 1.0, 1.0, verify_delay]
            outputs = await self._queue_ssh_operation(
                PRIORITY_WRITE,
                f"action:radio:slot{slot}:deactivate:attempt{attempt}",
                lambda cmds=all_commands, delays=settle_delays: self._async_execute_session_direct(
                    cmds, settle_delays=delays
                ),
            )

            if outputs is None:
                _LOGGER.error("Radio slot %d: échec de connexion (tentative %d/2)", slot, attempt)
                continue

            verify_output = outputs[-1]
            current_state = self._parse_radio_slot_active(verify_output, slot) if verify_output else None

            if current_state is False:
                _LOGGER.info("Radio slot %d désactivée avec succès", slot)
                return True

            _LOGGER.warning(
                "Radio slot %d pas encore désactivée (tentative %d/2, état lu=%s)",
                slot, attempt, current_state,
            )
            verify_delay += 3.0  # un peu plus de marge au 2e essai

        _LOGGER.error("Radio slot %d: échec après toutes les tentatives", slot)
        return False

    async def _toggle_radio_slow_on(self, slot: int) -> bool:
        """Activation radio : sessions séparées pour la commande puis les vérifications."""
        commands = [
            "configure terminal",
            f"wlan slot{slot}",
            "activate",
            "exit",
            "exit",
        ]
        settle_delays = [1.0, 1.0, 1.0, 1.0, 1.0]

        for attempt in (1, 2):
            _LOGGER.info("Radio slot %d: envoi de la commande activate (tentative %d/2)", slot, attempt)
            outputs = await self._queue_ssh_operation(
                PRIORITY_WRITE,
                f"action:radio:slot{slot}:activate:attempt{attempt}",
                lambda: self._async_execute_session_direct(commands, settle_delays=settle_delays),
            )

            if outputs is None:
                _LOGGER.error("Radio slot %d: échec de connexion pour la commande activate", slot)
                continue

            delays = (30, 10)
            for delay in delays:
                await asyncio.sleep(delay)
                current_state = await self.async_get_radio_state(slot)
                if current_state is None:
                    _LOGGER.warning("Radio slot %d: état indéterminé après %ds", slot, delay)
                    continue
                if current_state:
                    _LOGGER.info("Radio slot %d activée, vérification de la disponibilité de l'AP...", slot)
                    for ping_attempt in range(6):  # 6 x 5s = 30s max
                        if await self._is_ap_responsive():
                            _LOGGER.info("AP de nouveau disponible après %ds", (ping_attempt + 1) * 5)
                            return True
                        await asyncio.sleep(5)
                    _LOGGER.warning("AP toujours peu réactif après 30s, mais la radio est active")
                    return True

            if attempt == 1:
                _LOGGER.warning("Radio slot %d pas dans l'état attendu, nouvel essai", slot)

        _LOGGER.error("Radio slot %d: échec après toutes les tentatives", slot)
        return False

    async def async_get_ssid_list(self) -> list[str]:
        """Retourne les SSIDs connus, depuis le cache alimenté par le groupe rapide.

        Ne déclenche une commande SSH que si le cache est encore vide (par
        exemple au tout premier démarrage, avant le premier refresh "fast").
        """
        if self._known_ssids:
            return list(self._known_ssids)

        _LOGGER.debug("Cache SSID vide, interrogation ponctuelle de l'AP")
        try:
            outputs = await self._queue_ssh_operation(
                PRIORITY_ADHOC,
                "ssid_list:adhoc",
                lambda: self._async_execute_session_direct(["show wlan all"], settle_delays=[2.5]),
            )
            if outputs and outputs[0]:
                radio = self._parse_wlan(outputs[0])
                self._known_ssids = sorted(
                    {s for s in radio.get("slot1_ssids", []) if s}
                    | {s for s in radio.get("slot2_ssids", []) if s}
                )
        except Exception as err:
            _LOGGER.error("Error getting SSID list: %s", err)

        return list(self._known_ssids)

    async def async_get_ssid_schedule_state(self, ssid_name: str) -> Optional[bool]:
        """Lecture ponctuelle (hors cycle normal) du schedule d'un SSID."""
        try:
            outputs = await self._queue_ssh_operation(
                PRIORITY_ADHOC,
                f"ssid_schedule:state:{ssid_name}",
                lambda: self._async_execute_session_direct(
                    [f"show wlan-ssid-profile {ssid_name}"], settle_delays=[2.0]
                ),
            )
            if not outputs or not outputs[0]:
                return None
            return self._parse_ssid_schedule_mode(outputs[0])
        except Exception as err:
            _LOGGER.error("Error getting SSID schedule state for '%s': %s", ssid_name, err)
            return None

    async def async_toggle_ssid_schedule(self, ssid_name: str, enable: bool, persist: bool = False) -> bool:
        """Active/désactive le planning d'un SSID, commande + vérification en une session.

        - enable=True  -> "ssid-schedule" (le SSID suit son planning configuré)
        - enable=False -> "no ssid-schedule" (le SSID reste actif en permanence)
        - persist=True -> ajoute 'write' (persistance NVRAM, ~5-10s de blocage AP
          en plus). Utilisé uniquement pour le SSID Guest, afin de conserver le
          comportement historique de l'intégration (voir switch.py).
        """
        action = "enable" if enable else "disable"
        schedule_cmd = "ssid-schedule" if enable else "no ssid-schedule"

        for attempt in (1, 2):
            commands = [
                "configure terminal",
                f"wlan-ssid-profile {ssid_name}",
                schedule_cmd,
                "exit",
                "write" if persist else "exit",
            ]
            verify_cmd = f"show wlan-ssid-profile {ssid_name}"
            all_commands = commands + [verify_cmd]

            base_delay = 1.0
            write_delay = 8.0 if persist else 1.0
            verify_delay = 3.0 + (attempt - 1) * 3.0
            settle_delays = [base_delay, base_delay, base_delay, base_delay, write_delay, verify_delay]

            _LOGGER.info("SSID '%s': %s schedule (tentative %d/2)", ssid_name, action, attempt)
            outputs = await self._queue_ssh_operation(
                PRIORITY_WRITE,
                f"ssid_schedule:{ssid_name}:{action}:attempt{attempt}",
                lambda cmds=all_commands, delays=settle_delays: self._async_execute_session_direct(
                    cmds, settle_delays=delays
                ),
            )

            if outputs is None:
                _LOGGER.error("SSID '%s': échec de connexion (tentative %d/2)", ssid_name, attempt)
                continue

            verify_output = outputs[-1]
            current_state = self._parse_ssid_schedule_mode(verify_output) if verify_output else None

            if current_state is None:
                _LOGGER.warning("SSID '%s': état indéterminé après le changement", ssid_name)
                continue

            if current_state == enable:
                _LOGGER.info("SSID '%s': schedule %s avec succès", ssid_name, action)
                return True

            _LOGGER.warning(
                "SSID '%s': état inattendu (attendu=%s, lu=%s), nouvel essai",
                ssid_name, enable, current_state,
            )

        _LOGGER.error("SSID '%s': échec après toutes les tentatives", ssid_name)
        return False
