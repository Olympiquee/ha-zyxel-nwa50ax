"""Configuration de référence de l'AP (module pur config_snapshot.py).

Les fixtures `show_running_config.txt` et `dir_conf.txt` reprennent la
structure réelle d'une capture de l'AP (firmware 7.12) ; secrets et noms ont
été remplacés par des valeurs factices (toutes les valeurs secrètes
commencent par "FAKE", ce qui permet de vérifier qu'aucune ne survit).
"""
from pathlib import Path

import pytest

from ha_zyxel.config_snapshot import (
    Change,
    PendingReport,
    baseline_from_store,
    build_report,
    describe_changes,
    diff_configs,
    is_complete,
    make_baseline,
    normalize,
    parse_blocks,
    parse_dir_conf,
    radio_label,
    redact_line,
    render_confirmation,
    render_result,
)

FIXTURES = Path(__file__).parent / "fixtures" / "v712"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture
def lines():
    return normalize(load("show_running_config.txt"))


# --- aides d'édition de config (simulent un changement sur l'AP) -------------

def remove_line(lines, header, line):
    out, current, removed = [], None, False
    for item in lines:
        if item.strip() == "!":
            current = None
        elif not item.startswith(" "):
            current = item.strip()
        elif current == header and item.strip() == line and not removed:
            removed = True
            continue
        out.append(item)
    assert removed, f"ligne {line!r} introuvable dans {header!r}"
    return out


def drop_block(lines, header):
    out, skipping, dropped = [], False, False
    for item in lines:
        if not item.startswith(" ") and item.strip() != "!":
            skipping = item.strip() == header
            dropped = dropped or skipping
        if skipping:
            if item.strip() == "!":
                skipping = False
            continue
        out.append(item)
    assert dropped, f"bloc {header!r} introuvable"
    return out


def move_to_end_of_block(lines, header, line):
    without = remove_line(lines, header, line)
    out, current, placed = [], None, False
    for index, item in enumerate(without):
        if current == header and item.strip() == "!" and not placed:
            out.append(f" {line}")
            placed = True
        if item.strip() == "!":
            current = None
        elif not item.startswith(" "):
            current = item.strip()
        out.append(item)
    assert placed
    return out


# ---------------------------------------------------------------------------
# Lecture : normalisation et secrets
# ---------------------------------------------------------------------------

def test_real_capture_is_normalised_and_recognised_as_complete(lines):
    assert len(lines) > 200
    assert is_complete(lines)


def test_no_secret_value_survives_normalisation(lines):
    # Le hash admin et TOUTES les clés WiFi/WDS (valeurs factices "FAKE...")
    # ne doivent apparaître nulle part dans ce qui sera stocké.
    joined = "\n".join(lines)
    assert "FAKE" not in joined
    # Les mots-clés restent (le diff doit voir QU'une clé a changé) : 6 secrets.
    assert joined.count("<sha:") == 6
    assert "encrypted-password <sha:" in joined
    assert "encrypted-wpa-psk <sha:" in joined


def test_non_secret_keys_are_not_masked(lines):
    # `group-key 30000` est un intervalle, pas un secret.
    assert " group-key 30000" in lines
    assert " idle 300" in lines


def test_redact_line_masks_only_the_value_and_keeps_the_rest():
    redacted = redact_line("username admin encrypted-password $5$abc$def user-type admin")
    assert redacted.startswith("username admin encrypted-password <sha:")
    assert redacted.endswith("> user-type admin")
    assert "$5$abc$def" not in redacted


def test_redact_line_keeps_indentation_and_is_deterministic():
    first = redact_line(" encrypted-wpa-psk ABC=DEF=")
    assert first.startswith(" encrypted-wpa-psk <sha:")
    assert first == redact_line(" encrypted-wpa-psk ABC=DEF=")           # même secret -> même empreinte
    assert first != redact_line(" encrypted-wpa-psk ABC=XYZ=")           # secret différent -> empreinte différente


def test_redact_line_leaves_ordinary_lines_untouched_and_tolerates_missing_value():
    assert redact_line(" band 2.4G 5G") == " band 2.4G 5G"
    assert redact_line(" encrypted-psk") == " encrypted-psk"            # mot-clé sans valeur : pas d'erreur


