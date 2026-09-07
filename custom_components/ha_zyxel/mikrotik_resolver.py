"""Résolveur de noms d'appareils basé sur les baux DHCP du MikroTik.

Pourquoi MikroTik et pas AdGuard :
Dans cette architecture, le MikroTik est lui-même le serveur DNS interrogé par
les clients (allow-remote-requests: yes) et c'est LUI qui interroge AdGuard en
amont. Vu depuis AdGuard, toutes les requêtes arrivent donc avec l'IP du
MikroTik comme "client" - AdGuard ne voit jamais l'IP individuelle d'un
appareil du LAN et ne peut donc pas fournir de mapping IP/MAC -> nom fiable.
Le DHCP (et donc le hostname envoyé par chaque client en option 12) est servi
par le MikroTik, qui est donc la seule source de vérité pertinente ici.

Ce module est volontairement TOTALEMENT indépendant de zyxel_ssh_api.py :
connexion SSH séparée, cycle de rafraîchissement séparé, cache séparé. Une
panne ou une mauvaise configuration du MikroTik ne peut jamais bloquer ou
ralentir la récupération des données de l'AP Zyxel. L'intégration Zyxel
consulte ce cache via un simple callback synchrone (get_hostname), jamais
via un appel réseau direct.
"""
import asyncio
import logging
import re
import time
from typing import Optional

from homeassistant.core import HomeAssistant

from .const import DEFAULT_MIKROTIK_REFRESH_INTERVAL, MIKROTIK_HOSTNAME_CACHE_TTL

_LOGGER = logging.getLogger(__name__)

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

# Format réel de '/ip dhcp-server lease print' (validé sur un export RouterOS) :
#   "0 10.0.20.2 5C:64:8E:F4:69:67 NWA50AX"          (bail statique/waiting)
#   "4 D 10.0.30.247 A4:E5:7C:0C:CF:E5 shellyplug..." (bail dynamique, flag D)
#   "1 10.0.40.2 AC:E2:D3:14:7E:44"                   (pas de host-name connu)
# Les lignes de commentaire RouterOS ("；；； Nom du bail") ne matchent pas et
# sont donc naturellement ignorées.
_LEASE_LINE_RE = re.compile(
    r'^\s*\d+\s+(?:[A-Z]+\s+)?'
    r'(\d{1,3}(?:\.\d{1,3}){3})\s+'
    r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})'
    r'(?:\s+(\S+))?\s*$'
)


class MikrotikHostnameResolver:
    """Maintient un cache MAC/IP -> hostname à partir des baux DHCP MikroTik."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        username: str,
        password: str,
        port: int = 22,
        refresh_interval: int = DEFAULT_MIKROTIK_REFRESH_INTERVAL,
    ) -> None:
        self.hass = hass
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.refresh_interval = refresh_interval

        self._cache: dict[str, dict[str, str]] = {"by_mac": {}, "by_ip": {}}
        self._last_refresh_ok: float = 0.0
        self._task: Optional[asyncio.Task] = None

        if not HAS_PARAMIKO:
            _LOGGER.error(
                "paramiko indisponible : le résolveur de noms MikroTik est désactivé"
            )

    async def async_start(self) -> None:
        """Démarre la boucle de rafraîchissement en tâche de fond."""
        if not HAS_PARAMIKO:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh_loop())
            # Un premier rafraîchissement immédiat pour ne pas attendre
            # `refresh_interval` avant d'avoir des noms disponibles.
            await self._async_refresh_once()

    async def async_shutdown(self) -> None:
        """Arrête proprement la boucle de rafraîchissement."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_interval)
            try:
                await self._async_refresh_once()
            except Exception as err:  # ce composant ne doit jamais lever plus haut
                _LOGGER.warning("MikroTik resolver: échec du rafraîchissement (%s)", err)

    async def _async_refresh_once(self) -> None:
        raw = await self.hass.async_add_executor_job(self._fetch_leases_sync)
        if raw is None:
            # Échec de connexion : on garde le cache précédent tel quel plutôt
            # que de le vider (mieux vaut un nom potentiellement un peu
            # périmé que plus de nom du tout à cause d'un souci ponctuel).
            return
        self._cache = self._parse_leases(raw)
        self._last_refresh_ok = time.monotonic()
        _LOGGER.debug(
            "MikroTik resolver: %d bail(s) résolu(s) par MAC, %d par IP",
            len(self._cache["by_mac"]), len(self._cache["by_ip"]),
        )

    def _fetch_leases_sync(self) -> Optional[str]:
        """Session SSH indépendante vers le MikroTik - jamais mêlée au flux Zyxel."""
        ssh = None
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
            # RouterOS accepte l'exécution de commande non-interactive via SSH.
            # 'without-paging' évite un pager "--More--" qui bloquerait
            # indéfiniment une session non-interactive sans personne pour
            # appuyer sur une touche.
            _stdin, stdout, _stderr = ssh.exec_command(
                "/ip dhcp-server lease print without-paging", timeout=15
            )
            return stdout.read().decode("utf-8", errors="ignore")
        except Exception as err:
            _LOGGER.warning("MikroTik resolver: connexion échouée (%s)", err)
            return None
        finally:
            if ssh:
                try:
                    ssh.close()
                except Exception:
                    pass

    def _parse_leases(self, raw: str) -> dict[str, dict[str, str]]:
        """Parse la sortie de '/ip dhcp-server lease print' (format court).

        Parsing ligne par ligne, validé contre un export réel de
        `/ip dhcp-server lease print` : gère le flag optionnel (D/vide), les
        baux sans host-name (colonne absente), et ignore naturellement les
        lignes de commentaire RouterOS ("；；； Nom du bail") et les en-têtes
        (Flags/Columns/#) puisqu'elles ne correspondent pas au motif attendu.

        Si un même MAC apparaît sur plusieurs lignes (ex: un appareil ayant à
        la fois un bail dynamique et une réservation statique par ailleurs),
        la dernière ligne rencontrée avec un host-name renseigné l'emporte -
        sans conséquence ici car ces doublons partagent le même nom en pratique.
        """
        result: dict[str, dict[str, str]] = {"by_mac": {}, "by_ip": {}}
        if not raw:
            return result

        for line in raw.splitlines():
            match = _LEASE_LINE_RE.match(line.rstrip("\r"))
            if not match:
                continue

            ip, mac, hostname = match.groups()
            if not hostname:
                continue  # bail sans host-name connu, rien à mettre en cache

            result["by_mac"][mac.upper()] = hostname
            result["by_ip"][ip] = hostname

        return result

    def get_hostname(self, mac: Optional[str], ip: Optional[str]) -> Optional[str]:
        """Lookup synchrone et non bloquant - utilisé comme callback par ZyxelSSHAPI.

        Ne fait AUCUN accès réseau : simple lecture du cache déjà construit
        par la boucle de fond. Retourne None si le cache n'a jamais pu être
        alimenté avec succès, ou si le dernier succès est trop ancien.
        """
        if self._last_refresh_ok == 0.0:
            return None
        if time.monotonic() - self._last_refresh_ok > MIKROTIK_HOSTNAME_CACHE_TTL:
            return None

        if mac:
            name = self._cache["by_mac"].get(mac.upper())
            if name:
                return name
        if ip:
            return self._cache["by_ip"].get(ip)
        return None
