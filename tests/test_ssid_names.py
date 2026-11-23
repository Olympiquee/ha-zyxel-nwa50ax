"""Nom et sens des switches SSID (module pur ssid_names.py)."""
import itertools

import pytest

from ha_zyxel.ssid_names import (
    REVERSED_TAG,
    effective_state,
    guest_switch_label,
    inverted_profiles,
    schedule_switch_label,
    schedule_wanted,
    semantic_note,
)

NAMES = {"Home": "MyHome", "Guest": "MyHome_guest", "IoT": "MyHome_IoT"}


# --- switches de planning : nom -------------------------------------------

def test_schedule_label_uses_real_ssid_when_enabled():
    assert schedule_switch_label("Home", NAMES, True) == "SSID MyHome Schedule"


def test_schedule_label_keeps_profile_name_when_option_disabled():
    # Décoché = comportement historique, strictement inchangé.
    assert schedule_switch_label("Home", NAMES, False) == "SSID Home Schedule"


def test_schedule_label_falls_back_to_profile_when_real_name_unknown():
    assert schedule_switch_label("Studio", NAMES, True) == "SSID Studio Schedule"
    assert schedule_switch_label("Home", {}, True) == "SSID Home Schedule"


def test_schedule_label_disambiguates_profiles_sharing_one_ssid():
    names = {"Home24": "MyHome", "Home5": "MyHome", "IoT": "MyHome_IoT"}
    assert schedule_switch_label("Home24", names, True) == "SSID MyHome (Home24) Schedule"
    assert schedule_switch_label("Home5", names, True) == "SSID MyHome (Home5) Schedule"
    assert schedule_switch_label("IoT", names, True) == "SSID MyHome_IoT Schedule"


# --- switch Guest : nom -----------------------------------------------------

def test_guest_label_follows_same_rule_as_other_ssids():
    assert guest_switch_label("Guest", NAMES, True) == "SSID MyHome_guest Schedule"
    assert schedule_switch_label("Home", NAMES, True) == "SSID MyHome Schedule"


def test_guest_label_when_broadcast_ssid_is_simply_guest():
    assert guest_switch_label("Guest", {"Guest": "Guest"}, True) == "SSID Guest Schedule"


def test_guest_label_unchanged_when_option_disabled_or_unknown():
    assert guest_switch_label("Guest", NAMES, False) == "Guest SSID"
    assert guest_switch_label("Guest", {}, True) == "Guest SSID"


def test_guest_label_disambiguates_when_sharing_ssid_with_another_profile():
    names = {"Home": "MyHome", "Guest": "MyHome"}
    assert guest_switch_label("Guest", names, True) == "SSID MyHome (Guest) Schedule"


# --- étiquette [reversed] -----------------------------------------------------

def test_reversed_tag_is_appended_only_when_inverted():
    assert REVERSED_TAG == "[reversed]"
    assert schedule_switch_label("Home", NAMES, True, inverted=True) == "SSID MyHome Schedule [reversed]"
    assert schedule_switch_label("Home", NAMES, True, inverted=False) == "SSID MyHome Schedule"


@pytest.mark.parametrize("use_real", [True, False])
@pytest.mark.parametrize("names", [NAMES, {}])
def test_reversed_tag_present_for_every_naming_variant_of_guest(use_real, names):
    # Quel que soit le nom (vrai SSID, nom historique, SSID inconnu), un switch
    # inversé doit TOUJOURS le dire : c'est le but de l'étiquette.
    assert guest_switch_label("Guest", names, use_real, inverted=True).endswith(" [reversed]")
    assert not guest_switch_label("Guest", names, use_real, inverted=False).endswith("[reversed]")


def test_guest_historic_name_with_tag():
    assert guest_switch_label("Guest", {}, True, inverted=True) == "Guest SSID [reversed]"
    assert guest_switch_label("Guest", NAMES, False, inverted=True) == "Guest SSID [reversed]"


# --- inversion : états et commandes -----------------------------------------

def test_effective_state_truth_table():
    # normal : le switch reflète le planning ; inversé : l'inverse.
    assert effective_state(True, False) is True
    assert effective_state(False, False) is False
    assert effective_state(True, True) is False
    assert effective_state(False, True) is True