def test_normalize_drops_prompts_blank_lines_and_carriage_returns():
    raw = "Router> \r\n!\r\n\r\nhybrid-mode standalone\r\nRouter(config)# \r\nAP-Salon# \r\n"
    assert normalize(raw) == ["!", "hybrid-mode standalone"]


def test_is_complete_rejects_truncated_or_empty_output(lines):
    assert not is_complete(lines[:100])                      # coupée au milieu
    assert not is_complete(lines[:-1])                       # le "!" final manque
    assert not is_complete([])
    assert not is_complete(["!"] * 5)                        # trop courte


# ---------------------------------------------------------------------------
# dir /conf
# ---------------------------------------------------------------------------

def test_parse_dir_conf_reads_size_and_date_of_the_startup_file():
    assert parse_dir_conf(load("dir_conf.txt")) == {"size": 8736, "mtime": "2026-10-22 09:10:18"}


def test_parse_dir_conf_is_not_fooled_by_the_md5_companion_file():
    md5_first = (
        "startup-config.conf.md5                            74       2030-01-01 00:00:00\n"
        "startup-config.conf                                8736     2026-10-22 09:10:18\n"
    )
    assert parse_dir_conf(md5_first)["mtime"] == "2026-10-22 09:10:18"
    only_md5 = "startup-config.conf.md5                            74       2030-01-01 00:00:00\n"
    assert parse_dir_conf(only_md5) is None


@pytest.mark.parametrize("output", ["", None, "File Name  Size  Modified Time\n=====\n"])
def test_parse_dir_conf_returns_none_when_absent(output):
    assert parse_dir_conf(output) is None


# ---------------------------------------------------------------------------
# Comparaison
# ---------------------------------------------------------------------------

def test_identical_configs_have_no_difference(lines):
    assert diff_configs(lines, list(lines)) == []


def test_parse_blocks_groups_children_under_their_header(lines):
    blocks = parse_blocks(lines)
    assert blocks["wlan-ssid-profile Home"]["ssid-schedule"] == 1
    assert "ssid MyHome" in blocks["wlan-ssid-profile Home"]
    assert blocks["ip ssh server"] == {}                     # ligne isolée = bloc sans enfant


def test_disabling_a_schedule_is_exactly_one_line_difference(lines):
    # Reproduit ce que l'AP a réellement montré : seule la ligne " ssid-schedule" disparaît.
    current = remove_line(lines, "wlan-ssid-profile Home", "ssid-schedule")
    changes = diff_configs(lines, current)
    assert changes == [Change("wlan-ssid-profile Home", "line_removed", "ssid-schedule")]
    assert describe_changes(changes, "fr") == ["Planning du SSID Home : désactivé (SSID toujours actif)"]
    assert describe_changes(changes, "en") == ["SSID Home schedule: disabled (SSID always on)"]


def test_reenabling_a_schedule_is_described_as_active(lines):
    baseline = remove_line(lines, "wlan-ssid-profile Home", "ssid-schedule")
    changes = diff_configs(baseline, lines)
    assert describe_changes(changes, "fr") == ["Planning du SSID Home : actif (suit ses horaires)"]


@pytest.mark.parametrize("slot, label", [(1, "Radio 2,4 GHz (slot1) : désactivée"), (2, "Radio 5 GHz (slot2) : désactivée")])
def test_radio_turned_off_is_described_per_band(lines, slot, label):
    current = remove_line(lines, f"wlan slot{slot}", "activate")
    assert describe_changes(diff_configs(lines, current), "fr") == [label]


def test_radio_shown_as_explicit_no_activate_gives_a_single_message(lines):
    # Selon le firmware, une radio coupée peut apparaître en retirant "activate"
    # OU en ajoutant "no activate" : dans les deux cas une seule phrase.
    current = remove_line(lines, "wlan slot1", "activate")
    current = [*current[: current.index("wlan slot1") + 1], " no activate", *current[current.index("wlan slot1") + 1:]]
    messages = describe_changes(diff_configs(lines, current), "fr")
    assert messages == ["Radio 2,4 GHz (slot1) : désactivée"]


def test_line_order_inside_a_block_is_ignored(lines):
    # L'AP réordonne parfois les lignes : ce n'est pas un changement.
    reordered = move_to_end_of_block(lines, "wlan slot1", "activate")
    assert reordered != lines
    assert diff_configs(lines, reordered) == []


