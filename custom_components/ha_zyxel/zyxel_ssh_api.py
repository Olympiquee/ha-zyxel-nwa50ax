"""API client for Zyxel NWA50AX via SSH.

Firmware validé (voir tests/fixtures/) :
- 7.10(ABYW.3) - via la documentation constructeur et le CLI Reference Guide
- 7.12(ABYW.0) - via des captures CLI réelles, aucune rupture de format
  détectée sur les commandes utilisées par cette intégration.

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
- La clé hôte SSH est mémorisée au premier contact et vérifiée ensuite
  (TOFU - voir ssh_security.py), plutôt qu'acceptée sans condition à chaque
  connexion.
- Une valeur numérique/booléenne non déterminée (échec de parsing, format
  inattendu) est représentée par `None`, jamais par 0/False : ces deux
  dernières valeurs doivent rester réservées à une mesure réelle. Une donnée
  manquante qui ressemblerait à une vraie mesure serait plus trompeuse qu'une
  absence de donnée explicite.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass
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
    CLI_CLOSE_DELAY,
    CLI_CONNECT_TIMEOUT,
    CLI_INITIAL_DELAY,
    CLI_READ_IDLE_PAUSE,
    CLI_READ_IDLE_ROUNDS,
    CLI_READ_MAX_WAIT,
    CLI_SETTLE_CONFIG,
    CLI_SETTLE_DEFAULT,
    CLI_SETTLE_RADIO_VERIFY,
    CLI_SETTLE_SLOW_READ,
    CLI_SETTLE_WRITE,
    DATA_ITEMS,
    DATA_ITEM_CLIENTS,
    DATA_ITEM_CPU,
    DATA_ITEM_DEVICE_INFO,
    DATA_ITEM_INTERFACES,
    DATA_ITEM_MEMORY,
    DATA_ITEM_PORT,
    DATA_ITEM_RADIO,
    DATA_ITEM_SSID_SCHEDULES,
    DATA_ITEM_UPTIME,
    DEFAULT_ITEM_GROUPS,
)
from .ssh_security import PinnedHostKeyPolicy, SSHHostKeyChangedError

_LOGGER = logging.getLogger(__name__)

# Importer paramiko uniquement (plus stable avec NWA50AX)
try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False
    _LOGGER.error("paramiko not installed. Please install: pip install paramiko")


class ZyxelAuthError(Exception):
    """Levée quand l'AP refuse les identifiants (mot de passe erroné/changé).

    Distincte de ZyxelConnectionError (hôte injoignable) pour permettre à
    __init__.py de déclencher un flux `reauth` HA plutôt qu'un simple retry.
    """


@dataclass
class _DataItemSpec:
    """Définition d'un item de données : comment le demander et le parser.

    `build_commands` est un callable à zéro argument (généralement une lambda
    fermée sur `self`) plutôt qu'une simple liste, car certains items sont
    dynamiques (les schedules SSID dépendent du nombre de SSIDs actuellement
    connus, qui peut changer d'un cycle à l'autre).
    """

    build_commands: Callable[[], list[str]]
    settle_delay: float
    apply: Callable[[dict, list[str]], None]


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

        # Cache des SSIDs connus, alimenté par l'item "radio" (show wlan all),
        # quel que soit le groupe auquel cet item est actuellement affecté.
        self._known_ssids: list[str] = []

        # Répartition des données entre groupes fast/slow/daily - modifiable
        # depuis les options HA via set_item_groups(). Par défaut : le tri
        # qu'on a défini ensemble (voir const.DEFAULT_ITEM_GROUPS).
        self._item_groups: dict[str, str] = dict(DEFAULT_ITEM_GROUPS)
        self._item_registry: dict[str, _DataItemSpec] = self._build_item_registry()

        # Résolveur de nom d'appareil externe (MAC/IP -> hostname), branché par
        # __init__.py si configuré. Doit être synchrone et non bloquant.
        self._hostname_resolver: Callable[[Optional[str], Optional[str]], Optional[str]] = (
            lambda mac, ip: None
        )

        # Empreinte de clé hôte SSH mémorisée (TOFU) - branchée par __init__.py
        # depuis les options de l'entry. None tant qu'aucune connexion n'a
        # encore réussi.
        self._pinned_fingerprint: Optional[str] = None
        self._on_fingerprint_pinned: Callable[[str], None] = lambda fp: None

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

    def set_pinned_fingerprint(self, fingerprint: Optional[str]) -> None:
        """Empreinte SSH déjà connue (persistée côté HA), ou None si jamais vue."""
        self._pinned_fingerprint = fingerprint

    def set_fingerprint_pinned_callback(self, callback: Optional[Callable[[str], None]]) -> None:
        """Callback appelé la première fois qu'une empreinte est mémorisée (TOFU),
        pour que l'appelant (HA) la persiste dans les options de l'entry."""
        self._on_fingerprint_pinned = callback or (lambda fp: None)

    def _make_host_key_policy(self) -> PinnedHostKeyPolicy:
        def _get_pinned() -> Optional[str]:
            return self._pinned_fingerprint

        def _on_trust(fingerprint: str) -> None:
            self._pinned_fingerprint = fingerprint
            self._on_fingerprint_pinned(fingerprint)

        return PinnedHostKeyPolicy(_get_pinned, _on_trust)

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
        """Shutdown propre : vide la file, annule le travail en attente, puis le worker.

        Avant (point de revue #7) : seul le worker était annulé, en laissant
        d'éventuelles opérations encore en file (et leurs futures associées)
        sans résolution explicite. Sans conséquence en usage normal de HA,
        mais plus correct de tout annuler proprement à l'unload :

            stop accepting new work (implicite : plus personne n'appelle
            _queue_ssh_operation après unload côté HA)
                -> cancel queued operations
                -> cancel pending futures
                -> cancel l'opération en cours
                -> cancel le worker
        """
        # Vide la file et annule chaque future en attente
        while not self._ssh_queue.empty():
            try:
                _, _, operation_name, _, future = self._ssh_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not future.done():
                _LOGGER.debug("Annulation de l'opération en attente '%s' (shutdown)", operation_name)
                future.cancel()
            self._ssh_queue.task_done()

        # Annule l'opération éventuellement en cours d'exécution
        if self._current_operation_task and not self._current_operation_task.done():
            self._current_operation_task.cancel()

        # Puis le worker lui-même
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
        """Test SSH connection to the device.

        Lève `ZyxelAuthError` si l'AP refuse explicitement les identifiants
        (mot de passe erroné/changé) - distinct d'un simple retour False pour
        les autres échecs (hôte injoignable, timeout), afin que __init__.py
        puisse déclencher un flux `reauth` HA plutôt qu'un retry aveugle.
        Lève `SSHHostKeyChangedError` si la clé hôte SSH a changé depuis la
        dernière connexion réussie (voir ssh_security.py).
        """
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._test_connection
            )
            if result:
                _LOGGER.info("Successfully tested SSH connection to %s", self.host)
            return result
        except (ZyxelAuthError, SSHHostKeyChangedError):
            raise
        except Exception as err:
            _LOGGER.error("SSH connection test failed: %s", err)
            return False

    def _test_connection(self) -> bool:
        """Test SSH connection synchronously."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(self._make_host_key_policy())

        try:
            ssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=CLI_CONNECT_TIMEOUT,
                look_for_keys=False,
                allow_agent=False
            )
            ssh.close()
            return True
        except paramiko.AuthenticationException as err:
            _LOGGER.error("Authentification SSH refusée par %s: %s", self.host, err)
            raise ZyxelAuthError(f"Authentification refusée par {self.host}") from err
        except SSHHostKeyChangedError:
            raise
        except Exception as err:
            _LOGGER.error("Connection test failed: %s", err)
            return False

    async def async_disconnect(self) -> None:
        """Disconnect - not needed with paramiko (connections are per-session)."""
        await self.async_shutdown()

    def _read_available(
        self,
        shell,
        idle_rounds: int = CLI_READ_IDLE_ROUNDS,
        idle_pause: float = CLI_READ_IDLE_PAUSE,
        max_total_wait: float = CLI_READ_MAX_WAIT,
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
            settle_delays = [CLI_SETTLE_DEFAULT] * len(commands)
        elif len(settle_delays) != len(commands):
            raise ValueError("settle_delays doit avoir la même longueur que commands")

        ssh = None
        shell = None
        outputs: list[str] = [""] * len(commands)

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(self._make_host_key_policy())
            ssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=CLI_CONNECT_TIMEOUT,
                look_for_keys=False,
                allow_agent=False,
            )

            shell = ssh.invoke_shell()
            time.sleep(CLI_INITIAL_DELAY)
            if shell.recv_ready():
                shell.recv(8192)  # bannière / prompt initial

            for idx, cmd in enumerate(commands):
                _LOGGER.debug("Session SSH - envoi: %s", cmd)
                shell.send(cmd + "\n")
                time.sleep(settle_delays[idx])
                raw = self._read_available(shell)
                outputs[idx] = self._clean_output(raw, cmd) if capture else ""

            shell.send("exit\n")
            time.sleep(CLI_CLOSE_DELAY)

            return outputs

        except (paramiko.AuthenticationException, SSHHostKeyChangedError):
            raise
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
        """Exécute une session multi-commandes dans l'executor, et alimente le backoff.

        Une authentification refusée est reconvertie en `ZyxelAuthError` ici -
        seul endroit qui a besoin de connaître le type d'exception paramiko
        sous-jacent - pour que le reste du code (et __init__.py) n'ait jamais
        besoin d'importer paramiko directement.
        """
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._execute_session_sync, commands, settle_delays, capture
            )
        except paramiko.AuthenticationException as err:
            raise ZyxelAuthError(f"Authentification refusée par {self.host}") from err

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
        if group not in GROUP_PRIORITIES:
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
            self._queue_ssh_operation(
                priority, f"refresh:{group}", lambda: self._async_get_group_data_direct(group)
            )
        )
        self._pending_group_refresh[group] = task
        try:
            return await task
        finally:
            if self._pending_group_refresh.get(group) is task:
                del self._pending_group_refresh[group]

    def set_item_groups(self, item_groups: dict[str, str]) -> None:
        """Définit la répartition des données entre groupes fast/slow/daily.

        Configurable depuis les options de l'intégration côté HA. Les items
        absents de `item_groups` gardent leur affectation par défaut.
        """
        merged = dict(DEFAULT_ITEM_GROUPS)
        merged.update({k: v for k, v in (item_groups or {}).items() if v in GROUP_PRIORITIES})
        self._item_groups = merged
        _LOGGER.debug("Répartition des données mise à jour: %s", self._item_groups)

    def get_item_groups(self) -> dict[str, str]:
        """Copie de la répartition actuelle des données entre groupes."""
        return dict(self._item_groups)

    def get_diagnostics_snapshot(self) -> dict[str, Any]:
        """Petit résumé interne pour diagnostics.py.

        Volontairement dénué de tout MAC/IP/secret : juste de quoi comprendre
        l'état de santé de la connexion (échecs consécutifs, backoff) et un
        décompte (pas la liste) des SSIDs connus.
        """
        return {
            "consecutive_failures": self._consecutive_failures,
            "in_backoff": self._is_in_backoff(),
            "known_ssid_count": len(self._known_ssids),
        }

    async def _async_get_group_data_direct(self, group: str) -> dict[str, Any]:
        """Récupère, en UNE session SSH, tous les items actuellement affectés à `group`.

        Générique et piloté par `self._item_groups` / `self._item_registry` :
        aucune commande n'est câblée en dur ici, tout vient de la table
        construite dans `_build_item_registry()`.
        """
        items_in_group = [
            item for item in DATA_ITEMS
            if self._item_groups.get(item, DEFAULT_ITEM_GROUPS[item]) == group
        ]

        commands: list[str] = []
        settle_delays: list[float] = []
        spans: list[tuple[str, int, int]] = []

        for item_key in items_in_group:
            spec = self._item_registry[item_key]
            cmds = spec.build_commands()
            start = len(commands)
            if cmds:
                commands.extend(cmds)
                settle_delays.extend([spec.settle_delay] * len(cmds))
            # Span à vide (start == start) si cet item n'a rien à demander ce
            # coup-ci (ex: schedules SSID avant le tout premier cycle radio) -
            # son apply() sera quand même appelé avec une liste vide plus bas,
            # pour que ses valeurs par défaut ({} plutôt qu'absent) restent
            # cohérentes, sans pour autant ouvrir de session pour rien.
            spans.append((item_key, start, start + len(cmds)))

        if not commands:
            data: dict[str, Any] = {}
            for item_key, start, end in spans:
                self._item_registry[item_key].apply(data, [])
            return data

        outputs = await self._async_execute_session_direct(commands, settle_delays=settle_delays)
        if outputs is None:
            raise ZyxelConnectionError(f"Impossible de contacter l'AP (groupe {group})")

        data: dict[str, Any] = {}
        for item_key, start, end in spans:
            self._item_registry[item_key].apply(data, outputs[start:end])

        return data

    def _build_item_registry(self) -> dict[str, "_DataItemSpec"]:
        """Construit la table item -> (commandes, délai, parseur).

        C'est la SEULE source de vérité pour "quelle(s) commande(s) pour quel
        item, et comment en extraire les données" - `_async_get_group_data_direct`
        ne fait qu'assembler ce que cette table lui donne, quel que soit le
        groupe auquel chaque item est actuellement affecté.
        """

        def _apply_radio(data: dict, outputs: list[str]) -> None:
            output = outputs[0] if outputs else ""
            if not output:
                return
            data["radio"] = self._parse_wlan(output)
            self._known_ssids = sorted(
                {s for s in data["radio"].get("slot1_ssids", []) if s}
                | {s for s in data["radio"].get("slot2_ssids", []) if s}
            )

        def _apply_clients(data: dict, outputs: list[str]) -> None:
            output = outputs[0] if outputs else ""
            if output:
                data["clients"] = self._parse_clients(output)

        def _apply_cpu(data: dict, outputs: list[str]) -> None:
            if outputs and outputs[0]:
                data.setdefault("status", {})["cpu"] = self._parse_cpu(outputs[0])

        def _apply_memory(data: dict, outputs: list[str]) -> None:
            if outputs and outputs[0]:
                data.setdefault("status", {})["memory"] = self._parse_memory(outputs[0])

        def _apply_uptime(data: dict, outputs: list[str]) -> None:
            if outputs and outputs[0]:
                data.setdefault("status", {})["uptime"] = self._parse_uptime(outputs[0])

        def _apply_interfaces(data: dict, outputs: list[str]) -> None:
            if outputs and outputs[0]:
                data.setdefault("network", {}).update(self._parse_interfaces(outputs[0]))

        def _apply_port(data: dict, outputs: list[str]) -> None:
            if outputs and outputs[0]:
                data.setdefault("network", {})["port"] = self._parse_port_status(outputs[0])

        def _apply_ssid_schedules(data: dict, outputs: list[str]) -> None:
            schedules = {}
            for name, output in zip(self._known_ssids, outputs):
                if output:
                    schedules[name] = self._parse_ssid_schedule_mode(output)
            data["ssid_schedules"] = schedules

        def _apply_device_info(data: dict, outputs: list[str]) -> None:
            if outputs and outputs[0]:
                data["device_info"] = self._parse_version(outputs[0])

        return {
            DATA_ITEM_RADIO: _DataItemSpec(lambda: ["show wlan all"], CLI_SETTLE_SLOW_READ, _apply_radio),
            DATA_ITEM_CLIENTS: _DataItemSpec(
                lambda: ["show wireless-hal station info"], CLI_SETTLE_SLOW_READ, _apply_clients
            ),
            DATA_ITEM_CPU: _DataItemSpec(lambda: ["show cpu all"], CLI_SETTLE_DEFAULT, _apply_cpu),
            DATA_ITEM_MEMORY: _DataItemSpec(lambda: ["show mem status"], CLI_SETTLE_DEFAULT, _apply_memory),
            DATA_ITEM_UPTIME: _DataItemSpec(lambda: ["show system uptime"], CLI_SETTLE_DEFAULT, _apply_uptime),
            DATA_ITEM_INTERFACES: _DataItemSpec(
                lambda: ["show interface all"], CLI_SETTLE_DEFAULT, _apply_interfaces
            ),
            DATA_ITEM_PORT: _DataItemSpec(lambda: ["show port status"], CLI_SETTLE_DEFAULT, _apply_port),
            DATA_ITEM_SSID_SCHEDULES: _DataItemSpec(
                lambda: [f"show wlan-ssid-profile {name}" for name in self._known_ssids],
                CLI_SETTLE_DEFAULT,
                _apply_ssid_schedules,
            ),
            DATA_ITEM_DEVICE_INFO: _DataItemSpec(lambda: ["show version"], CLI_SETTLE_DEFAULT, _apply_device_info),
        }

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

        model_match = re.search(r'(?i)model\s*:\s*(.+)', output)
        if model_match:
            info["model"] = model_match.group(1).strip()

        firmware_match = re.search(r'(?i)firmware\s+version\s*:\s*(.+)', output)
        if firmware_match:
            info["firmware"] = firmware_match.group(1).strip()

        build_match = re.search(r'(?i)build\s+date\s*:\s*(.+)', output)
        if build_match:
            info["build_date"] = build_match.group(1).strip()

        return info

    def _parse_uptime(self, output: str) -> Optional[int]:
        """Parse 'show system uptime' output. Returns uptime in seconds, or
        None si le format n'a pas pu être reconnu (plutôt que 0, qui serait
        indiscernable d'un AP qui viendrait tout juste de redémarrer)."""
        match = re.search(r'(\d+)\s+days?\s+(\d+):(\d+):(\d+)', output, re.IGNORECASE)
        if match:
            days, hours, minutes, seconds = (int(g) for g in match.groups())
            return days * 86400 + hours * 3600 + minutes * 60 + seconds

        match = re.search(r'(\d+):(\d+):(\d+)', output)
        if match:
            hours, minutes, seconds = (int(g) for g in match.groups())
            return hours * 3600 + minutes * 60 + seconds

        return None

    def _parse_cpu(self, output: str) -> dict[str, Any]:
        """Parse 'show cpu all' output.

        `current`/`avg_1min`/`avg_5min` sont None si aucun cœur n'a pu être
        extrait (plutôt que 0, qui laisserait croire à une mesure réelle de
        0% d'utilisation). Fonctionne quel que soit le nombre de cœurs (2 ou 4
        selon le modèle/firmware - vu 4 cœurs en 7.12 contre l'hypothèse
        initiale de 2)."""
        cpu_data: dict[str, Any] = {
            "current": None,
            "avg_1min": None,
            "avg_5min": None,
            "cores": [],
        }

        core_pattern = r'(?i)CPU\s+core\s+(\d+)\s+utilization:\s*(\d+)\s*%'
        core_1min_pattern = r'(?i)CPU\s+core\s+(\d+)\s+utilization\s+for\s+1\s*min:\s*(\d+)\s*%'
        core_5min_pattern = r'(?i)CPU\s+core\s+(\d+)\s+utilization\s+for\s+5\s*min:\s*(\d+)\s*%'

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

    def _parse_memory(self, output: str) -> Optional[int]:
        """Parse 'show mem status' output. Returns percentage, or None si le
        format n'a pas pu être reconnu (plutôt que 0)."""
        match = re.search(r'(?i)memory\s+usage\s*:\s*(\d+)\s*%', output)
        if match:
            return int(match.group(1))
        return None

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

            # Le format du champ Band varie ("2.4G"/"5G" selon la doc, "2.4GHz"
            # /"5GHz" observé en 7.12) - le code qui consomme cette valeur
            # (compteurs par bande, device_tracker) teste une sous-chaîne
            # ("2.4" / "5"), donc les deux formats fonctionnent. On capture
            # largement pour rester tolérant à d'éventuelles variantes futures.
            band_match = re.search(r'Band:\s*(\S+)', block)
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
        network: dict[str, Any] = {
            "ip_address": None,
            "netmask": None,
            "interfaces": [],
        }

        lan_match = re.search(r'(?i)lan\s+Up\s+([\d.]+)\s+([\d.]+)', output)
        if lan_match:
            network["ip_address"] = lan_match.group(1)
            network["netmask"] = lan_match.group(2)

        interface_lines = re.findall(r'(\d+)\s+(\S+)\s+(Up|Down|n/a)\s+([\d.]+|n/a)', output, re.IGNORECASE)
        for iface in interface_lines:
            network["interfaces"].append({
                "name": iface[1],
                "status": iface[2],
                "ip": iface[3] if iface[3] != "n/a" else None,
            })

        return network

    def _parse_wlan(self, output: str) -> dict[str, Any]:
        """Parse 'show wlan all' output.

        `slot1_active`/`slot2_active` sont None si le format n'a pas pu être
        reconnu - PAS False, qui laisserait croire à tort que la radio a été
        interrogée avec succès et trouvée désactivée."""
        radio: dict[str, Any] = {
            "slot1_active": None,
            "slot1_band": "Unknown",
            "slot1_ssids": [],
            "slot2_active": None,
            "slot2_band": "Unknown",
            "slot2_ssids": [],
        }

        slot1_match = re.search(
            r'(?i)slot:\s*slot1.*?Activate:\s*(\w+).*?Band:\s*([\dG.]+)', output, re.DOTALL
        )
        if slot1_match:
            radio["slot1_active"] = slot1_match.group(1).lower() == "yes"
            radio["slot1_band"] = slot1_match.group(2)

        slot1_block = re.search(r'slot:\s*slot1(.*?)(?:slot:\s*slot2|$)', output, re.DOTALL | re.IGNORECASE)
        if slot1_block:
            # [ \t]* (pas \s*) : \s inclut le saut de ligne, ce qui faisait
            # "déborder" la capture sur le libellé du CHAMP SUIVANT quand un
            # profil SSID est vide (ex: "SSID_profile_5:\n SSID_profile_6:"
            # capturait à tort "SSID_profile_6:" comme valeur du profil 5).
            # Bug latent trouvé via les fixtures 7.12 (4 SSIDs + profils vides
            # en fin de liste, jamais exercé par les données de test précédentes).
            ssids = re.findall(r'SSID_profile_\d+:[ \t]*(\S+)', slot1_block.group(1))
            radio["slot1_ssids"] = [s for s in ssids if s]

        slot2_match = re.search(
            r'(?i)slot:\s*slot2.*?Activate:\s*(\w+).*?Band:\s*([\dG.]+)', output, re.DOTALL
        )
        if slot2_match:
            radio["slot2_active"] = slot2_match.group(1).lower() == "yes"
            radio["slot2_band"] = slot2_match.group(2)

        slot2_block = re.search(r'slot:\s*slot2(.*?)$', output, re.DOTALL | re.IGNORECASE)
        if slot2_block:
            ssids = re.findall(r'SSID_profile_\d+:[ \t]*(\S+)', slot2_block.group(1))
            radio["slot2_ssids"] = [s for s in ssids if s]

        return radio

    def _parse_radio_slot_active(self, output: str, slot: int) -> Optional[bool]:
        """Extrait uniquement l'état Activate: yes/no d'un slot depuis 'show wlan all'."""
        match = re.search(rf"(?i)slot:\s*slot{slot}.*?Activate:\s*(\w+)", output, re.DOTALL)
        return (match.group(1).lower() == "yes") if match else None

    def _parse_ssid_schedule_mode(self, output: str) -> Optional[bool]:
        """Extrait SSID_schedule_mode: yes/no depuis 'show wlan-ssid-profile <name>'."""
        match = re.search(r'(?i)SSID_schedule_mode:\s*(\w+)', output)
        if not match:
            return None
        return match.group(1).lower() == "yes"

    def _parse_port_status(self, output: str) -> dict[str, Any]:
        """Parse 'show port status' output.

        Les champs numériques (tx_rate/rx_rate/tx_bytes/rx_bytes) sont None si
        le format n'a pas pu être reconnu, jamais 0."""
        port: dict[str, Any] = {
            "status": None,
            "speed": None,
            "tx_bytes": None,
            "rx_bytes": None,
            "tx_rate": None,
            "rx_rate": None,
            "uptime": None,
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
                lambda: self._async_execute_session_direct(["reboot"], settle_delays=[CLI_SETTLE_CONFIG * 2]),
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
                lambda: self._async_execute_session_direct(["show wlan all"], settle_delays=[CLI_SETTLE_SLOW_READ]),
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
                    lambda: self._async_execute_session_direct(["show version"], settle_delays=[CLI_SETTLE_DEFAULT]),
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
        verify_delay = CLI_SETTLE_RADIO_VERIFY

        for attempt in (1, 2):
            settle_delays = [CLI_SETTLE_CONFIG] * 5 + [verify_delay]
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
            verify_delay += CLI_SETTLE_RADIO_VERIFY * 0.75  # un peu plus de marge au 2e essai

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
        settle_delays = [CLI_SETTLE_CONFIG] * 5

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
                lambda: self._async_execute_session_direct(["show wlan all"], settle_delays=[CLI_SETTLE_SLOW_READ]),
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
                    [f"show wlan-ssid-profile {ssid_name}"], settle_delays=[CLI_SETTLE_CONFIG * 2]
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

            base_delay = CLI_SETTLE_CONFIG
            write_delay = CLI_SETTLE_WRITE if persist else CLI_SETTLE_CONFIG
            verify_delay = CLI_SETTLE_RADIO_VERIFY * 0.75 + (attempt - 1) * (CLI_SETTLE_RADIO_VERIFY * 0.75)
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