@pytest.mark.parametrize("inverted", [True, False])
def test_effective_state_unknown_stays_unknown(inverted):
    # L'inversion ne doit jamais fabriquer un état à partir de rien.
    assert effective_state(None, inverted) is None


def test_schedule_wanted_truth_table():
    assert schedule_wanted(True, False) is True     # normal : ON  -> planning actif
    assert schedule_wanted(False, False) is False   # normal : OFF -> always-on
    assert schedule_wanted(True, True) is False     # inversé: ON  -> always-on
    assert schedule_wanted(False, True) is True     # inversé: OFF -> planning actif


@pytest.mark.parametrize("schedule, inverted", list(itertools.product([True, False], [True, False])))
def test_commanding_the_displayed_state_leaves_schedule_unchanged(schedule, inverted):
    # Cohérence : si on demande le switch à l'état qu'il affiche déjà, le
    # planning voulu est celui qui existe déjà (aucune dérive possible).
    shown = effective_state(schedule, inverted)
    assert schedule_wanted(shown, inverted) is schedule


def test_semantic_note_describes_both_directions():
    assert semantic_note(False).startswith("ON = Schedule actif")
    assert semantic_note(True).startswith("ON = Always-on")
    assert semantic_note(True).endswith(REVERSED_TAG)


# --- profils inversés (valeur des options) --------------------------------

def test_inverted_profiles_defaults_to_guest_when_never_configured():
    # Défaut = comportement historique : aucun changement pour l'existant.
    assert inverted_profiles(None) == frozenset({"Guest"})


def test_inverted_profiles_explicit_empty_list_means_none_inverted():
    # Piège classique : [] est "faux" en Python. Un utilisateur qui décoche
    # tout veut AUCUN switch inversé, pas le retour au défaut.
    assert inverted_profiles([]) == frozenset()


def test_inverted_profiles_explicit_choice_is_kept_as_is():
    assert inverted_profiles(["Home", "IoT"]) == frozenset({"Home", "IoT"})
    assert "Guest" not in inverted_profiles(["Home"])


# --- choix de l'écran d'inversion ---------------------------------------------

from ha_zyxel.ssid_names import invert_choices  # noqa: E402


def test_invert_choices_lists_every_known_profile_with_its_real_ssid():
    choices = invert_choices(
        current=["Guest"],
        listed=["Guest", "Home", "IoT", "Studio"],
        schedules=["Guest", "Home", "IoT", "Studio"],
        names={"Home": "MyHome", "Guest": "MyHome_guest", "IoT": "MyHome_IoT", "Studio": "MyHome_Studio"},
    )
    assert [c["value"] for c in choices] == ["Guest", "Home", "IoT", "Studio"]
    assert [c["label"] for c in choices] == [
        "MyHome_guest (Guest)", "MyHome (Home)", "MyHome_IoT (IoT)", "MyHome_Studio (Studio)",
    ]


def test_invert_choices_does_not_depend_on_a_single_source():
    # Même si la liste de l'API est vide (cache pas encore rempli), les profils
    # lus par le cycle lent suffisent : on ne montre jamais "Guest seul" à tort.
    only_schedules = invert_choices(["Guest"], [], ["Guest", "Home"], {})
    assert [c["value"] for c in only_schedules] == ["Guest", "Home"]
    only_names = invert_choices(["Guest"], [], [], {"Home": "MyHome"})
    assert [c["value"] for c in only_names] == ["Guest", "Home"]


def test_invert_choices_keeps_already_saved_profiles_and_deduplicates():
    choices = invert_choices(["Home", "Old"], ["Home", "IoT"], ["Home"], {})
    assert [c["value"] for c in choices] == ["Home", "IoT", "Old"]
    assert [c["label"] for c in choices] == ["Home", "IoT", "Old"]      # pas de nom réel connu -> nom du profil


def test_invert_choices_label_has_no_redundant_parentheses():
    assert invert_choices([], ["Guest"], [], {"Guest": "Guest"})[0]["label"] == "Guest"


def test_invert_choices_sorts_case_insensitively():
    assert [c["value"] for c in invert_choices([], ["iot", "Home", "guest"], [], {})] == ["guest", "Home", "iot"]
