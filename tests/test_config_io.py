"""API : lecture de la config de référence, write, écouteur de persistance.

La session SSH est simulée : on vérifie les commandes envoyées, la gestion des
sorties tronquées, et surtout les cas d'échec (jamais de succès affiché à tort).
"""
import asyncio
from pathlib import Path

import pytest

from ha_zyxel import zyxel_ssh_api
from ha_zyxel.const import CLI_SETTLE_SLOW_READ, CLI_SETTLE_WRITE
from ha_zyxel.zyxel_ssh_api import ZyxelSSHAPI

FIXTURES = Path(__file__).parent / "fixtures" / "v712"
RUNNING = (FIXTURES / "show_running_config.txt").read_text()
DIR_CONF = (FIXTURES / "dir_conf.txt").read_text()


class FakeSession:
    """Remplace _async_execute_session_direct : renvoie des sorties préparées."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []                      # [(commands, settle_delays)]

    async def __call__(self, commands, settle_delays=None, capture=True):
        self.calls.append((list(commands), list(settle_delays) if settle_delays else None))
        return self.replies.pop(0)


def run(make_coro):
    """Exécute un scénario avec une API neuve (file SSH liée à la boucle du test)."""
    async def scenario():
        api = ZyxelSSHAPI("10.0.20.2", "admin", "dummy-password")
        try:
            return await make_coro(api)
        finally:
            await api.async_shutdown()
    return asyncio.run(scenario())


# ---------------------------------------------------------------------------
# async_read_config_state
# ---------------------------------------------------------------------------

def test_read_config_state_returns_masked_lines_and_startup_meta():
    session = FakeSession([RUNNING, DIR_CONF])

    async def scenario(api):
        api._async_execute_session_direct = session
        return await api.async_read_config_state()

    state = run(scenario)
    assert state["startup"] == {"size": 8736, "mtime": "2026-10-22 09:10:18"}
    assert len(state["lines"]) > 200
    assert "FAKE" not in "\n".join(state["lines"])               # aucun secret ne sort de l'API
    assert session.calls[0][0] == ["show running-config", "dir /conf"]
    assert session.calls[0][1][0] == CLI_SETTLE_SLOW_READ


def test_read_config_state_retries_once_with_longer_settle_when_output_is_truncated():
    truncated = "\n".join(RUNNING.splitlines()[:60])
    session = FakeSession([truncated, DIR_CONF], [RUNNING, DIR_CONF])

    async def scenario(api):
        api._async_execute_session_direct = session
        return await api.async_read_config_state()

    state = run(scenario)
    assert state is not None and len(session.calls) == 2
    assert session.calls[1][1][0] > session.calls[0][1][0]       # 2e essai : délai plus long


def test_read_config_state_gives_up_rather_than_return_a_partial_config():
    truncated = "\n".join(RUNNING.splitlines()[:60])
    session = FakeSession([truncated, DIR_CONF], [truncated, DIR_CONF])

    async def scenario(api):
        api._async_execute_session_direct = session
        return await api.async_read_config_state()

    assert run(scenario) is None and len(session.calls) == 2


def test_read_config_state_does_not_retry_when_the_session_itself_failed():
    session = FakeSession(None)

    async def scenario(api):
        api._async_execute_session_direct = session
        return await api.async_read_config_state()

    assert run(scenario) is None and len(session.calls) == 1


# ---------------------------------------------------------------------------
# async_write_config
# ---------------------------------------------------------------------------

def test_write_config_waits_until_the_ap_responds_again(monkeypatch):
    monkeypatch.setattr(zyxel_ssh_api, "WRITE_RECOVERY_POLL_SECONDS", 0)
    session = FakeSession(["", "Building configuration... [OK]", ""])
    pings = iter([False, False, True])                            # AP gelée, puis de retour

    async def scenario(api):
        api._async_execute_session_direct = session

        async def responsive():
            return next(pings)

        api._is_ap_responsive = responsive
        return await api.async_write_config()

    result = run(scenario)
    assert result["success"] is True and result["responsive"] is True
    assert result["reason"] is None and result["seconds"] >= 0
    assert session.calls[0][0] == ["configure terminal", "write", "exit"]
    assert session.calls[0][1][1] == CLI_SETTLE_WRITE            # la durée d'attente du write est bien appliquée
    assert next(pings, "épuisé") == "épuisé"                     # 3 tests exactement, pas un de plus


def test_write_config_reports_rejection_without_pretending_success():
    session = FakeSession(["", "% Command not found\nERROR: Parse error/command not found!", ""])
    pinged = []

    async def scenario(api):
        api._async_execute_session_direct = session

        async def responsive():
            pinged.append(1)
            return True

        api._is_ap_responsive = responsive
        return await api.async_write_config()

    result = run(scenario)
    assert result == {"success": False, "responsive": False, "seconds": None, "reason": "rejected"}
    assert pinged == []                                          # inutile d'attendre l'AP


def test_write_config_reports_session_failure():
    async def scenario(api):
        api._async_execute_session_direct = FakeSession(None)
        return await api.async_write_config()

    assert run(scenario) == {"success": False, "responsive": False, "seconds": None, "reason": "session"}


def test_write_config_gives_up_waiting_but_still_reports_the_write_as_sent(monkeypatch):
    monkeypatch.setattr(zyxel_ssh_api, "WRITE_RECOVERY_MAX_SECONDS", 0)
    monkeypatch.setattr(zyxel_ssh_api, "WRITE_RECOVERY_POLL_SECONDS", 0)

    async def scenario(api):
        api._async_execute_session_direct = FakeSession(["", "OK", ""])

        async def never():
            return False

        api._is_ap_responsive = never
        return await api.async_write_config()

    result = run(scenario)
    assert result["success"] is True and result["responsive"] is False


# ---------------------------------------------------------------------------
# Écouteur de persistance (switch Guest : toggle + write)
# ---------------------------------------------------------------------------

def toggle_outputs(mode):
    """Sorties d'une session de toggle : 5 commandes + `show wlan-ssid-profile`."""
    return ["", "", "", "", "", f"ssid profile: Guest\n  SSID: MyHome_guest\n  SSID_schedule_mode: {mode}\n"]


