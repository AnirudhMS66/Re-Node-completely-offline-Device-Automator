"""Unit tests for the control center's logic. No display, no phone, no tkinter needed."""

import json
import queue
import time

import pytest

from gui_app.log_bus import BrainTracker, LogBus, NoteTracker, classify, parse_raw, parse_step
from gui_app.state import CommandHistory, ExposureTimer, format_mmss
from gui_app.workers import AgentRunner, Poller, parse_wifi_on, parse_wm_size, pick_serial, probe_ollama


# ---------------- log line parsing ----------------
def test_parse_raw_splits_fields():
    (line,) = parse_raw("12:34:56|INFO|[BRAIN] Input received: 'mail'")
    assert (line.time, line.level, line.tag) == ("12:34:56", "INFO", "brain")
    assert line.message == "[BRAIN] Input received: 'mail'"


def test_parse_raw_message_may_contain_pipes():
    (line,) = parse_raw("01:02:03|INFO|[NOTE]   | a | b")
    assert line.message == "[NOTE]   | a | b"


def test_parse_raw_traceback_becomes_continuation_lines():
    lines = parse_raw("01:02:03|ERROR|[FAILED] boom\nTraceback (most recent call last):\n  File x\nValueError: bad")
    assert len(lines) == 4 and not lines[0].continuation and all(l.continuation for l in lines[1:])
    assert all(l.tag == "error" for l in lines)


def test_parse_raw_unformatted_text_is_kept():
    (line,) = parse_raw("just some text")
    assert line.message == "just some text" and line.level == "INFO"


@pytest.mark.parametrize("level,message,tag", [
    ("INFO", "[BRAIN] Reasoning: x", "brain"),
    ("INFO", "[STEP 2/3] Opening", "step"),
    ("INFO", "[NOTE]   | hi", "note"),
    ("INFO", "[WIFI] Wi-Fi is ON", "wifi"),
    ("WARNING", "slow response", "warning"),
    ("ERROR", "anything", "error"),
    ("INFO", "[FAILED] mail: x", "error"),
    ("INFO", "[DONE] ok", "done"),
    ("WARNING", "[SAFETY] forcing Wi-Fi OFF", "safety"),
    ("DEBUG", "raw output", "debug"),
    ("INFO", "[ACT] Tapping", "info"),
    ("INFO", "--- Step 2/10 ---", "step"),
])
def test_classify(level, message, tag):
    assert classify(level, message) == tag


# ---------------- step progress ----------------
def test_parse_step_workflow_and_reactive():
    assert parse_step("[STEP 3/11] Opening the first note") == (3, 11, "Opening the first note")
    assert parse_step("[STEP 1/3] Local brain reasoning") == (1, 3, "Local brain reasoning")
    assert parse_step("--- Step 4/10 ---") == (4, 10, "LLM reasoning step")
    assert parse_step("[BRAIN] Intent detected") is None
    assert parse_step("Step without brackets 3/4") is None


# ---------------- brain / note trackers ----------------
BRAIN_LINES = [
    "[BRAIN] Input received: 'mail'",
    "[BRAIN] Querying local model 'gemma:2b' at http://localhost:11434 (offline, no cloud)...",
    "[BRAIN] Intent detected: MAIL (confidence 0.93, source=ollama:gemma:2b, 1.2s)",
    "[BRAIN] Reasoning: Send the first note.",
    "[BRAIN] Execution plan:",
    "[BRAIN]   1. Open Notes",
    "[BRAIN]   2. Open Gmail",
]


def test_brain_tracker_builds_card():
    t = BrainTracker()
    assert all(t.feed(m) for m in BRAIN_LINES)
    s = t.state
    assert (s.command, s.model, s.intent, s.confidence, s.elapsed_s) == ("mail", "gemma:2b", "MAIL", 0.93, 1.2)
    assert s.source == "ollama:gemma:2b" and not s.is_fallback
    assert s.reasoning == "Send the first note." and s.plan == ["Open Notes", "Open Gmail"]


def test_brain_tracker_flags_fallback_and_resets_per_command():
    t = BrainTracker()
    t.feed("[BRAIN] Input received: 'mirror'")
    t.feed("[BRAIN] Intent detected: MIRROR (confidence 1.00, source=deterministic-fallback, 0.0s)")
    assert t.state.is_fallback and "FALLBACK" in t.state.source_tag
    t.feed("[BRAIN] Input received: 'mail'")
    assert t.state.intent == "" and t.state.command == "mail"       # fresh card


