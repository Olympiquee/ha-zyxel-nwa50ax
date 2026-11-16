"""Configuration de référence de l'AP - logique pure, sans Home Assistant.

Contexte
--------
`write` enregistre TOUTE la configuration en cours (running-config) dans la
mémoire flash de l'AP. Or la CLI ne permet pas de lire le fichier enregistré
(`startup-config.conf` n'est lisible que par FTP). On ne peut donc pas comparer
"en cours" contre "enregistré" directement.

Principe retenu : une **configuration de référence**, c'est-à-dire une copie de
`show running-config` prise à un moment où l'on SAIT que la config en cours et
la config enregistrée sont identiques (juste après un `write`). Le bouton
compare ensuite la config actuelle à cette référence pour annoncer ce que le
prochain `write` va rendre permanent.

Deux garde-fous importants, déduits de vraies sorties de l'AP :

1. `show running-config` contient des SECRETS (hash du mot de passe admin, clés
   WiFi chiffrées). La référence est donc stockée MASQUÉE : chaque valeur
   secrète est remplacée par une empreinte courte. Un changement de clé reste
   détectable, mais aucun secret n'est stocké, journalisé ni affiché.
2. Une référence peut devenir obsolète si la config est enregistrée par un
   autre moyen (interface web...). La date de `startup-config.conf`, lue avec
   `dir /conf`, change à chaque enregistrement : si elle diffère de celle notée
   dans la référence, on le signale au lieu de présenter la comparaison comme
   fiable.

Ce module ne dépend ni de Home Assistant ni de paramiko, pour être testé
unitairement sur des captures réelles.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

BASELINE_VERSION = 1

# Une sortie plus courte, ou qui ne se termine pas par "!", est considérée
# comme tronquée : mieux vaut ne pas comparer que produire un faux diff.
MIN_COMPLETE_LINES = 20

MAX_GENERIC_LINES = 12


# ----------------------------------------------------------------------------
# Lecture : normalisation et masquage des secrets
# ----------------------------------------------------------------------------

# Un mot-clé annonce une valeur secrète si le TOKEN entier correspond. On vise
# les noms réellement vus (`encrypted-password`, `encrypted-wpa-psk`,
# `encrypted-psk`) et les noms usuels ailleurs dans cette famille de CLI.
# Volontairement strict sur le token entier : `group-key 30000` (intervalle,
# pas un secret) n'est pas masqué.
_SENSITIVE_TOKEN = re.compile(
    r"^(encrypted-.+|.*password.*|.*passphrase.*|.*secret.*|.*psk.*|community|"
    r"pre-shared-key|auth-key|priv-key|private-key|api-key|token)$",
    re.IGNORECASE,
)

# Ligne de prompt résiduelle (hostname personnalisé que _clean_output ne
# connaît pas) : jamais du contenu de configuration.
_PROMPT_LINE = re.compile(r"^[\w.\-]+(\(config[^)]*\))?[#>]\s*$")


def _fingerprint(value: str) -> str:
    return "<sha:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8] + ">"


def redact_line(line: str) -> str:
    """Remplace la valeur qui suit un mot-clé secret par une empreinte.

    `username admin encrypted-password $5$abc... user-type admin`
    devient `username admin encrypted-password <sha:1a2b3c4d> user-type admin`.
    L'indentation est conservée (elle sert à reconnaître les blocs).
    """
    indent = line[: len(line) - len(line.lstrip(" "))]
    tokens = line.strip().split()
    for index, token in enumerate(list(tokens[:-1])):
        if _SENSITIVE_TOKEN.match(token):
            tokens[index + 1] = _fingerprint(tokens[index + 1])
    return indent + " ".join(tokens)


def normalize(text: str) -> list[str]:
    """Sortie brute de `show running-config` -> lignes propres ET masquées."""
    lines: list[str] = []
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.rstrip()
        if not line.strip() or _PROMPT_LINE.match(line.strip()):
            continue
        lines.append(redact_line(line))
    return lines


def is_complete(lines: list[str]) -> bool:
    """Une config complète est assez longue et se termine par le "!" de fin."""
    return len(lines) >= MIN_COMPLETE_LINES and lines[-1].strip() == "!"


_DIR_STARTUP = re.compile(
    r"(?m)^\s*startup-config\.conf\s+(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s*$"
)


def parse_dir_conf(output: str) -> Optional[dict[str, Any]]:
    """Taille et date de `startup-config.conf` d'après `dir /conf`.

    L'ancrage `\\.conf\\s+` évite de confondre avec `startup-config.conf.md5`.
    """
    match = _DIR_STARTUP.search(output or "")
    if not match:
        return None
    return {"size": int(match.group(1)), "mtime": f"{match.group(2)} {match.group(3)}"}


# ----------------------------------------------------------------------------
# Comparaison par blocs
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    header: str
    kind: str  # block_added | block_removed | line_added | line_removed
    line: Optional[str] = None


def parse_blocks(lines: list[str]) -> dict[str, Counter]:
    """Découpe la config en blocs : {en-tête: lignes enfants}.

    Format de la CLI : une ligne non indentée ouvre un bloc, les lignes
    indentées en sont les enfants, et "!" le ferme. Les lignes isolées
    (`ip ssh server`...) sont des blocs sans enfant.

    Les enfants sont comptés dans un Counter, donc l'ORDRE des lignes dans un
    bloc est ignoré - important : l'AP réordonne parfois des lignes (par ex.
    `activate` n'est pas à la même place dans `wlan slot1` et `wlan slot2`).
    """
    blocks: dict[str, Counter] = {}
    current: Optional[str] = None
    for line in lines:
        if line.strip() == "!":
            current = None
        elif not line.startswith(" "):
            current = line.strip()
            blocks.setdefault(current, Counter())
        elif current is not None:
            blocks[current][line.strip()] += 1
    return blocks


def diff_configs(baseline_lines: list[str], current_lines: list[str]) -> list[Change]:
    """Différences entre la référence et la config actuelle, bloc par bloc."""
    old = parse_blocks(baseline_lines)
    new = parse_blocks(current_lines)
    changes: list[Change] = []

    for header, children in new.items():
        if header not in old:
            changes.append(Change(header, "block_added"))
            continue
        for line in (children - old[header]).elements():
            changes.append(Change(header, "line_added", line))
        for line in (old[header] - children).elements():
            changes.append(Change(header, "line_removed", line))

    for header in old:
        if header not in new:
            changes.append(Change(header, "block_removed"))
    return changes


# ----------------------------------------------------------------------------
# Rapport
# ----------------------------------------------------------------------------

@dataclass
class PendingReport:
    # unreadable : config actuelle illisible/incomplète - rien à comparer
    # no_baseline: pas de référence - comparaison impossible
    # stale      : la config enregistrée a changé depuis la référence
    # changes    : des changements sont en attente
    # none       : rien en attente
    status: str
    changes: list[Change] = field(default_factory=list)
    baseline_captured_at: Optional[str] = None


def build_report(
    baseline: Optional[Mapping[str, Any]],
    current_lines: Optional[list[str]],
    startup_meta: Optional[Mapping[str, Any]],
) -> PendingReport:
    if current_lines is None:
        return PendingReport("unreadable")
    if not baseline:
        return PendingReport("no_baseline")

    changes = diff_configs(baseline["lines"], current_lines)
    known = baseline.get("startup_mtime")
    now = (startup_meta or {}).get("mtime")
    stale = bool(known and now and known != now)

    if stale:
        status = "stale"
    else:
        status = "changes" if changes else "none"
    return PendingReport(status, changes, baseline.get("captured_at"))


def make_baseline(
    lines: list[str],
    startup_meta: Optional[Mapping[str, Any]],
    reason: str,
    captured_at: str,
) -> dict[str, Any]:
    return {
        "version": BASELINE_VERSION,
        "lines": list(lines),
        "startup_mtime": (startup_meta or {}).get("mtime"),
        "startup_size": (startup_meta or {}).get("size"),
        "captured_at": captured_at,
        "reason": reason,
    }


def baseline_from_store(data: Any) -> Optional[dict[str, Any]]:
    """Valide une référence relue depuis le stockage (jamais de confiance aveugle)."""
    if not isinstance(data, Mapping) or data.get("version") != BASELINE_VERSION:
        return None
    lines = data.get("lines")
    if not isinstance(lines, list) or not all(isinstance(item, str) for item in lines):
        return None
    if not is_complete(lines):
        return None
    return dict(data)


# ----------------------------------------------------------------------------
# Messages (fr / en)
# ----------------------------------------------------------------------------

_MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        "title_confirm": "Zyxel : enregistrer la configuration ?",
        "title_result": "Zyxel : enregistrement de la configuration",
        "unreadable": (
            "Je n'ai pas pu lire la configuration en cours de l'AP (AP injoignable ou "
            "lecture incomplète). Je ne peux donc pas lister ce qui va être enregistré."
        ),
        "no_baseline": (
            "Je n'ai pas encore de configuration de référence : je ne peux pas lister ce "
            "qui va être enregistré. Une référence sera créée juste après l'enregistrement, "
            "et les prochaines comparaisons seront détaillées."
        ),
        "none": (
            "Aucun changement en attente : la configuration en cours est identique à la "
            "dernière configuration enregistrée que je connais."
        ),
        "stale": (
            "La configuration enregistrée dans l'AP a changé depuis ma référence (sauvegarde "
            "depuis l'interface web, switch Guest ?). Les différences ci-dessous peuvent donc "
            "inclure des changements déjà enregistrés."
        ),
        "stale_no_diff": "Aucune différence détectée par rapport à ma référence.",
        "will_persist": (
            "Enregistrer rendra les changements suivants permanents (conservés après un "
            "redémarrage de l'AP) :"
        ),
        "radios_off": (
            "Attention : radio actuellement désactivée : {radios}. Si tu enregistres, elle "
            "restera désactivée après un redémarrage de l'AP."
        ),
        "confirm": (
            "**Appuie de nouveau sur le bouton dans les {seconds} s pour confirmer.** Sans "
            "nouvel appui, rien n'est enregistré. Le `write` fige toute la configuration en "
            "cours et peut geler l'AP pendant plusieurs dizaines de secondes."
        ),
        "result_ok": (
            "Configuration enregistrée dans la mémoire de l'AP. L'AP répondait de nouveau "
            "{seconds} s après l'envoi de la commande."
        ),
        "result_unresponsive": (
            "La commande d'enregistrement a été envoyée, mais l'AP ne répond pas encore "
            "{seconds} s plus tard. Patiente un peu, puis relance un rafraîchissement."
        ),
        "result_failed_session": (
            "Échec : la session SSH n'a pas pu être menée à terme. Rien n'est garanti : "
            "vérifie l'état de l'AP."
        ),
        "result_failed_rejected": "Échec : l'AP a refusé la commande `write`.",
        "baseline_ok": "La configuration de référence a été mise à jour.",
        "baseline_ko": (
            "La référence n'a pas pu être mise à jour : la prochaine comparaison sera moins précise."
        ),
        "block_added": "Ajouté : {header}",
        "block_removed": "Supprimé : {header}",
        "radio": "Radio {band} ({slot}) : {state}",
        "radio_on": "activée",
        "radio_off": "désactivée",
        "schedule": "Planning du SSID {name} : {state}",
        "schedule_on": "actif (suit ses horaires)",
        "schedule_off": "désactivé (SSID toujours actif)",
        "more": "… et {n} autre(s) modification(s)",
        "band1": "2,4 GHz",
        "band2": "5 GHz",
    },
    "en": {
        "title_confirm": "Zyxel: save the configuration?",
        "title_result": "Zyxel: saving the configuration",
        "unreadable": (
            "I could not read the AP's running configuration (AP unreachable or incomplete "
            "read), so I cannot list what is about to be saved."
        ),
        "no_baseline": (
            "I do not have a reference configuration yet, so I cannot list what is about to "
            "be saved. One will be created right after the save, and later comparisons will "
            "be detailed."
        ),
        "none": (
            "No pending change: the running configuration is identical to the last saved "
            "configuration I know of."
        ),
        "stale": (
            "The configuration saved in the AP has changed since my reference (saved from "
            "the web interface, Guest switch?). The differences below may therefore include "
            "changes that are already saved."
        ),
        "stale_no_diff": "No difference detected compared to my reference.",
        "will_persist": (
            "Saving will make the following changes permanent (kept after an AP restart):"
        ),
        "radios_off": (
            "Warning: radio currently disabled: {radios}. If you save, it will stay "
            "disabled after an AP restart."
        ),
        "confirm": (
            "**Press the button again within {seconds} s to confirm.** Without another "
            "press, nothing is saved. `write` freezes the whole running configuration and "
            "may freeze the AP for several tens of seconds."
        ),
        "result_ok": (
            "Configuration saved to the AP's memory. The AP was responding again {seconds} s "
            "after the command was sent."
        ),
        "result_unresponsive": (
            "The save command was sent, but the AP is still not responding {seconds} s later. "
            "Give it a moment, then trigger a refresh."
        ),
        "result_failed_session": (
            "Failed: the SSH session could not be completed. Nothing is guaranteed: check "
            "the AP's state."
        ),
        "result_failed_rejected": "Failed: the AP rejected the `write` command.",
        "baseline_ok": "The reference configuration has been updated.",
        "baseline_ko": (
            "The reference could not be updated: the next comparison will be less precise."
        ),
        "block_added": "Added: {header}",
        "block_removed": "Removed: {header}",
        "radio": "Radio {band} ({slot}): {state}",
        "radio_on": "enabled",
        "radio_off": "disabled",
        "schedule": "SSID {name} schedule: {state}",
        "schedule_on": "active (follows its hours)",
        "schedule_off": "disabled (SSID always on)",
        "more": "… and {n} more change(s)",
        "band1": "2.4 GHz",
        "band2": "5 GHz",
    },
}


def _messages(lang: Optional[str]) -> dict[str, str]:
    return _MESSAGES["fr" if str(lang or "").lower().startswith("fr") else "en"]


_SLOT_HEADER = re.compile(r"^wlan slot(\d+)$")
_SSID_HEADER = re.compile(r"^wlan-ssid-profile (.+)$")


def radio_label(slot: int, lang: Optional[str] = None) -> str:
    """Libellé d'une radio : slot1 = 2,4 GHz, slot2 = 5 GHz."""
    t = _messages(lang)
    return f"{t.get(f'band{slot}', f'slot{slot}')} (slot{slot})"