def run_toggle(replies, persist, enable=True):
    calls = []

    async def scenario(api):
        api._async_execute_session_direct = FakeSession(*replies)
        api.add_persist_listener(lambda: calls.append("notified"))
        ok = await api.async_toggle_ssid_schedule("Guest", enable=enable, persist=persist)
        return ok

    return run(scenario), calls


def test_persist_listener_is_called_after_a_successful_persistent_toggle():
    ok, calls = run_toggle([toggle_outputs("yes")], persist=True, enable=True)
    assert ok is True and calls == ["notified"]


def test_persist_listener_is_not_called_for_a_non_persistent_toggle():
    ok, calls = run_toggle([toggle_outputs("yes")], persist=False, enable=True)
    assert ok is True and calls == []                             # rien n'a été enregistré -> rien à re-capturer


def test_persist_listener_is_not_called_when_the_toggle_failed():
    ok, calls = run_toggle([toggle_outputs("no"), toggle_outputs("no")], persist=True, enable=True)
    assert ok is False and calls == []


def test_a_failing_listener_never_breaks_the_toggle():
    async def scenario(api):
        api._async_execute_session_direct = FakeSession(toggle_outputs("yes"))

        def broken():
            raise RuntimeError("boom")

        api.add_persist_listener(broken)
        return await api.async_toggle_ssid_schedule("Guest", enable=True, persist=True)

    assert run(scenario) is True


def test_persist_listener_can_be_removed():
    async def scenario(api):
        api._async_execute_session_direct = FakeSession(toggle_outputs("yes"))
        calls = []
        remove = api.add_persist_listener(lambda: calls.append(1))
        remove()
        remove()                                                  # idempotent
        await api.async_toggle_ssid_schedule("Guest", enable=True, persist=True)
        return calls

    assert run(scenario) == []


# ---------------------------------------------------------------------------
# Nettoyage réel des sorties de session (pas simulé) : la config doit sortir intacte
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "prefix, suffix",
    [
        ("show running-config\r\n", "Router> "),            # écho seul + prompt final
        ("Router> show running-config\r\n", "\r\nRouter> "),  # prompt et écho sur la même ligne
    ],
)
def test_real_session_output_cleaning_keeps_the_whole_config_intact(prefix, suffix):
    from ha_zyxel.config_snapshot import is_complete, normalize

    api = ZyxelSSHAPI("10.0.20.2", "admin", "dummy-password")
    raw = prefix + RUNNING.replace("\n", "\r\n") + suffix
    cleaned = api._clean_output(raw, "show running-config")

    assert normalize(cleaned) == normalize(RUNNING)           # rien perdu, rien ajouté
    assert is_complete(normalize(cleaned))
