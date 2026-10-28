"""'turn on wifi' style commands switch the radio directly (instant), instead of the slow LLM / macro route."""

import pytest

from gui_app import toggles
from gui_app.workers import ToggleAgent
from tests.test_gui_logic import FakeShell


@pytest.mark.parametrize("text,expected", [
    ("turn on wifi", ("wifi", True)), ("Turn off Wi-Fi", ("wifi", False)), ("wifi on", ("wifi", True)), ("switch off the wifi", ("wifi", False)),
    ("enable bluetooth", ("bluetooth", True)), ("disable bluetooth", ("bluetooth", False)), ("bluetooth", ("bluetooth", None)),
    ("wifi", ("wifi", None)), ("toggle wifi", ("wifi", None)), ("turn on flashlight", ("flashlight", True)), ("torch off", ("flashlight", False)),
    ("please turn on wifi", ("wifi", True)),
])
def test_parse_spoken(text, expected):
    assert toggles.parse_spoken(text) == expected


@pytest.mark.parametrize("text", ["The White Fleet has", "mirror", "mail", "turn on the wifi hotspot please now", "open wifi settings", "set timer for 5 minutes", ""])
def test_parse_spoken_ignores_everything_else(text):
    assert toggles.parse_spoken(text) is None


def test_turn_on_wifi_is_direct_and_idempotent():
    shell, lines, seen = FakeShell(wifi="0"), [], []
    agent = ToggleAgent(shell, lambda lvl, msg: lines.append(msg), lambda: (lambda g: seen.append(g) or True))
    assert agent("turn on wifi") is True
    assert shell.vals["wifi_on"] == "1" and seen == []                  # never reached the LLM / macro agent
    n = len(shell.calls)
    assert agent("turn on wifi") is True                                # already on: no svc call, still success
    assert not any(c[:2] == ["svc", "wifi"] for c in shell.calls[n:])
    assert agent("turn off wifi") is True and shell.vals["wifi_on"] == "0"
    assert agent("wifi") is True and shell.vals["wifi_on"] == "1"       # bare word flips


def test_turn_off_bluetooth_works_without_the_heavy_agent():
    shell = FakeShell(bt="1")
    agent = ToggleAgent(shell, lambda *a: None, lambda: None)
    assert agent("turn off bluetooth") is True and shell.vals["bluetooth_on"] == "0"


def test_flashlight_want_uses_assumed_state():
    calls = []
    macro = lambda goal: calls.append(goal) or True
    r = toggles.toggle_flashlight(macro, None, lambda *a: None, want=True)
    assert r.ok and r.now is True and calls == ["flashlight"]
    r = toggles.toggle_flashlight(macro, True, lambda *a: None, want=True)       # already on (assumed)
    assert r.ok and len(calls) == 1
