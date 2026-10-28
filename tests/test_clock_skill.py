"""Pure-parser tests for timer / alarm commands (no phone, no Tk). Fixed 'now' = Tue 27 Oct 2026 10:00."""

from datetime import datetime

import pytest

from clock_skill import (
    CANCEL_ALARM, SET_ALARM, SET_TIMER, SHOW_ALARMS, SHOW_TIMERS, STOP_TIMER, parse_clock_command as parse,
)

NOW = datetime(2026, 10, 27, 10, 0)

TIMERS = [
    ("set timer for 10 minutes", 600), ("timer 5 min", 300), ("start a 30 second timer", 30), ("set a timer for one hour thirty minutes", 5400),
    ("timer for 1 hour 5 minutes 20 seconds", 3920), ("set timer 90 seconds", 90), ("timer for half an hour", 1800),
    ("set a timer for an hour", 3600), ("start timer for 2 hours", 7200), ("set a 15 minute timer", 900),
    ("timer for twenty five minutes", 1500), ("Set Timer For 45 Seconds", 45), ("please set a timer for 3 minutes", 180),
    ("timer for 1.5 hours", 5400), ("set timer 2h30m", 9000), ("timer for 10 mins and 30 secs", 630), ("start a timer for ninety seconds", 90),
    ("can you set a timer for 20 minutes", 1200), ("set timer for 1 minute", 60), ("timer 24 hours", 86400),
]


@pytest.mark.parametrize("text,seconds", TIMERS)
def test_timer_phrases(text, seconds):
    i = parse(text, NOW)
    assert i is not None and i.ok and i.kind == SET_TIMER and i.seconds == seconds, i


ALARMS = [  # text, (hour24, minute)
    ("set alarm for 7:30 am", (7, 30)), ("alarm at 6 pm", (18, 0)), ("wake me up at 5:45 am", (5, 45)), ("set an alarm for 07:15", (7, 15)),
    ("alarm at noon", (12, 0)), ("alarm at midnight", (0, 0)), ("alarm at 0730", (7, 30)), ("set alarm for 13:00", (13, 0)),
    ("alarm for six thirty am", (6, 30)), ("alarm at quarter past 7 pm", (19, 15)), ("alarm at half past six pm", (18, 30)),
    ("set an alarm for ten to eight pm", (19, 50)), ("alarm at 7 a.m.", (7, 0)), ("Alarm at 11:59 PM", (23, 59)),
    ("set alarm 8 o'clock am", (8, 0)), ("alarm at 12 am", (0, 0)), ("alarm at 12:30 pm", (12, 30)), ("set an alarm for 9 pm", (21, 0)),
]


@pytest.mark.parametrize("text,hm", ALARMS)
def test_alarm_phrases(text, hm):
    i = parse(text, NOW)
    assert i is not None and i.ok and i.kind == SET_ALARM and (i.hour, i.minute) == hm, i


def test_label_and_repeat():
    i = parse("alarm at 6:30 am tomorrow called gym time", NOW)
    assert (i.hour, i.minute, i.label) == (6, 30, "gym time") and i.when == datetime(2026, 10, 28, 6, 30)
    j = parse("set alarm for 7 am every weekday", NOW)
    assert j.days == (0, 1, 2, 3, 4) and j.when == datetime(2026, 10, 28, 7, 0)
    k = parse("alarm at 9 am every saturday and sunday", NOW)
    assert k.days == (5, 6) and k.when == datetime(2026, 10, 31, 9, 0)
    assert parse("alarm at 8 am on weekends", NOW).days == (5, 6)
    assert parse("alarm 7 daily", NOW).days == tuple(range(7))
    assert parse("set timer for 2 min called pasta", NOW).label == "pasta"


def test_label_is_sanitised_for_the_shell():
    i = parse("set timer for 1 minute called tea; rm -rf /", NOW)
    assert i.ok and ";" not in i.label and "/" not in i.label and len(i.label) <= 40


