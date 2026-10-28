"""Timer / alarm workflow on the simulated Clock app (no phone needed)."""

import time
from datetime import datetime

import pytest

import clock_state
import clock_workflow
import config
from clock_skill import parse_clock_command
from clock_workflow import run_clock
from tests.fake_clock import CLOCK, ClockDevice, ClockDispatcher, ClockPhone
from workflows import WorkflowContext

NOW = datetime(2026, 10, 27, 22, 58)


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "UI_WAIT_TIMEOUT_S", 0.2)
    monkeypatch.setattr(clock_workflow, "FAILURE_DUMP", tmp_path / "last_failure_ui.txt")
    import brain.brain as b
    monkeypatch.setattr(b.requests, "post", lambda *a, **k: (_ for _ in ()).throw(b.requests.ConnectionError("no ollama")))


def env(**kw):
    phone = ClockPhone(**kw)
    ctx = WorkflowContext(device=ClockDevice(phone), dispatcher=ClockDispatcher(phone), find_element=lambda *a: None, capture_ui_state=lambda *a: [])
    return phone, ctx, clock_state.ClockState()


def run(text, ctx, state):
    intent = parse_clock_command(text, NOW)
    assert intent is not None, text
    return run_clock(ctx, intent, text, state)


# ---------------- timers ----------------
def test_timer_via_intent():
    phone, ctx, state = env()
    assert run("set timer for 10 minutes", ctx, state)
    assert [t["total"] for t in phone.timers] == [600]
    assert [t.total_s for t in state.running_timers()] == [600]
    assert any("SET_TIMER" in " ".join(c) for c in phone.am_calls)


def test_timer_label_is_quoted_and_passed():
    phone, ctx, state = env()
    assert run("set timer for 2 min called pasta water", ctx, state)
    call = " ".join(next(c for c in phone.am_calls if "SET_TIMER" in " ".join(c)))
    assert "'pasta water'" in call
    assert phone.timers[0]["label"] == "pasta water" and state.running_timers()[0].label == "pasta water"


def test_timer_intent_refused_falls_back_to_ui():
    phone, ctx, state = env(intents="refused")
    assert run("set timer for 5 minutes", ctx, state)
    assert [t["total"] for t in phone.timers] == [300]            # keypad 5 0 0 -> 05m 00s
    assert len(state.running_timers()) == 1


def test_timer_intent_silently_ignored_is_detected_and_ui_used():
    phone, ctx, state = env(intents="silent")
    assert run("set timer for 90 seconds", ctx, state)
    assert [t["total"] for t in phone.timers] == [90]              # exactly one -> no duplicate


def test_timer_total_failure_dumps_ui_and_returns_false(tmp_path):
    phone, ctx, state = env(intents="refused", timer_ui_works=False)
    assert run("set timer for 1 minute", ctx, state) is False
    assert state.running_timers() == []
    dump = (tmp_path / "last_failure_ui.txt").read_text(encoding="utf-8")
    assert "reason:" in dump and CLOCK in dump


def test_timer_hours_keypad_digits():
    assert clock_workflow.timer_digits(3600 + 5 * 60 + 20) == "10520"
    assert clock_workflow.timer_digits(600) == "1000"
    assert clock_workflow.timer_digits(45) == "45"


def test_stop_timer():
    phone, ctx, state = env()
    run("set timer for 10 minutes", ctx, state)
    assert run("stop timer", ctx, state)
    assert phone.timers == [] and state.running_timers() == []


def test_stop_timer_ui_fallback():
    phone, ctx, state = env(intents="refused")
    run("set timer for 10 minutes", ctx, state)
    assert phone.timers
    assert run("cancel timer", ctx, state)
    assert phone.timers == []


# ---------------- alarms ----------------
def test_alarm_via_intent():
    phone, ctx, state = env()
    assert run("set alarm for 7:30 am", ctx, state)
    assert [(a["h"], a["m"]) for a in phone.alarms] == [(7, 30)]
    assert [(a.hour, a.minute) for a in state.active_alarms()] == [(7, 30)]


def test_alarm_pm_and_label_and_days():
    phone, ctx, state = env()
    assert run("alarm at 6:45 pm called gym every weekday", ctx, state)
    a = phone.alarms[0]
    assert (a["h"], a["m"], a["label"]) == (18, 45, "gym")
    assert a["days"] == [2, 3, 4, 5, 6]                  # Mon..Fri in Android numbering (Sun=1)
    rec = state.active_alarms()[0]
    assert rec.days == [0, 1, 2, 3, 4] and rec.label == "gym"


