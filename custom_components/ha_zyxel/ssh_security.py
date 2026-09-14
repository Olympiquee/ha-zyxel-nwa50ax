"""Politique de clé hôte SSH TOFU (Trust On First Use).

Remplace `paramiko.AutoAddPolicy()`, qui accepte silencieusement N'IMPORTE
QUELLE clé hôte à chaque connexion - y compris si elle change entre deux
connexions (ce qui peut arriver lors d'une attaque MITM sur le LAN, pas
seulement lors d'un remplacement de matériel légitime).

Le compromis retenu ici (revue de code - point 2) : pas de gestion complète
de known_hosts, juste une empreinte mémorisée au premier contact et comparée
ensuite :

    première connexion
        -> empreinte inconnue -> on fait confiance, on mémorise
    connexions suivantes
        -> empreinte identique -> OK
        -> empreinte différente -> on refuse et on lève une erreur explicite

C'est le meilleur rapport sécurité/complexité pour un homelab sur un LAN
segmenté : ça ne protège pas contre un attaquant présent dès la toute
première connexion, mais ça détecte tout changement de clé après coup.
"""
import base64
import hashlib
import logging
from typing import Callable, Optional

import paramiko

_LOGGER = logging.getLogger(__name__)


def fingerprint_of(key: paramiko.PKey) -> str:
    """Empreinte SHA256 d'une clé hôte, au format lisible type OpenSSH."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class SSHHostKeyChangedError(Exception):
    """Levée quand la clé hôte SSH ne correspond plus à celle mémorisée.

    Peut signifier un remplacement légitime de l'équipement (reset,
    remplacement matériel) OU une interception de la connexion sur le LAN.
    Dans les deux cas, une action explicite de l'utilisateur est nécessaire
    plutôt que d'accepter silencieusement la nouvelle clé.
    """


class PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Politique TOFU : mémorise la clé au premier contact, la vérifie ensuite."""

    def __init__(
        self,
        get_pinned_fingerprint: Callable[[], Optional[str]],
        on_first_trust: Callable[[str], None],
    ) -> None:
        """
        `get_pinned_fingerprint` : renvoie l'empreinte déjà mémorisée, ou None
        si c'est la toute première connexion.
        `on_first_trust` : appelé avec la nouvelle empreinte lorsqu'on lui
        fait confiance pour la première fois (à l'appelant de la persister).
        """
        self._get_pinned_fingerprint = get_pinned_fingerprint
        self._on_first_trust = on_first_trust

    def missing_host_key(self, client, hostname, key) -> None:
        fingerprint = fingerprint_of(key)
        pinned = self._get_pinned_fingerprint()

        if pinned is None:
            _LOGGER.warning(
                "Nouvelle clé SSH mémorisée pour %s (première connexion) : %s",
                hostname, fingerprint,
            )
            self._on_first_trust(fingerprint)
            return

        if fingerprint != pinned:
            raise SSHHostKeyChangedError(
                f"La clé SSH de {hostname} a changé : attendu {pinned}, "
                f"reçu {fingerprint}. Vérifie qu'il ne s'agit pas d'une "
                f"interception, ou réinitialise l'empreinte mémorisée dans "
                f"les options de l'intégration si le changement est légitime "
                f"(remplacement matériel, reset usine)."
            )
        # Empreinte identique à celle déjà connue : rien à faire, c'est le cas normal.