def test_brain_tracker_ignores_other_lines():
    t = BrainTracker()
    assert t.feed("[STEP 1/3] x") is False and t.feed("random") is False


def test_brain_tracker_records_gate_disagreement():
    t = BrainTracker()
    t.feed("[BRAIN] Model said OTHER but the deterministic command gate matched MAIL; proceeding with MAIL.")
    assert t.state.disagreement.startswith("Model said OTHER")


def test_note_tracker_collects_and_replaces():
    n = NoteTracker()
    for m in ["[NOTE] Copied note content (stored in Python variable + device clipboard):",
              "[NOTE]   | line one", "[NOTE]   | line two", "[NOTE]   | "]:
        assert n.feed(m)
    assert n.text == "line one\nline two\n"
    n.end_block()
    n.feed("[NOTE]   | next note")
    assert n.text == "next note"
    assert n.feed("[STEP 4/11] Pressing Home") is False


# ---------------- log bus ----------------
def test_logbus_drain_is_bounded_and_ordered():
    bus = LogBus()
    for i in range(10):
        bus.emit("INFO", f"msg {i}")
    first = bus.drain(max_items=4)
    rest = bus.drain()
    assert [l.message for l in first] == [f"msg {i}" for i in range(4)]
    assert len(rest) == 6 and bus.drain() == []


def test_logbus_loguru_sink_roundtrip_and_detach():
    from loguru import logger
    bus = LogBus()
    assert bus.attach_loguru(logger)
    logger.info("[WIFI] hello from loguru")
    logger.debug("debug line")
    lines = bus.drain()
    assert any(l.message == "[WIFI] hello from loguru" and l.tag == "wifi" for l in lines)
    assert any(l.level == "DEBUG" for l in lines)
    bus.detach()
    logger.info("after detach")
    assert bus.drain() == []


# ---------------- exposure timer ----------------
def test_exposure_basic_window():
    t = ExposureTimer()
    t.update(False, 100.0)
    t.update(True, 110.0)
    assert t.is_on and t.current_s(117.0) == 7.0 and format_mmss(t.current_s(117.0)) == "00:07"
    t.update(True, 120.0)                      # repeated ON keeps the same window
    t.update(False, 121.2)
    assert not t.is_on and t.last_window_s == pytest.approx(11.2)
    assert t.total_s(500.0) == pytest.approx(11.2)


def test_exposure_multiple_windows_total():
    t = ExposureTimer()
    for state, now in [(True, 0), (False, 5), (True, 10), (False, 13), (True, 20)]:
        t.update(state, now)
    assert t.windows == [5, 3] and t.last_window_s == 3
    assert t.total_s(24) == pytest.approx(5 + 3 + 4)       # open window counted live
    assert t.labels(24)["windows"] == "3"


def test_exposure_unknown_state_does_not_close_or_open_window():
    t = ExposureTimer()
    t.update(True, 0)
    t.update(None, 5)                           # unplugged / adb hiccup
    assert t.is_on and t.windows == []
    t.update(False, 8)
    assert t.last_window_s == 8
    t2 = ExposureTimer()
    t2.update(None, 3)
    assert not t2.is_on and t2.state is None


def test_exposure_labels_when_never_on():
    labels = ExposureTimer().labels(0)
    assert labels == {"current": "--:--", "last": "none yet", "total": "0.0 s", "windows": "0"}


def test_format_mmss():
    assert format_mmss(0) == "00:00" and format_mmss(65.9) == "01:05" and format_mmss(-3) == "00:00"


def test_command_history_up_down_and_persistence(tmp_path):
    path = tmp_path / "gui_history.json"
    h = CommandHistory(path)
    for c in ["mirror", "open settings", "mail", "mail", "  "]:
        h.add(c)
    assert h.entries == ["mirror", "open settings", "mail"]         # no consecutive dupes, no blanks
    assert [h.previous(), h.previous(), h.previous(), h.previous()] == ["mail", "open settings", "mirror", "mirror"]
    assert [h.next(), h.next(), h.next(), h.next()] == ["open settings", "mail", "", ""]
    assert CommandHistory(path).entries == ["mirror", "open settings", "mail"]
    assert CommandHistory(tmp_path / "none.json").previous() is None


