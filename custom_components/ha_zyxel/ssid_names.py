"""Switches SSID : nom d'affichage et inversion - logique pure, sans Home Assistant.

Volontairement isolée de switch.py pour pouvoir être testée unitairement : les
règles de nommage (vrai SSID vs nom du profil, doublons, étiquette [reversed])
et d'inversion (quel état affiche le switch, quel planning appliquer) sont la
seule partie "intelligente" de cette fonctionnalité.

Vocabulaire :
- "profil"   = le nom du profil SSID côté AP (ex. "Home") - utilisé dans les
               commandes CLI, c'est lui qui identifie le switch.
- "SSID"     = le nom réellement diffusé par le WiFi (ex. "MyHome"), lu dans le
               champ `SSID:` de `show wlan-ssid-profile <profil>`.
- "planning" = le schedule du profil (`SSID_schedule_mode: yes|no`).

Sens des switches :
- normal   : ON = planning ACTIF (le SSID suit ses horaires)
             OFF = planning désactivé (SSID toujours actif)
- inversé  : ON = SSID toujours actif (planning désactivé)
             OFF = planning actif
  Un switch inversé affiche "[reversed]" dans son nom.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

# Par défaut seul Guest est inversé : c'est son comportement historique
# (ON = SSID toujours actif), conservé tel quel pour ne casser aucune automatisation.
DEFAULT_INVERTED_PROFILES: tuple[str, ...] = ("Guest",)

REVERSED_TAG = "[reversed]"


# ----------------------------------------------------------------------------
# Inversion
# ----------------------------------------------------------------------------

def inverted_profiles(stored: Optional[Iterable[str]]) -> frozenset[str]:
    """Profils inversés à partir de la valeur enregistrée dans les options.

    - None  : jamais configuré -> défaut (Guest inversé).
    - liste : choix explicite de l'utilisateur, y compris la liste VIDE
              ("je ne veux aucun switch inversé"), qui ne doit surtout pas
              retomber sur le défaut.
    """
    if stored is None:
        return frozenset(DEFAULT_INVERTED_PROFILES)
    return frozenset(stored)


def effective_state(schedule_enabled: Optional[bool], inverted: bool) -> Optional[bool]:
    """État AFFICHÉ par le switch, d'après l'état réel du planning.

    None (état inconnu) reste None : l'inversion ne doit jamais fabriquer un
    état à partir de rien.
    """
    if schedule_enabled is None:
        return None
    return schedule_enabled != inverted


def schedule_wanted(switch_on: bool, inverted: bool) -> bool:
    """Planning à appliquer sur l'AP quand l'utilisateur met le switch sur ON/OFF.

    True = planning actif (`ssid-schedule`), False = désactivé (`no ssid-schedule`).
    """
    return switch_on != inverted


def semantic_note(inverted: bool) -> str:
    """Légende du sens du switch, pour ses attributs."""
    if inverted:
        return "ON = Always-on (24/7), OFF = Schedule actif (auto on/off) [reversed]"
    return "ON = Schedule actif (auto on/off), OFF = Always-on (24/7)"


def invert_choices(
    current: Iterable[str],
    listed: Iterable[str],
    schedules: Iterable[str],
    names: Mapping[str, str],
) -> list[dict[str, str]]:
    """Options de l'écran « Inverser des switches » : TOUS les profils connus.

    On réunit toutes les sources (liste de l'API, planning lu par le cycle lent,
    noms diffusés, choix déjà enregistrés) : l'écran ne dépend ainsi d'aucune
    source unique qui pourrait être vide à un instant donné. Le libellé montre
    le vrai SSID : "MyHome (Home)".
    """
    profiles = set(current) | set(listed) | set(schedules) | set(names)
    options = []
    for profile in sorted(profiles, key=lambda p: (p.lower(), p)):
        real = names.get(profile)
        label = f"{real} ({profile})" if real and real != profile else profile
        options.append({"value": profile, "label": label})
    return options


# ----------------------------------------------------------------------------
# Noms
# ----------------------------------------------------------------------------

def _tag(label: str, inverted: bool) -> str:
    return f"{label} {REVERSED_TAG}" if inverted else label


def schedule_switch_label(
    profile: str,
    names: Mapping[str, str],
    use_real: bool,
    inverted: bool = False,
) -> str:
    """Nom du switch de planning d'un SSID.

    - option désactivée, ou SSID réel inconnu -> "SSID <profil> Schedule"
      (comportement historique)
    - sinon -> "SSID <ssid réel> Schedule"
    - si deux profils diffusent le même SSID réel (ex. un profil par bande),
      le profil est ajouté entre parenthèses pour que les noms restent
      distincts : "SSID <ssid réel> (<profil>) Schedule".
    - si le switch est inversé, " [reversed]" est ajouté à la fin.
    """
    base = f"SSID {profile} Schedule"
    real = names.get(profile) if use_real else None
    if real:
        shared = sum(1 for value in names.values() if value == real) > 1
        shown = f"{real} ({profile})" if shared else real
        base = f"SSID {shown} Schedule"
    return _tag(base, inverted)


def guest_switch_label(
    profile: str,
    names: Mapping[str, str],
    use_real: bool,
    inverted: bool = False,
) -> str:
    """Nom du switch Guest : MÊME règle que les autres SSID.

    - option activée et SSID réel connu -> "SSID <ssid réel> Schedule"
      (ex. "SSID MyHome_guest Schedule", ou "SSID Guest Schedule" si le SSID
      diffusé s'appelle simplement "Guest")
    - option désactivée, ou SSID réel inconnu -> "Guest SSID" (nom historique)
    - si le switch est inversé (cas par défaut du Guest), " [reversed]" est
      ajouté à la fin.
    """
    real: Optional[str] = names.get(profile)
    if not use_real or not real:
        return _tag("Guest SSID", inverted)
    return schedule_switch_label(profile, names, True, inverted)