@pytest.mark.parametrize("now,text,expected", [
    (datetime(2026, 10, 27, 22, 58), "alarm at 6", datetime(2026, 10, 28, 6, 0)),        # no am/pm, late night: next 6 is tomorrow morning
    (datetime(2026, 10, 27, 10, 0), "alarm at 6", datetime(2026, 10, 27, 18, 0)),        # morning: next 6 is this evening
    (datetime(2026, 10, 27, 5, 0), "alarm at 6", datetime(2026, 10, 27, 6, 0)),
    (datetime(2026, 10, 27, 22, 58), "alarm at 6 pm", datetime(2026, 10, 28, 18, 0)),    # explicit but already past today
    (datetime(2026, 10, 27, 10, 0), "alarm at 6 pm", datetime(2026, 10, 27, 18, 0)),
    (datetime(2026, 10, 27, 10, 0), "alarm at 9 am", datetime(2026, 10, 28, 9, 0)),
    (datetime(2026, 10, 27, 23, 59), "alarm at 12", datetime(2026, 10, 28, 0, 0)),
    (datetime(2026, 12, 31, 23, 30), "alarm at 7:00 am", datetime(2027, 1, 1, 7, 0)),    # year rollover
    (datetime(2026, 2, 28, 23, 0), "alarm at 1 am", datetime(2026, 3, 1, 1, 0)),         # month rollover
    (datetime(2026, 10, 27, 10, 0), "alarm at 10:00 am", datetime(2026, 10, 28, 10, 0)), # exactly now = already passed
    (datetime(2026, 10, 27, 10, 0), "alarm at 7 am tomorrow", datetime(2026, 10, 28, 7, 0)),
    (datetime(2026, 10, 27, 10, 0), "alarm at 7 am on friday", datetime(2026, 10, 30, 7, 0)),
])
def test_rollover(now, text, expected):
    i = parse(text, now)
    assert i.ok and i.when == expected, (i.when, i.note)


def test_relative_alarm():
    i = parse("wake me up in 45 minutes", datetime(2026, 10, 27, 22, 30))
    assert (i.hour, i.minute) == (23, 15) and i.relative
    j = parse("alarm in 2 hours", datetime(2026, 10, 27, 23, 0))
    assert (j.hour, j.minute) == (1, 0) and j.when == datetime(2026, 10, 28, 1, 0)


def test_ambiguity_is_explained():
    i = parse("alarm at 6", datetime(2026, 10, 27, 22, 58))
    assert "06:00 AM tomorrow" in i.note and "next occurrence" in i.note


@pytest.mark.parametrize("text", ["alarm at 25:00", "alarm at 7:75", "alarm at 13 pm", "timer 0 seconds", "set timer", "timer 25 hours", "set timer for 5",
                                   "set alarm", "alarm in 20 seconds", "alarm at 0 am"])
def test_impossible_or_incomplete_are_errors_not_actions(text):
    i = parse(text, NOW)
    assert i is not None and not i.ok and i.error, text


@pytest.mark.parametrize("text,kind", [
    ("stop timer", STOP_TIMER), ("cancel the timer", STOP_TIMER), ("delete my timer", STOP_TIMER), ("show timers", SHOW_TIMERS),
    ("show alarms", SHOW_ALARMS), ("open my alarms", SHOW_ALARMS), ("cancel alarm", CANCEL_ALARM), ("cancel all alarms", CANCEL_ALARM),
    ("delete the 7 am alarm", CANCEL_ALARM), ("turn off the alarm", CANCEL_ALARM), ("dismiss alarm at 6:30 am", CANCEL_ALARM),
])
def test_management_commands(text, kind):
    i = parse(text, NOW)
    assert i is not None and i.ok and i.kind == kind, i


def test_cancel_details():
    assert parse("cancel all alarms", NOW).cancel_all is True
    i = parse("cancel the 7 pm alarm", NOW)
    assert (i.hour, i.minute) == (19, 0)
    j = parse("cancel alarm 7", NOW)
    assert j.hour == 7 and "no am/pm" in j.note


@pytest.mark.parametrize("text", [
    "mirror", "mail", "turn on wifi", "turn off bluetooth", "open camera", "flashlight", "airplane mode", "go home", "go back", "quick settings",
    "my alarm app keeps crashing", "set the table", "remind me in 5 minutes", "what time is it", "read my mail", "set volume to 5",
    "open settings", "", "   ", "the timer and the alarm", "set a reminder", "mirror legacy",
])
def test_non_clock_commands_are_left_alone(text):
    assert parse(text, NOW) is None, text


def test_garbage_input_never_raises():
    for bad in (None, 123, "\x00\x01", "alarm " * 100, "timer " + "9" * 500, "alarm at " + ":" * 20):
        parse(bad, NOW)


def test_summary_text():
    assert parse("set timer for 10 minutes", NOW).summary == "Timer for 10 min"
    assert parse("alarm at 7:30 am tomorrow", NOW).summary == "Alarm for 07:30 AM tomorrow"