def test_alarm_rollover_ambiguous_six_at_2258():
    phone, ctx, state = env()
    assert run("alarm at 6", ctx, state)
    assert [(a["h"], a["m"]) for a in phone.alarms] == [(6, 0)]      # next 6 o'clock after 22:58 is 06:00


def test_alarm_intent_refused_falls_back_to_ui_12h():
    phone, ctx, state = env(intents="refused")
    assert run("set alarm for 7:30 pm", ctx, state)
    assert [(a["h"], a["m"]) for a in phone.alarms] == [(19, 30)]


def test_alarm_ui_fallback_24h_picker():
    phone, ctx, state = env(intents="refused", ampm=False)
    assert run("set alarm for 18:05", ctx, state)
    assert [(a["h"], a["m"]) for a in phone.alarms] == [(18, 5)]


def test_alarm_silent_intent_detected_via_list():
    phone, ctx, state = env(intents="silent", next_alarm_readable=False)
    assert run("alarm at 9 am", ctx, state)
    assert [(a["h"], a["m"]) for a in phone.alarms] == [(9, 0)]


def test_alarm_no_clock_app_is_a_clean_failure():
    phone = ClockPhone(clock_pkg="com.example.nope")
    phone.installed.discard("com.example.nope")
    ctx = WorkflowContext(device=ClockDevice(phone), dispatcher=ClockDispatcher(phone), find_element=lambda *a: None, capture_ui_state=lambda *a: [])
    assert run_clock(ctx, parse_clock_command("alarm at 7 am", NOW), "alarm at 7 am", clock_state.ClockState()) is False


def test_impossible_commands_touch_nothing():
    phone, ctx, state = env()
    for text in ("alarm at 25:00", "timer 0 seconds", "set timer"):
        intent = parse_clock_command(text, NOW)
        assert intent is not None and not intent.ok
        assert run_clock(ctx, intent, text, state) is False
    assert phone.am_calls == [] and phone.timers == [] and phone.alarms == []


def test_cancel_specific_alarm():
    phone, ctx, state = env()
    run("set alarm for 7 am", ctx, state)
    run("set alarm for 8 am", ctx, state)
    assert run("cancel the 7 am alarm", ctx, state)
    assert {(a["h"], a["on"]) for a in phone.alarms} == {(7, False), (8, True)}
    assert [(a.hour) for a in state.active_alarms()] == [8]


def test_cancel_alarm_ambiguous_hour_matches_tracked_pm():
    phone, ctx, state = env()
    run("set alarm for 7 pm", ctx, state)
    assert run("cancel alarm 7", ctx, state)
    assert [a["on"] for a in phone.alarms] == [False]


def test_cancel_next_alarm_and_all_via_ui():
    phone, ctx, state = env(intents="refused")
    run("set alarm for 7 am", ctx, state)
    run("set alarm for 8 am", ctx, state)
    assert run("cancel all alarms", ctx, state)
    assert all(not a["on"] for a in phone.alarms) and state.active_alarms() == []


def test_show_screens():
    phone, ctx, state = env()
    assert run("show alarms", ctx, state) and phone.screen == "clock_alarm"
    assert run("show timers", ctx, state) and phone.screen == "clock_timer"


def test_oem_tab_names_still_found():
    phone, ctx, state = env(intents="refused", tab_names=("Alarms", "Timer"))
    assert run("set alarm for 6 am", ctx, state)
    assert [(a["h"], a["m"]) for a in phone.alarms] == [(6, 0)]


def test_forced_package_is_used(monkeypatch):
    phone, ctx, state = env(clock_pkg="com.vendor.clock")
    monkeypatch.setattr(config, "CLOCK_PACKAGE", "com.vendor.clock")
    assert run("set timer for 1 minute", ctx, state)
    assert "com.vendor.clock" in " ".join(next(c for c in phone.am_calls if "SET_TIMER" in " ".join(c)))


# ---------------- verification helpers ----------------
def test_next_alarm_parsing():
    phone, ctx, _ = env()
    assert clock_workflow.next_alarm_time(ctx) == (True, None)
    phone.add_alarm(19, 5)
    assert clock_workflow.next_alarm_time(ctx) == (True, (19, 5))
    phone.next_alarm_readable = False
    assert clock_workflow.next_alarm_time(ctx)[0] is False