# ---------------- workers: parsing helpers ----------------
def test_parse_wifi_matches_dispatcher_mapping():
    assert [parse_wifi_on(v) for v in ("1", "2", "0", "3", "null", "", "1\n")] == [True, True, False, False, None, None, True]


def test_parse_wm_size_override_wins():
    assert parse_wm_size("Physical size: 1080x2400") == "1080x2400"
    assert parse_wm_size("Physical size: 1080x2400\nOverride size: 720x1600") == "720x1600"
    assert parse_wm_size("") == ""


def test_pick_serial():
    devs = [("AAA", "unauthorized"), ("BBB", "device")]
    assert pick_serial(devs, "") == ("BBB", "")
    assert pick_serial(devs, "BBB") == ("BBB", "")
    assert pick_serial(devs, "AAA")[0] == "" and "unauthorized" in pick_serial(devs, "AAA")[1]
    assert pick_serial(devs, "ZZZ")[0] == ""
    assert pick_serial([], "") == ("", "no device found by adb")
    assert "accept the USB-debugging prompt" in pick_serial([("AAA", "unauthorized")], "")[1]


def test_probe_ollama_down_returns_status_not_exception():
    status = probe_ollama("http://127.0.0.1:9", "gemma:2b", timeout=0.5)   # port 9: nothing listens
    assert status.reachable is False and status.model == "gemma:2b"


# ---------------- workers: runner + poller ----------------
def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_runner_one_at_a_time_and_reports_result():
    events: queue.Queue = queue.Queue()
    gate = __import__("threading").Event()
    runner = AgentRunner(lambda goal: gate.wait(2) or True, events)
    assert runner.start("mail") is True and runner.busy
    assert runner.start("mirror") is False                 # double-click / second command refused
    gate.set()
    kind, result = events.get(timeout=2)
    assert kind == "run_done" and result.goal == "mail" and result.ok and not runner.busy
    assert runner.start("   ") is False                    # blank goal ignored


def test_runner_catches_exceptions_and_false_results():
    events: queue.Queue = queue.Queue()

    def boom(goal):
        raise RuntimeError("phone unplugged")

    runner = AgentRunner(boom, events)
    runner.start("x")
    _, result = events.get(timeout=2)
    assert result.ok is False and "RuntimeError: phone unplugged" in result.error and "Traceback" in result.details
    assert _wait(lambda: not runner.busy)

    runner2 = AgentRunner(lambda g: False, events)
    runner2.start("y")
    _, r2 = events.get(timeout=2)
    assert r2.ok is False and "see log" in r2.error


def test_runner_catches_systemexit():
    events: queue.Queue = queue.Queue()
    runner = AgentRunner(lambda g: (_ for _ in ()).throw(SystemExit(1)), events)
    runner.start("x")
    _, result = events.get(timeout=2)
    assert result.ok is False and "SystemExit" in result.error


def test_poller_reports_pauses_and_survives_probe_errors():
    events: queue.Queue = queue.Queue()
    state = {"paused": False, "n": 0}

    def probe():
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("adb gone")
        return state["n"]

    p = Poller("t", 0.02, probe, events, paused=lambda: state["paused"])
    p.start()
    first = events.get(timeout=2)
    assert first[0] == "t" and isinstance(first[1], OSError)
    assert events.get(timeout=2)[1] == 2                    # thread survived the exception
    state["paused"] = True
    time.sleep(0.1)
    calls_when_paused = state["n"]
    time.sleep(0.15)
    assert state["n"] == calls_when_paused                  # probe not called while paused
    assert any(e[1] == "paused" for e in list(events.queue))
    p.stop()


def test_demo_agent_logs_expected_prefixes_and_restores_wifi():
    from gui_app.demo import DemoPhone, make_demo_agent
    bus = LogBus()
    phone = DemoPhone()
    agent = make_demo_agent(phone, bus.emit, speed=0.0)
    assert agent("mail") is True and phone.wifi_state() is False
    msgs = [l.message for l in bus.drain(1000)]
    assert any(m.startswith("[STEP 11/11]") for m in msgs) and any("[NOTE]   |" in m for m in msgs)
    assert agent("fail") is False
    with pytest.raises(RuntimeError):
        agent("crash")


