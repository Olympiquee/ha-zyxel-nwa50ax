"""Configuration de référence et bouton Write : orchestration (dépend de Home Assistant).

La logique pure (masquage des secrets, comparaison, messages) vit dans
`config_snapshot.py` ; ici : stockage, notifications, double appui, et le lien
avec l'API. Voir config_snapshot.py pour le principe et ses limites.

Cycle de vie de la référence
----------------------------
- Reprise juste après un `write` réussi (bouton, ou switch Guest qui écrit aussi) :
  la config en cours et la config enregistrée sont alors identiques.
- Jamais reprise "à froid" (au démarrage de l'intégration) : l'AP peut porter
  des changements temporaires inconnus, la référence serait fausse. Sans
  référence, le bouton le dit et n'invente aucune liste.
- Stockée MASQUÉE dans le stockage Home Assistant (aucun secret : voir
  config_snapshot.redact_line), supprimée avec l'entrée d'intégration.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Mapping, Optional

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .config_snapshot import (
    baseline_from_store,
    build_report,
    make_baseline,
    radio_label,
    render_confirmation,
    render_result,
)
from .const import DOMAIN, PRIORITY_BASELINE, PRIORITY_MANUAL, WRITE_CONFIRM_WINDOW_SECONDS

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


class ConfigBaseline:
    """Référence de configuration + machine à états du bouton Write (double appui)."""

    def __init__(self, hass: HomeAssistant, entry_id: str, api: Any) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._api = api
        self._store: Store = Store(hass, STORAGE_VERSION, self.storage_key(entry_id))
        self._baseline: Optional[dict[str, Any]] = None
        self._armed_until = 0.0
        self._confirm_until: Optional[str] = None
        self._busy = asyncio.Lock()
        self._capture_task: Optional[asyncio.Task] = None
        self._last_result: Optional[dict[str, Any]] = None
        self._notification_id = f"{DOMAIN}_write_config_{entry_id}"

    # -- stockage ---------------------------------------------------------

    @staticmethod
    def storage_key(entry_id: str) -> str:
        return f"{DOMAIN}.config_baseline.{entry_id}"

    @classmethod
    async def async_remove_stored(cls, hass: HomeAssistant, entry_id: str) -> None:
        """Supprime la référence quand l'entrée d'intégration est supprimée."""
        await Store(hass, STORAGE_VERSION, cls.storage_key(entry_id)).async_remove()

    async def async_load(self) -> None:
        """Relit la référence enregistrée (ignorée si elle est invalide)."""
        self._baseline = baseline_from_store(await self._store.async_load())

    def async_shutdown(self) -> None:
        """Annule une reprise de référence en cours (déchargement de l'entrée)."""
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()

    # -- état exposé au bouton -------------------------------------------

    @property
    def armed(self) -> bool:
        return time.monotonic() < self._armed_until

    @property
    def confirm_until(self) -> Optional[str]:
        """Date limite (UTC, ISO) du 2e appui pour la dernière demande, sinon None."""
        return self._confirm_until

    @property
    def reference_captured_at(self) -> Optional[str]:
        return (self._baseline or {}).get("captured_at")

    @property
    def last_result(self) -> Optional[dict[str, Any]]:
        return self._last_result

    # -- référence --------------------------------------------------------

    async def async_capture(self, reason: str, priority: int = PRIORITY_BASELINE) -> bool:
        """Lit la config en cours et la retient comme nouvelle référence.

        À n'appeler que lorsque l'on SAIT que la config en cours vient d'être
        enregistrée. Ne lève jamais : un échec laisse simplement l'ancienne
        référence en place (qui sera alors signalée périmée).
        """
        try:
            state = await self._api.async_read_config_state(priority)
            if state is None:
                _LOGGER.debug("Référence de configuration non mise à jour (lecture impossible)")
                return False
            self._baseline = make_baseline(
                state["lines"], state["startup"], reason, dt_util.utcnow().isoformat()
            )
            await self._store.async_save(self._baseline)
            _LOGGER.info("Configuration de référence mise à jour (%s)", reason)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Mise à jour de la configuration de référence échouée: %s", err)
            return False

    def schedule_capture(self, reason: str) -> None:
        """Programme une reprise en tâche de fond (écouteur synchrone de l'API)."""
        if self._capture_task and not self._capture_task.done():
            return
        self._capture_task = self._hass.async_create_background_task(
            self.async_capture(reason), name=f"{DOMAIN} config baseline ({reason})"
        )

    # -- bouton -----------------------------------------------------------

    async def async_press(self, radio_state: Optional[Mapping[str, Any]] = None) -> str:
        """Un appui sur le bouton Write. Retourne "armed" | "written" | "failed" | "busy".

        1er appui : compare la config actuelle à la référence et NOTIFIE ce que
                    le `write` rendrait permanent (rien n'est écrit).
        2e appui dans la fenêtre de confirmation : écrit réellement.
        """
        if self._busy.locked():
            _LOGGER.info("Appui ignoré : une opération sur la configuration est déjà en cours")
            return "busy"
        async with self._busy:
            if self.armed:
                self._armed_until = 0.0
                self._confirm_until = None
                return await self._write()
            return await self._preview(radio_state)

    def _radios_off(self, radio_state: Optional[Mapping[str, Any]]) -> list[str]:
        lang = self._hass.config.language
        return [
            radio_label(slot, lang)
            for slot in (1, 2)
            if (radio_state or {}).get(f"slot{slot}_active") is False
        ]

    async def _preview(self, radio_state: Optional[Mapping[str, Any]]) -> str:
        state = await self._api.async_read_config_state(PRIORITY_MANUAL)
        report = build_report(
            self._baseline,
            state["lines"] if state else None,
            state["startup"] if state else None,
        )
        title, message = render_confirmation(
            report, self._radios_off(radio_state), self._hass.config.language,
            WRITE_CONFIRM_WINDOW_SECONDS,
        )
        self._notify(title, message)
        self._armed_until = time.monotonic() + WRITE_CONFIRM_WINDOW_SECONDS
        self._confirm_until = (
            dt_util.utcnow() + timedelta(seconds=WRITE_CONFIRM_WINDOW_SECONDS)
        ).isoformat()
        _LOGGER.info("Write armé (%s) - confirmation attendue sous %ds", report.status, WRITE_CONFIRM_WINDOW_SECONDS)
        return "armed"

    async def _write(self) -> str:
        result = await self._api.async_write_config()
        baseline_ok = False
        if result["success"] and result["responsive"]:
            # L'AP vient d'enregistrer : config en cours == config enregistrée.
            baseline_ok = await self.async_capture("write")
        self._last_result = {
            **result,
            "baseline_updated": baseline_ok,
            "at": dt_util.utcnow().isoformat(),
        }
        title, message = render_result(result, baseline_ok, self._hass.config.language)
        self._notify(title, message)
        return "written" if result["success"] else "failed"

    def _notify(self, title: str, message: str) -> None:
        persistent_notification.async_create(
            self._hass, message, title=title, notification_id=self._notification_id
        )