def describe_changes(changes: list[Change], lang: Optional[str] = None) -> list[str]:
    """Traduit les différences en phrases lisibles.

    Les deux cas que l'intégration provoque elle-même (radio et planning d'un
    SSID) ont une phrase dédiée ; le reste est listé tel quel (déjà masqué).
    """
    t = _messages(lang)
    radios: dict[int, bool] = {}
    schedules: dict[str, bool] = {}
    generic: list[str] = []

    for change in changes:
        is_line = change.kind in ("line_added", "line_removed")
        added = change.kind == "line_added"

        slot = _SLOT_HEADER.match(change.header)
        if slot and is_line and change.line in ("activate", "no activate"):
            radios[int(slot.group(1))] = (change.line == "activate") == added
            continue

        profile = _SSID_HEADER.match(change.header)
        if profile and is_line and change.line in ("ssid-schedule", "no ssid-schedule"):
            schedules[profile.group(1)] = (change.line == "ssid-schedule") == added
            continue

        if change.kind == "block_added":
            generic.append(t["block_added"].format(header=change.header))
        elif change.kind == "block_removed":
            generic.append(t["block_removed"].format(header=change.header))
        else:
            sign = "+" if added else "-"
            generic.append(f"{change.header} : {sign} {change.line}")

    messages: list[str] = []
    for slot_number in sorted(radios):
        state = t["radio_on"] if radios[slot_number] else t["radio_off"]
        band = t.get(f"band{slot_number}", f"slot{slot_number}")
        messages.append(t["radio"].format(band=band, slot=f"slot{slot_number}", state=state))
    for name in sorted(schedules):
        state = t["schedule_on"] if schedules[name] else t["schedule_off"]
        messages.append(t["schedule"].format(name=name, state=state))

    messages.extend(generic[:MAX_GENERIC_LINES])
    if len(generic) > MAX_GENERIC_LINES:
        messages.append(t["more"].format(n=len(generic) - MAX_GENERIC_LINES))
    return messages