# ---------------- Ollama auto-start ----------------
from gui_app import workers as W_
from gui_app.workers import OllamaStatus, ensure_ollama, is_local_endpoint, stop_process


class FakeProc:
    def __init__(self, exits_with=None):
        self.returncode = exits_with
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.terminated = True


def _status(up):
    return OllamaStatus(up, "gemma:2b")


def test_is_local_endpoint():
    assert all(is_local_endpoint(e) for e in ("http://localhost:11434", "http://127.0.0.1:11434"))
    assert not is_local_endpoint("http://192.168.1.9:11434") and not is_local_endpoint("https://example.com")


def test_ensure_ollama_already_running_starts_nothing():
    def no_popen(*a, **k):
        raise AssertionError("must not start a second server")
    r = ensure_ollama("http://localhost:11434", "gemma:2b", popen=no_popen, probe=lambda: _status(True))
    assert r.status == "already_running" and r.process is None and r.ready


def test_ensure_ollama_starts_and_waits_until_reachable(monkeypatch):
    monkeypatch.setattr(W_, "find_ollama_exe", lambda: "/usr/bin/ollama")
    calls = {"n": 0, "cmd": None}

    def probe():
        calls["n"] += 1
        return _status(calls["n"] >= 4)             # comes up on the 4th probe

    def popen(cmd, **k):
        calls["cmd"] = cmd
        return FakeProc()

    r = ensure_ollama("http://localhost:11434", "gemma:2b", wait_s=5, popen=popen, sleep=lambda s: None, probe=probe)
    assert r.status == "started" and r.process is not None and calls["cmd"] == ["/usr/bin/ollama", "serve"]


def test_ensure_ollama_not_installed_and_remote(monkeypatch):
    monkeypatch.setattr(W_, "find_ollama_exe", lambda: None)
    r = ensure_ollama("http://localhost:11434", "m", probe=lambda: _status(False))
    assert r.status == "not_installed" and not r.ready and "ollama.com" in r.message
    r = ensure_ollama("http://10.0.0.5:11434", "m", probe=lambda: _status(False))
    assert r.status == "remote"


def test_ensure_ollama_popen_failure_and_early_exit(monkeypatch):
    monkeypatch.setattr(W_, "find_ollama_exe", lambda: "ollama")

    def bad_popen(*a, **k):
        raise OSError("blocked")
    assert ensure_ollama("http://localhost:11434", "m", popen=bad_popen, probe=lambda: _status(False)).status == "failed"

    # `ollama serve` exits at once (port taken by the tray app) and the server is up a moment later
    seq = iter([False, False, True])
    r = ensure_ollama("http://localhost:11434", "m", popen=lambda *a, **k: FakeProc(exits_with=1),
                      sleep=lambda s: None, probe=lambda: _status(next(seq)))
    assert r.status == "already_running" and r.process is None
    # ...and one that exits and stays down
    r = ensure_ollama("http://localhost:11434", "m", popen=lambda *a, **k: FakeProc(exits_with=1),
                      sleep=lambda s: None, probe=lambda: _status(False))
    assert r.status == "failed" and "code 1" in r.message


def test_ensure_ollama_timeout_returns_failed_but_keeps_process(monkeypatch):
    monkeypatch.setattr(W_, "find_ollama_exe", lambda: "ollama")
    r = ensure_ollama("http://localhost:11434", "m", wait_s=0.05, popen=lambda *a, **k: FakeProc(),
                      sleep=lambda s: time.sleep(0.02), probe=lambda: _status(False))
    assert r.status == "failed" and r.process is not None and not r.ready


def test_stop_process_only_terminates_what_it_is_given():
    stop_process(None)                                   # no-op
    p = FakeProc()
    stop_process(p)
    assert p.terminated


