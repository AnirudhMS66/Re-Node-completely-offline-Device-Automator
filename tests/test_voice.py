import base64
import subprocess
import sys

import pytest

import voice


def test_script_and_encoding_round_trip():
    script = voice.build_script(6.0)
    assert "FromSeconds(6)" in script and "Recognize(" in script and "'turn on wifi'" in script
    assert "set timer for" in script and "set alarm for" in script and "DictationGrammar" not in script   # fixed phrases only
    assert base64.b64decode(voice._encode(script)).decode("utf-16-le") == script
    assert "FromSeconds(2)" in voice.build_script(0.2)            # never below 2 s


def test_clean_and_result_split():
    assert voice.clean("  set   timer \r\n") == "set timer" and voice.clean("") is None and voice.clean(None) is None
    assert voice.clean("set alarm for seven thirty a m") == "set alarm for seven thirty am"
    assert voice.split_result("0.87|turn on wifi") == (0.87, "turn on wifi") and voice.split_result("0,91|mail") == (0.91, "mail")
    assert voice.split_result("garbage") is None and voice.split_result("x|y") is None


def test_number_words_cover_1_to_59_for_the_clock_parser():
    from clock_skill import parse_clock_command
    words = voice.number_words()
    assert len(words) == 59 and "twenty five" in words and "forty five" in words
    for w, n in (("five", 300), ("twenty five", 1500), ("fifty nine", 3540)):
        assert parse_clock_command(f"set timer for {w} minutes").seconds == n
    i = parse_clock_command(voice.clean("set alarm for seven thirty a m"))
    assert (i.hour, i.minute) == (7, 30)


def test_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert voice.is_available() is False
    with pytest.raises(RuntimeError):
        voice.listen_once(1)


def test_listen_once_windows_paths(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(voice, "_powershell", lambda: "powershell")
    assert voice.is_available() is True
    ok = subprocess.CompletedProcess([], 0, stdout="0.91|set timer for five minutes\r\n", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ok)
    assert voice.listen_once(5) == "set timer for five minutes"
    doubtful = subprocess.CompletedProcess([], 0, stdout="0.31|turn on wifi\r\n", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: doubtful)
    assert voice.listen_once(5) is None                      # low confidence is never acted on
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="\r\n", stderr=""))
    assert voice.listen_once(5) is None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="No audio input\n"))
    with pytest.raises(RuntimeError, match="No audio input"):
        voice.listen_once(5)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 1)
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="in time"):
        voice.listen_once(5)
