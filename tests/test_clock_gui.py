"""Clock panel: command building (pure), the demo clock agent, and a Tk smoke test (skipped without a display)."""

from datetime import datetime

import pytest

import clock_state
from clock_skill import SET_ALARM, SET_TIMER, parse_clock_command
from gui_app import clock_panel as cp
from gui_app.demo import DemoPhone, make_demo_agent

NOW = datetime(2026, 10, 27, 10, 0)


# ---------------- pure helpers ----------------
@pytest.mark.parametrize("m,s,expected", [(5, 0, "set timer for 5 minutes"), (1, 0, "set timer for 1 minute"), (0, 30, "set timer for 30 seconds"),
                                          (2, 15, "set timer for 2 minutes 15 seconds"), (0, 0, "")])
def test_timer_command(m, s, expected):
    assert cp.timer_command(m, s) == expected


def test_timer_commands_all_parse_back_to_the_same_seconds():
    for m in (0, 1, 5, 10, 15, 90, 1439):
        for s in (0, 1, 30, 59):
            cmd = cp.timer_command(m, s)
            if not cmd:
                continue
            i = parse_clock_command(cmd, NOW)
            assert i.kind == SET_TIMER and i.seconds == m * 60 + s, cmd


def test_alarm_command_validation_and_round_trip():
    assert cp.alarm_command("7", "30", "AM") == ("set alarm for 7:30 am", "")
    assert cp.alarm_command("12", "0", "PM", "  gym   time ") == ("set alarm for 12:00 pm called gym time", "")
    assert cp.alarm_command("0", "30", "AM")[1] and cp.alarm_command("13", "0", "AM")[1] and cp.alarm_command("7", "75", "AM")[1]
    assert cp.alarm_command("x", "30", "AM")[1]
    for h in range(1, 13):
        for ap, offset in (("AM", 0), ("PM", 12)):
            cmd, err = cp.alarm_command(str(h), "5", ap)
            i = parse_clock_command(cmd, NOW)
            assert not err and i.kind == SET_ALARM and (i.hour, i.minute) == (h % 12 + offset, 5), cmd


def test_formatting_helpers():
    assert cp.clock_hms(65) == "01:05" and cp.clock_hms(3725) == "1:02:05" and cp.clock_hms(-4) == "00:00"
    assert cp.rings_in(100, 70) == "in 30 s" and cp.rings_in(7200, 0) == "in 2 h 00 m" and cp.rings_in(3000, 0) == "in 50 min"
    assert cp.parse_spin("12", 0, 59) == 12 and cp.parse_spin("60", 0, 59) is None and cp.parse_spin("x", 0, 59) is None and cp.parse_spin("", 0, 59) == 0


# ---------------- demo agent ----------------
def test_demo_agent_runs_clock_commands_through_the_real_parser():
    state = clock_state.ClockState()
    log = []
    agent = make_demo_agent(DemoPhone(), lambda lvl, msg: log.append(msg), speed=0, state=state)
    assert agent("set timer for 5 minutes called tea")
    assert [(t.total_s, t.label) for t in state.running_timers()] == [(300, "tea")]
    assert agent("alarm at 7:30 am every weekday")
    assert [(a.hour, a.minute, a.days) for a in state.active_alarms()] == [(7, 30, [0, 1, 2, 3, 4])]
    assert agent("cancel alarm 7:30 am")
    assert state.active_alarms() == []
    assert agent("stop the 300 second timer")
    assert state.running_timers() == []
    assert agent("alarm at 25:00") is False                       # impossible: nothing recorded
    assert agent("stop the 9 minute timer") is False              # not running
    assert any("[CLOCK] Understood" in m for m in log) and any("[STEP 2/5]" in m for m in log)


def test_demo_agent_leaves_other_commands_alone():
    log = []
    agent = make_demo_agent(DemoPhone(), lambda lvl, msg: log.append(msg), speed=0, state=clock_state.ClockState())
    assert agent("go back")
    assert not any("[CLOCK]" in m for m in log)


# ---------------- Tk smoke test ----------------
@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    yield root
    root.destroy()


def test_panel_builds_runs_commands_and_lists_state(tk_root):
    state = clock_state.ClockState()
    sent = []
    panel = cp.ClockPanel(tk_root, state=state, run_command=lambda c: sent.append(c) or True)
    panel.card.pack()
    panel.min_var.set("2"); panel.sec_var.set("30")
    panel.on_start_timer()
    panel.on_preset(10)
    panel.hour_var.set("6"); panel.alarm_min_var.set("45"); panel.ampm.set("PM"); panel.label_var.set("gym")
    panel.on_set_alarm()
    panel.on_stop_all()
    assert sent == ["set timer for 2 minutes 30 seconds", "set timer for 10 minutes", "set alarm for 6:45 pm called gym", "stop timer"]
    assert "Understood: Timer for 10 min" in panel.status.cget("text") or "Understood" in panel.status.cget("text")

    state.add_timer(120, "eggs"); state.add_timer(600)
    state.add_alarm(18, 45, "gym", ())
    panel.refresh()
    tk_root.update_idletasks()
    texts = [w.cget("text") for w in panel.timer_list.winfo_children() if w.winfo_class() == "Label"]
    assert any("eggs" == t for t in texts) and any(t.startswith("01:5") or t.startswith("02:00") for t in texts)
    assert any(w.winfo_class() == "TButton" for w in panel.timer_list.winfo_children())   # per-timer Stop
    assert any("06:45 PM" == w.cget("text") for w in panel.alarm_list.winfo_children() if w.winfo_class() == "Label")


def test_panel_rejects_bad_input_without_sending(tk_root):
    sent = []
    panel = cp.ClockPanel(tk_root, state=clock_state.ClockState(), run_command=lambda c: sent.append(c) or True)
    panel.min_var.set("0"); panel.sec_var.set("0"); panel.on_start_timer()
    panel.min_var.set("abc"); panel.on_start_timer()
    panel.hour_var.set("0"); panel.on_set_alarm()
    assert sent == []
    assert panel.submit("alarm at 25:00") is False and sent == []
    assert panel.submit("open camera") is False and sent == []


def test_panel_per_row_buttons_send_precise_commands(tk_root):
    sent = []
    state = clock_state.ClockState()
    panel = cp.ClockPanel(tk_root, state=state, run_command=lambda c: sent.append(c) or True)
    panel.card.pack()
    state.add_timer(600); state.add_alarm(7, 30, "", ())
    panel.refresh()
    for w in panel.timer_list.winfo_children() + panel.alarm_list.winfo_children():
        if w.winfo_class() == "TButton":
            w.invoke()
    assert sent == ["stop the 600 second timer", "cancel alarm 07:30 am"]