def test_added_and_removed_blocks_are_reported(lines):
    without_iot = drop_block(lines, "wlan-ssid-profile IoT")
    assert describe_changes(diff_configs(lines, without_iot), "fr") == ["Supprimé : wlan-ssid-profile IoT"]
    assert describe_changes(diff_configs(without_iot, lines), "fr") == ["Ajouté : wlan-ssid-profile IoT"]


def test_a_changed_wifi_key_is_detected_without_ever_exposing_it():
    old = load("show_running_config.txt")
    new = old.replace("FAKEHOMEPSK0001=", "FAKEHOMEPSK9999=")
    changes = diff_configs(normalize(old), normalize(new))
    assert {c.header for c in changes} == {"wlan-security-profile Securite_Home"}
    assert {c.kind for c in changes} == {"line_added", "line_removed"}
    text = "\n".join(describe_changes(changes, "fr"))
    assert "encrypted-wpa-psk <sha:" in text
    assert "FAKE" not in text


def test_generic_changes_are_capped_with_a_summary_line():
    changes = [Change("some block", "line_added", f"line {i}") for i in range(15)]
    messages = describe_changes(changes, "fr")
    assert len(messages) == 13
    assert messages[-1] == "… et 3 autre(s) modification(s)"


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

def baseline_of(lines, mtime="2026-10-22 09:10:18"):
    return make_baseline(lines, {"size": 8736, "mtime": mtime}, "write", "2026-10-22T09:10:30+00:00")


STARTUP = {"size": 8736, "mtime": "2026-10-22 09:10:18"}


def test_report_unreadable_when_current_config_could_not_be_read(lines):
    assert build_report(baseline_of(lines), None, STARTUP).status == "unreadable"


def test_report_without_baseline(lines):
    assert build_report(None, lines, STARTUP).status == "no_baseline"


def test_report_none_when_nothing_changed_and_reference_is_fresh(lines):
    report = build_report(baseline_of(lines), lines, STARTUP)
    assert report.status == "none" and report.changes == []


def test_report_lists_pending_changes_when_reference_is_fresh(lines):
    current = remove_line(lines, "wlan-ssid-profile Home", "ssid-schedule")
    report = build_report(baseline_of(lines), current, STARTUP)
    assert report.status == "changes"
    assert report.changes == [Change("wlan-ssid-profile Home", "line_removed", "ssid-schedule")]
    assert report.baseline_captured_at == "2026-10-22T09:10:30+00:00"


def test_report_stale_when_the_saved_file_changed_since_the_reference(lines):
    # Quelqu'un a enregistré depuis (interface web...) : la date du fichier a changé.
    later = {"size": 8751, "mtime": "2026-10-23 18:00:00"}
    assert build_report(baseline_of(lines), lines, later).status == "stale"
    current = remove_line(lines, "wlan-ssid-profile Home", "ssid-schedule")
    report = build_report(baseline_of(lines), current, later)
    assert report.status == "stale" and len(report.changes) == 1     # liste conservée, mais signalée


def test_report_is_not_stale_when_a_date_is_unknown(lines):
    # Date illisible d'un côté ou de l'autre : on ne prétend pas qu'elle a changé.
    assert build_report(baseline_of(lines, mtime=None), lines, STARTUP).status == "none"
    assert build_report(baseline_of(lines), lines, None).status == "none"


# ---------------------------------------------------------------------------
# Stockage
# ---------------------------------------------------------------------------

def test_baseline_survives_a_store_round_trip(lines):
    stored = baseline_of(lines)
    assert baseline_from_store(stored) == stored


def test_baseline_from_store_rejects_anything_dubious(lines):
    good = baseline_of(lines)
    assert baseline_from_store(None) is None
    assert baseline_from_store("n'importe quoi") is None
    assert baseline_from_store({**good, "version": 99}) is None
    assert baseline_from_store({**good, "lines": "pas une liste"}) is None
    assert baseline_from_store({**good, "lines": [*lines[:10], 42]}) is None
    assert baseline_from_store({**good, "lines": lines[:100]}) is None      # tronquée


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def test_radio_label():
    assert radio_label(1, "fr") == "2,4 GHz (slot1)"
    assert radio_label(2, "en") == "5 GHz (slot2)"
    assert radio_label(3, "fr") == "slot3 (slot3)"


