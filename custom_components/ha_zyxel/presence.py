"""Suivi de présence WiFi à partir des clients renvoyés par le groupe rapide.

Volontairement isolé du reste : ce composant ne fait AUCUN accès réseau, ni
SSH ni DNS - juste un cache en mémoire avec un délai de grâce anti-flapping.
Il est alimenté par __init__.py à chaque cycle RÉUSSI du groupe rapide, et
consulté par device_tracker.py.

Pourquoi un délai de grâce : certains appareils (smartphones en veille,
notamment) se déconnectent brièvement du WiFi pour économiser la batterie
sans avoir réellement quitté le logement. Sans délai de grâce, un
device_tracker basé strictement sur "présent dans le dernier cycle" ferait
clignoter home/absent à chaque micro-coupure. Le délai de grâce absorbe ça :
un appareil n'est marqué absent qu'après avoir manqué plusieurs cycles
d'affilée.

Ce même mécanisme gère naturellement une panne prolongée de l'AP : si le
groupe rapide échoue pendant plus longtemps que le délai de grâce, plus aucun
appareil n'est mis à jour et tous finissent par apparaître "absent" - sans
logique spécifique à écrire pour ce cas.
"""
import time
from typing import Any


class PresenceTracker:
    """Cache MAC -> dernière vue / dernières infos connues, avec délai de grâce."""

    def __init__(self, grace_period: float) -> None:
        self.grace_period = grace_period
        self._last_seen: dict[str, float] = {}
        self._last_info: dict[str, dict[str, Any]] = {}

    def update(self, clients: list[dict[str, Any]]) -> None:
        """À appeler à chaque cycle RÉUSSI du groupe rapide (pas en cas d'échec).

        Ne pas appeler sur un échec : le délai de grâce fait naturellement
        passer les appareils en "absent" si l'AP ne répond plus assez
        longtemps, sans traitement particulier à écrire ici pour ce cas.
        """
        now = time.monotonic()
        for client in clients:
            mac = client.get("mac")
            if not mac:
                continue
            self._last_seen[mac] = now
            self._last_info[mac] = client

    def is_connected(self, mac: str) -> bool:
        """True si vu dans le cycle courant ou pas plus vieux que le délai de grâce."""
        last = self._last_seen.get(mac)
        if last is None:
            return False
        return (time.monotonic() - last) < self.grace_period

    def get_info(self, mac: str) -> dict[str, Any]:
        """Dernières infos connues pour cette MAC (hostname, ip, ssid, band...)."""
        return self._last_info.get(mac, {})

    def get_display_name(self, mac: str) -> str:
        """Nom d'affichage : le hostname résolu, désambiguïsé si nécessaire.

        Plusieurs MAC connues peuvent légitimement partager le même hostname
        générique (ex: "iPhone" envoyé tel quel par iOS, combiné à la
        randomisation d'adresse MAC qui peut faire apparaître un même
        appareil - ou simplement deux appareils du même modèle - sous
        plusieurs identités). Sans ce garde-fou, deux entités device_tracker
        bien distinctes s'afficheraient sous un nom identique, rendant
        impossible de savoir laquelle est laquelle dans un automatisme ou un
        dashboard. On ne désambiguïse QUE s'il y a réellement collision, pour
        garder des noms propres dans le cas normal (pas de collision).
        """
        info = self._last_info.get(mac, {})
        hostname = info.get("hostname")
        if not hostname:
            return mac

        colliding = any(
            other_mac != mac and other_info.get("hostname") == hostname
            for other_mac, other_info in self._last_info.items()
        )
        if not colliding:
            return hostname

        suffix = ":".join(mac.split(":")[-2:])  # 2 derniers octets, ex: "1E:50"
        return f"{hostname} ({suffix})"

    def known_macs(self) -> set[str]:
        """Toutes les MAC déjà vues au moins une fois depuis le démarrage."""
        return set(self._last_seen.keys())