def test_ensure_ollama_integration_with_fake_binary(tmp_path, monkeypatch):
    """A real subprocess: a fake `ollama serve` that answers /api/tags, found through PATH."""
    import os, socket, stat, sys
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = tmp_path / "fake_ollama.py"
    script.write_text(
        "import sys, json\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        b = json.dumps({'models': [{'name': 'gemma:2b'}]}).encode()\n"
        "        self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)\n"
        "    def log_message(self, *a): pass\n"
        f"HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n"
    )
    monkeypatch.setattr(W_, "find_ollama_exe", lambda: sys.executable)
    real_popen = W_.subprocess.Popen
    monkeypatch.setattr(W_, "find_ollama_exe", lambda: sys.executable)
    r = ensure_ollama(f"http://127.0.0.1:{port}", "gemma:2b", wait_s=10,
                      popen=lambda cmd, **k: real_popen([cmd[0], str(script)], **k))
    try:
        assert r.status == "started" and r.process is not None
        assert probe_ollama(f"http://127.0.0.1:{port}", "gemma:2b").model_present
        # a second call finds it running and starts nothing
        assert ensure_ollama(f"http://127.0.0.1:{port}", "gemma:2b").status == "already_running"
    finally:
        stop_process(r.process)
    assert r.process.poll() is not None                  # we really stopped what we started


# ---------------- real toggles (Wi-Fi / Bluetooth / Flashlight) ----------------
from gui_app import toggles as T


class FakeShell:
    """Simulated `adb shell`: wifi_on / bluetooth_on settings plus svc commands."""

    def __init__(self, wifi="0", bt="0", svc_works=True, readable=True):
        self.vals = {"wifi_on": wifi, "bluetooth_on": bt}
        self.svc_works, self.readable, self.calls = svc_works, readable, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:3] == ["settings", "get", "global"]:
            if not self.readable:
                raise RuntimeError("no device")
            return self.vals[args[3]] + "\n"
        if args[:2] == ["svc", "wifi"]:
            if self.svc_works:
                self.vals["wifi_on"] = "1" if args[2] == "enable" else "0"
            return ""
        if args[:2] == ["svc", "bluetooth"]:
            if self.svc_works:
                self.vals["bluetooth_on"] = "1" if args[2] == "enable" else "0"
            return ""
        raise RuntimeError(f"unexpected {args}")


def _log():
    lines = []
    return lines, (lambda level, msg: lines.append((level, msg)))


def _fast(**k):
    t = {"v": 0.0}

    def sleep(s):
        t["v"] += s

    return dict(sleep=sleep, clock=lambda: t["v"], **k)


def test_toggle_goal_helpers():
    assert T.goal_for("wifi") == "@toggle:wifi" and T.parse_goal("@toggle:bluetooth") == "bluetooth"
    assert T.parse_goal("turn on wifi") is None and T.parse_goal("@toggle:nope") is None
    assert T.friendly("@toggle:wifi") == "Wi-Fi toggle" and T.friendly("mail") == "mail"


@pytest.mark.parametrize("kind,value,expected", [
    ("wifi", "1", True), ("wifi", "2", True), ("wifi", "0", False), ("wifi", "3", False), ("wifi", "null", None),
    ("bluetooth", "1", True), ("bluetooth", "0", False), ("bluetooth", "2", None), ("bluetooth", "", None),
    ("flashlight", "1", None),
])
def test_parse_radio_state(kind, value, expected):
    assert T.parse_radio_state(kind, value) is expected


@pytest.mark.parametrize("kind,key", [("wifi", "wifi_on"), ("bluetooth", "bluetooth_on")])
def test_toggle_flips_off_to_on_and_back(kind, key):
    shell = FakeShell()
    lines, log = _log()
    r = T.toggle_radio(kind, shell, None, log, **_fast())
    assert r.ok and (r.was, r.now, r.method) == (False, True, "adb") and shell.vals[key] == "1"
    r = T.toggle_radio(kind, shell, None, log, **_fast())
    assert r.ok and (r.was, r.now) == (True, False) and shell.vals[key] == "0"      # second press = OFF
    assert any("turning ON" in m for _, m in lines) and any("turning OFF" in m for _, m in lines)


def test_toggle_uses_only_readonly_probe_plus_svc():
    shell = FakeShell(wifi="1")
    T.toggle_radio("wifi", shell, None, _log()[1], **_fast())
    assert ["svc", "wifi", "disable"] in shell.calls
    assert all(c[:3] == ["settings", "get", "global"] or c[:2] == ["svc", "wifi"] for c in shell.calls)