def test_confirmation_lists_changes_and_explains_how_to_confirm(lines):
    current = remove_line(remove_line(lines, "wlan-ssid-profile Home", "ssid-schedule"), "wlan slot1", "activate")
    report = build_report(baseline_of(lines), current, STARTUP)
    title, message = render_confirmation(report, [], "fr", 60)
    assert title == "Zyxel : enregistrer la configuration ?"
    assert "permanents" in message
    assert "- Radio 2,4 GHz (slot1) : désactivée" in message
    assert "- Planning du SSID Home : désactivé (SSID toujours actif)" in message
    assert "60 s" in message and "geler" in message


def test_confirmation_without_baseline_warns_about_disabled_radios(lines):
    report = build_report(None, lines, STARTUP)
    _, message = render_confirmation(report, ["2,4 GHz (slot1)"], "fr", 60)
    assert "référence" in message
    assert "radio actuellement désactivée : 2,4 GHz (slot1)" in message


def test_confirmation_does_not_repeat_radio_hint_when_the_diff_already_covers_it(lines):
    current = remove_line(lines, "wlan slot1", "activate")
    report = build_report(baseline_of(lines), current, STARTUP)
    _, message = render_confirmation(report, ["2,4 GHz (slot1)"], "fr", 60)
    assert "Attention" not in message


def test_confirmation_when_nothing_is_pending(lines):
    _, message = render_confirmation(build_report(baseline_of(lines), lines, STARTUP), [], "fr", 60)
    assert "Aucun changement en attente" in message and "permanents" not in message


def test_confirmation_when_reference_is_stale_says_so(lines):
    current = remove_line(lines, "wlan-ssid-profile Home", "ssid-schedule")
    report = build_report(baseline_of(lines), current, {"size": 1, "mtime": "2030-01-01 00:00:00"})
    _, message = render_confirmation(report, [], "fr", 60)
    assert "a changé depuis ma référence" in message
    assert "- Planning du SSID Home" in message
    assert "permanents" not in message                        # on n'affirme pas ce qui serait permanent


def test_confirmation_when_current_config_is_unreadable():
    _, message = render_confirmation(PendingReport("unreadable"), ["5 GHz (slot2)"], "fr", 60)
    assert "pas pu lire" in message and "5 GHz (slot2)" in message


def test_confirmation_never_leaks_a_secret():
    old = load("show_running_config.txt")
    new = old.replace("FAKEGUESTPSK001=", "FAKEGUESTPSK777=")
    report = build_report(baseline_of(normalize(old)), normalize(new), STARTUP)
    for lang in ("fr", "en"):
        _, message = render_confirmation(report, [], lang, 60)
        assert "FAKE" not in message and "<sha:" in message


def test_confirmation_is_available_in_english(lines):
    current = remove_line(lines, "wlan-ssid-profile Home", "ssid-schedule")
    title, message = render_confirmation(build_report(baseline_of(lines), current, STARTUP), [], "en", 45)
    assert title == "Zyxel: save the configuration?"
    assert "Press the button again within 45 s" in message
    assert "- SSID Home schedule: disabled (SSID always on)" in message


def test_unknown_language_falls_back_to_english(lines):
    assert render_confirmation(PendingReport("none"), [], "de", 60)[0] == "Zyxel: save the configuration?"
    assert render_confirmation(PendingReport("none"), [], None, 60)[0] == "Zyxel: save the configuration?"


def test_result_messages():
    ok = {"success": True, "responsive": True, "seconds": 23.4, "reason": None}
    _, message = render_result(ok, True, "fr")
    assert "23.4 s" in message
    assert "La configuration de référence a été mise à jour." in message
    assert "moins précise" in render_result(ok, False, "fr")[1]

    slow = {"success": True, "responsive": False, "seconds": 120.0, "reason": None}
    assert "ne répond pas encore" in render_result(slow, False, "fr")[1]

    assert "refusé" in render_result({"success": False, "reason": "rejected"}, False, "fr")[1]
    assert "session SSH" in render_result({"success": False, "reason": "session"}, False, "fr")[1]
    assert "rejected" in render_result({"success": False, "reason": "rejected"}, False, "en")[1]