def render_confirmation(
    report: PendingReport,
    radios_off: list[str],
    lang: Optional[str],
    window_seconds: int,
) -> tuple[str, str]:
    """(titre, message) de la notification affichée au premier appui."""
    t = _messages(lang)
    parts: list[str] = []

    if report.status == "unreadable":
        parts.append(t["unreadable"])
    elif report.status == "no_baseline":
        parts.append(t["no_baseline"])
    elif report.status == "none":
        parts.append(t["none"])
    else:  # changes | stale
        if report.status == "stale":
            parts.append(t["stale"])
        if report.changes:
            if report.status == "changes":
                parts.append(t["will_persist"])
            parts.append("\n".join(f"- {line}" for line in describe_changes(report.changes, lang)))
        elif report.status == "stale":
            parts.append(t["stale_no_diff"])

    # Sans diff fiable, on ne peut que signaler l'état courant des radios.
    if radios_off and report.status in ("unreadable", "no_baseline", "stale"):
        parts.append(t["radios_off"].format(radios=", ".join(radios_off)))

    parts.append(t["confirm"].format(seconds=window_seconds))
    return t["title_confirm"], "\n\n".join(parts)


def render_result(result: Mapping[str, Any], baseline_ok: bool, lang: Optional[str]) -> tuple[str, str]:
    """(titre, message) de la notification affichée après l'enregistrement."""
    t = _messages(lang)
    seconds = result.get("seconds")

    if not result.get("success"):
        key = "result_failed_rejected" if result.get("reason") == "rejected" else "result_failed_session"
        return t["title_result"], t[key]

    if not result.get("responsive"):
        return t["title_result"], t["result_unresponsive"].format(seconds=seconds)

    parts = [t["result_ok"].format(seconds=seconds)]
    parts.append(t["baseline_ok"] if baseline_ok else t["baseline_ko"])
    return t["title_result"], "\n\n".join(parts)