def test_countdown_text_formats():
    mk = lambda t: [dict(text=t, content_desc="", resource_id="", **{"class": "x", "package": "p"})]
    assert clock_workflow.timer_countdowns(mk("09:58")) == [598]
    assert clock_workflow.timer_countdowns(mk("1:00:00")) == [3600]
    assert clock_workflow.timer_countdowns(mk("00h 09m 58s")) == [598]
    assert clock_workflow.timer_countdowns(mk("Timer")) == []


# ---------------- routing ----------------
def test_routing_main_sends_clock_commands_to_clock_and_leaves_the_rest(monkeypatch):
    import main
    from tests.fake_phone import Phone, FakeDevice, FakeDispatcher
    phone = ClockPhone()
    monkeypatch.setattr(main, "connect_device", lambda *a, **k: ClockDevice(phone))
    monkeypatch.setattr(main, "AndroidDispatcher", lambda *a, **k: ClockDispatcher(phone))
    monkeypatch.setattr(main, "_announce_environment", lambda d: None)
    calls = {"clock": [], "wf": [], "skill": []}
    monkeypatch.setattr(main, "run_clock", lambda ctx, intent, goal: calls["clock"].append(goal) or True)
    monkeypatch.setattr(main, "run_workflow", lambda name, ctx, goal: calls["wf"].append(name) or True)
    monkeypatch.setattr(main, "_run_fixed_steps", lambda *a, **k: calls["skill"].append(1) or True)
    monkeypatch.setattr(main, "_run_reactive_loop", lambda *a, **k: False)
    main.run_autonomous_agent("set timer for 5 minutes")
    main.run_autonomous_agent("mirror")
    main.run_autonomous_agent("mail")
    main.run_autonomous_agent("turn on wifi")
    main.run_autonomous_agent("my alarm app keeps crashing")
    assert calls["clock"] == ["set timer for 5 minutes"]
    assert calls["wf"] == ["MIRROR", "MAIL"]
    assert len(calls["skill"]) == 1                                 # only "turn on wifi" hit a known skill


def test_brain_knows_the_new_intents():
    from brain.brain import FALLBACK_PLANS, WORKFLOW_INTENTS, reason_about_command
    assert {"TIMER", "ALARM"} <= set(WORKFLOW_INTENTS) and {"TIMER", "ALARM"} <= set(FALLBACK_PLANS)
    assert reason_about_command("set timer", expected_intent="TIMER")["intent"] == "TIMER"


def test_state_persistence_and_expiry(tmp_path):
    now = [1000.0]
    s = clock_state.ClockState(tmp_path / "s.json", clock=lambda: now[0])
    s.add_timer(60, "tea"); s.add_alarm(7, 0, "x", ())
    s2 = clock_state.ClockState(tmp_path / "s.json", clock=lambda: now[0])
    assert [t.label for t in s2.running_timers()] == ["tea"] and len(s2.active_alarms()) == 1
    now[0] += 61
    assert s2.running_timers() == [] and s2.all_timers()[0].status == "done"
    now[0] += clock_state.DONE_KEEP_S
    assert s2.all_timers() == []


# ---------------- stopping one specific timer ----------------
def test_stop_one_of_two_timers_keeps_the_other():
    phone, ctx, state = env()
    run("set timer for 10 minutes", ctx, state)
    run("set timer for 5 minutes", ctx, state)
    assert run("stop the 600 second timer", ctx, state)
    assert [t["total"] for t in phone.timers] == [300]
    assert [t.total_s for t in state.running_timers()] == [300]


def test_stop_unknown_timer_is_reported_not_faked():
    phone, ctx, state = env()
    run("set timer for 10 minutes", ctx, state)
    assert run("stop the 7 minute timer", ctx, state) is False
    assert [t["total"] for t in phone.timers] == [600]


def test_stop_by_label():
    phone, ctx, state = env()
    run("set timer for 3 minutes called tea", ctx, state)
    run("set timer for 9 minutes called rice", ctx, state)
    assert run("stop timer called tea", ctx, state)
    assert [t["label"] for t in phone.timers] == ["rice"]


def test_gui_stop_command_round_trips():
    from clock_skill import stop_timer_command
    i = parse_clock_command(stop_timer_command(600), NOW)
    assert i.kind == "STOP_TIMER" and i.seconds == 600
    j = parse_clock_command(stop_timer_command(60, "tea"), NOW)
    assert j.label == "tea"