def test_toggle_falls_back_to_macro_when_svc_does_nothing():
    shell = FakeShell(svc_works=False)
    macro_calls = []

    def macro(goal):
        macro_calls.append(goal)
        shell.vals["wifi_on"] = "1"                 # the tile tap works
        return True

    r = T.toggle_radio("wifi", shell, macro, _log()[1], **_fast())
    assert r.ok and r.method == "macro" and macro_calls == ["turn on wifi"]


def test_toggle_reports_failure_honestly():
    shell = FakeShell(svc_works=False)
    r = T.toggle_radio("bluetooth", shell, lambda g: True, _log()[1], **_fast())   # macro "succeeds" but nothing changes
    assert not r.ok and r.method == "failed" and "did not switch ON" in r.message
    r = T.toggle_radio("bluetooth", FakeShell(svc_works=False), None, _log()[1], **_fast())
    assert not r.ok and r.method == "failed"


def test_toggle_unreadable_state():
    shell = FakeShell(readable=False)
    r = T.toggle_radio("wifi", shell, None, _log()[1], **_fast())
    assert not r.ok and "Cannot read" in r.message
    r = T.toggle_radio("wifi", shell, lambda g: True, _log()[1], **_fast())
    assert r.ok and r.method == "macro (state unknown)" and r.now is None


def test_toggle_survives_svc_exceptions():
    class Boom(FakeShell):
        def __call__(self, args):
            if args[0] in ("svc", "cmd"):
                raise RuntimeError("permission denied")
            return super().__call__(args)

    r = T.toggle_radio("bluetooth", Boom(), None, _log()[1], **_fast())
    assert not r.ok


def test_flashlight_tracks_assumed_state():
    lines, log = _log()
    ran = []
    r = T.toggle_flashlight(lambda g: ran.append(g) or True, None, log)
    assert r.ok and r.now is True and ran == ["flashlight"]
    r = T.toggle_flashlight(lambda g: True, True, log)
    assert r.now is False
    r = T.toggle_flashlight(lambda g: False, True, log)
    assert not r.ok and r.now is True                       # failed macro keeps the old assumption
    assert not T.toggle_flashlight(None, None, log).ok


# ---------------- ToggleAgent routing + demo shell ----------------
from gui_app.workers import ToggleAgent, load_real_agent


def test_toggle_agent_routes_toggles_and_passes_other_goals_through():
    shell = FakeShell()
    lines, log = _log()
    seen = []
    agent = ToggleAgent(shell, lambda lvl, msg: lines.append(msg), lambda: (lambda goal: seen.append(goal) or True))
    assert agent("@toggle:wifi") is True and shell.vals["wifi_on"] == "1" and seen == []     # direct adb, agent untouched
    assert agent("mirror") is True and seen == ["mirror"]
    assert agent("@toggle:flashlight") is True and agent.torch_assumed is True and seen[-1] == "flashlight"
    assert agent("@toggle:flashlight") is True and agent.torch_assumed is False


def test_toggle_agent_without_loaded_agent():
    shell = FakeShell()
    agent = ToggleAgent(shell, lambda *a: None, lambda: None)
    assert agent("@toggle:bluetooth") is True                      # radios work with no agent at all
    assert agent("@toggle:flashlight") is False and "not loaded" in agent.last_error.lower()
    with pytest.raises(RuntimeError):
        agent("mirror")


def test_toggle_agent_failure_message_reaches_the_runner():
    agent = ToggleAgent(FakeShell(svc_works=False), lambda *a: None, lambda: None)
    events: queue.Queue = queue.Queue()
    runner = AgentRunner(agent, events)
    runner.start("@toggle:wifi")
    _, result = events.get(timeout=20)
    assert result.ok is False and "did not switch ON" in result.error


def test_demo_shell_drives_the_real_toggle_logic():
    from gui_app.demo import DemoPhone
    phone = DemoPhone()
    r = T.toggle_radio("wifi", phone.shell, None, _log()[1])
    assert r.ok and phone.wifi_state() is True
    r = T.toggle_radio("bluetooth", phone.shell, None, _log()[1])
    assert r.ok and phone.bt_state() is True
    assert T.toggle_radio("wifi", phone.shell, None, _log()[1]).now is False
