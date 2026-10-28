"""Timer / alarm workflow: executes a parsed ClockIntent (clock_skill.py) on the phone.

Three layers, tried in this order, and every layer is VERIFIED before the next one is skipped:
  1. Android's standard Clock intents over adb (SET_TIMER / SET_ALARM / SHOW_* / DISMISS_*) -- fast and
     does not depend on any screen layout.
  2. Clock-app UI automation with multi-selector fallback chains (the same style as the mirror / mail
     workflows), used when the intent is refused, ignored, or could not be confirmed.
  3. On total failure the visible UI is written to logs/last_failure_ui.txt so selectors can be fixed
     in minutes (use `python inspect_ui.py --package <clock package>` on the phone).

Honesty rules baked in: nothing is reported as done unless it was confirmed on the phone, or (only when
the phone gives no readable evidence at all) clearly logged as "unverified".

Everything is 100% offline -- the local LLM is only asked to narrate the plan, exactly like mirror / mail.
"""

from __future__ import annotations

import re
import time
from datetime import datetime

import config
import clock_state
from clock_skill import (
    CANCEL_ALARM, PAUSE_TIMER, SET_ALARM, SET_TIMER, SHOW_ALARMS, SHOW_TIMERS, STOP_TIMER,
    ClockIntent, format_days, format_duration, format_time12,
)
from perception.perception import dump_nodes, find_by_chain
from utils.logger import logger
from workflows import (
    WorkflowContext, WorkflowError, _launch, _resolve_package, _sleep, _step, _tap, _wait_for, log_brain_reasoning,
)

TAG = "[CLOCK]"

ACTION_SET_TIMER = "android.intent.action.SET_TIMER"
ACTION_SET_ALARM = "android.intent.action.SET_ALARM"
ACTION_SHOW_TIMERS = "android.intent.action.SHOW_TIMERS"
ACTION_SHOW_ALARMS = "android.intent.action.SHOW_ALARMS"
ACTION_DISMISS_ALARM = "android.intent.action.DISMISS_ALARM"
ACTION_DISMISS_TIMER = "android.intent.action.DISMISS_TIMER"
_X = "android.intent.extra.alarm."

FAILURE_DUMP = config.LOG_DIR / "last_failure_ui.txt"

_ERROR_RE = re.compile(r"^\s*Error\b|Exception|unable to resolve|Activity not started|does not exist", re.IGNORECASE | re.MULTILINE)
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(am|pm)?\s*$", re.IGNORECASE)
_AMPM_RE = re.compile(r"^\s*(am|pm)\s*$", re.IGNORECASE)
_COUNTDOWN_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
_COUNTDOWN_HMS_RE = re.compile(r"^\s*(?:(\d+)\s*h\w*\s*)?(?:(\d+)\s*m\w*\s*)?(?:(\d+)\s*s\w*\s*)?$", re.IGNORECASE)


# =============================================================================================
# Low-level helpers
# =============================================================================================


def _quote(text: str) -> str:
    """The label is already restricted to safe characters by the parser; quotes make `adb shell` keep the spaces."""
    return "'" + text.replace("'", "") + "'"


def am_start(ctx: WorkflowContext, action: str, extras: list[tuple[str, str, str]], package: str = "") -> tuple[bool, str]:
    """`am start -a <action> [-p pkg] <extras>`. Returns (accepted, output). `am` exits 0 even when nothing
    handled the intent, so the output text is what decides."""
    args = ["am", "start", "-a", action]
    if package:
        args += ["-p", package]
    for flag, key, value in extras:
        args += [flag, key, str(value)]
    try:
        out = ctx.dispatcher.shell(*args) or ""
    except Exception as exc:  # noqa: BLE001 -- CalledProcessError, missing adb, ...
        return False, f"{type(exc).__name__}: {exc}"
    return (not _ERROR_RE.search(out)), out.strip()


def _clock_nodes(ctx: WorkflowContext, pkg: str) -> list[dict]:
    try:
        return [n for n in dump_nodes(ctx.device) if n["package"] == pkg]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"{TAG} dump failed: {exc}")
        return []


def dump_failure_ui(ctx: WorkflowContext, reason: str, pkg: str = "") -> str:
    """Write what the phone is showing right now to logs/last_failure_ui.txt (for fixing selectors)."""
    lines = [f"Re-Node clock failure @ {datetime.now():%Y-%m-%d %H:%M:%S}", f"reason: {reason}", f"clock package: {pkg or '(unknown)'}"]
    try:
        lines.append(f"foreground: {ctx.device.app_current()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"foreground: unavailable ({exc})")
    try:
        for n in dump_nodes(ctx.device):
            lines.append(
                f"{'CLICK' if n['clickable'] else '     '} pkg={n['package']} rid={n['resource_id']!r} text={n['text']!r} "
                f"desc={n['content_desc']!r} cls={n['class']} checked={n['checked']} bounds={n['bounds']}"
            )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"hierarchy unavailable: {exc}")
    try:
        FAILURE_DUMP.parent.mkdir(parents=True, exist_ok=True)
        FAILURE_DUMP.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(f"{TAG} UI dump saved to {FAILURE_DUMP}")
    except OSError as exc:
        logger.warning(f"{TAG} could not write the UI dump: {exc}")
    return str(FAILURE_DUMP)


def _tab_chain(name: str) -> list[dict]:
    word = rf"^{name}s?$"
    return [
        {"rid": rf"tab.*{name}|{name}.*tab|{name}s?_tab", "clickable": True},
        {"desc": word, "clickable": True},
        {"text": word, "clickable": True},
        {"label": word},
    ]


def _open_tab(ctx: WorkflowContext, pkg: str, name: str, timeout: float = 4.0) -> bool:
    node, nodes = _wait_for(ctx, _tab_chain(name), timeout=timeout, pkg=pkg)
    if node is None:
        return False
    _tap(ctx, nodes, node)
    _sleep(0.6)
    return True


def _open_clock(ctx: WorkflowContext, pkg: str, tab: str) -> bool:
    _launch(ctx, pkg, fresh=True)
    _sleep(0.8)
    ok = _open_tab(ctx, pkg, tab)
    if not ok:
        logger.warning(f"{TAG} Could not find the {tab.title()} tab (the app may already be on it)")
    return True


# ---------------- reading the Clock app's screens ----------------


def alarm_rows(nodes: list[dict]) -> list[dict]:
    """Alarms visible in the Clock app's alarm list: [{h24, minute, y, switch}] (switch = its toggle node if found)."""
    rows = []
    texts = [n for n in nodes if n["text"].strip() or n["content_desc"].strip()]
    for n in texts:
        for raw in (n["text"], n["content_desc"]):
            m = _TIME_RE.match(raw or "")
            if not m:
                continue
            hour, minute, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
            if not ap:
                for o in texts:
                    if o is not n and abs(o["y"] - n["y"]) <= 90 and _AMPM_RE.match(o["text"] or o["content_desc"] or ""):
                        ap = (o["text"] or o["content_desc"]).strip().lower()
                        break
            h24 = hour % 12 + (12 if ap == "pm" else 0) if ap else hour
            switch = None
            for o in nodes:
                cls = o["class"]
                if o is not n and abs(o["y"] - n["y"]) <= 120 and re.search(r"Switch|Toggle|CheckBox", cls, re.IGNORECASE):
                    switch = o
                    break
            rows.append({"h24": h24, "minute": minute, "y": n["y"], "switch": switch})
            break
    return rows


def timer_countdowns(nodes: list[dict]) -> list[int]:
    out = []
    for n in nodes:
        for raw in (n["text"], n["content_desc"]):
            raw = (raw or "").strip()
            if not raw:
                continue
            m = _COUNTDOWN_RE.match(raw)
            if m:
                a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
                out.append(a * 3600 + b * 60 + int(c) if c is not None else a * 60 + b)
                break
            m = _COUNTDOWN_HMS_RE.match(raw)
            if m and any(m.groups()):
                h, mi, s = (int(g) if g else 0 for g in m.groups())
                out.append(h * 3600 + mi * 60 + s)
                break
    return [c for c in out if c > 0]       # the idle "00h 00m 00s" setup display is not a running timer


_NEXT_ALARM_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)?", re.IGNORECASE)


def next_alarm_time(ctx: WorkflowContext) -> tuple[bool, tuple[int, int] | None]:
    """(readable, (h24, minute) of the phone's next alarm or None if there is none)."""
    try:
        out = (ctx.dispatcher.shell("settings", "get", "system", "next_alarm_formatted") or "").strip()
    except Exception:  # noqa: BLE001
        return False, None
    if not out:
        return False, None
    if out.lower() == "null":
        return True, None
    m = _NEXT_ALARM_RE.search(out)
    if not m:
        return False, None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    return True, ((h % 12 + (12 if ap == "pm" else 0)) if ap else h, mi)


# =============================================================================================
# Verification
# =============================================================================================


def verify_alarm_present(ctx: WorkflowContext, pkg: str, hour: int, minute: int, want_present: bool = True) -> bool | None:
    """True/False = confirmed on the phone; None = the phone showed nothing readable."""
    readable, nxt = next_alarm_time(ctx)
    if want_present and readable and nxt == (hour, minute):
        logger.info(f"{TAG} Confirmed by the phone's next-alarm setting: {format_time12(hour, minute)}")
        return True
    # Look at the Clock app's alarm list (also makes a good live-demo visual).
    _open_clock(ctx, pkg, "alarm")
    nodes = _clock_nodes(ctx, pkg)
    rows = alarm_rows(nodes)
    if not rows:
        if not nodes:
            return None                       # Clock app not readable at all
        return not want_present               # readable screen with an empty alarm list
    mine = [r for r in rows if (r["h24"], r["minute"]) == (hour, minute)]
    if want_present:
        if mine:
            logger.info(f"{TAG} Confirmed in the Clock app's alarm list: {format_time12(hour, minute)}")
        return bool(mine)
    # cancellation: absent, or its switch is off
    if not mine:
        return True
    switches = [r["switch"] for r in mine if r["switch"] is not None]
    if switches and all(not s["checked"] for s in switches):
        return True
    return False if switches else None


def _timer_setup_visible(nodes: list[dict]) -> bool:
    """The idle Timer screen: a keypad of digit buttons and/or the all-zero "00h 00m 00s" display."""
    digits = sum(1 for n in nodes if re.fullmatch(r"\d", n["text"].strip() or n["content_desc"].strip()))
    zero = any(_COUNTDOWN_HMS_RE.match(n["text"] or "") and re.search(r"\d", n["text"]) and not any(timer_countdowns([n])) for n in nodes
               if re.search(r"\d", n["text"] or "") and len((n["text"] or "").strip()) > 2)
    return digits >= 5 or zero


def verify_timer_running(ctx: WorkflowContext, pkg: str, seconds: int, started_at: float) -> bool | None:
    _open_clock(ctx, pkg, "timer")
    nodes = _clock_nodes(ctx, pkg)
    counts = timer_countdowns(nodes)
    if not counts:
        return False if _timer_setup_visible(nodes) else None
    elapsed = max(0.0, time.time() - started_at)
    lo, hi = max(0, seconds - elapsed - 25), seconds + 2
    ok = any(lo <= c <= hi for c in counts)
    if ok:
        logger.info(f"{TAG} Confirmed in the Clock app: a {format_duration(seconds)} countdown is running")
    return ok


def verify_no_timers(ctx: WorkflowContext, pkg: str) -> bool | None:
    _open_clock(ctx, pkg, "timer")
    nodes = _clock_nodes(ctx, pkg)
    if not nodes:
        return None
    return not timer_countdowns(nodes)


# =============================================================================================
# UI-automation fallbacks (selector chains; written for AOSP/Google Clock and common OEM variants)
# =============================================================================================


def _digit_chain(d: str) -> list[dict]:
    return [{"text": rf"^{d}$", "clickable": True}, {"desc": rf"^{d}$", "clickable": True}, {"rid": rf"(?:^|[_/]){d}$", "clickable": True}]


_START_CHAIN = [
    {"rid": r"fab|start|timer_start", "desc": r"start|play", "clickable": True},
    {"desc": r"^(start|play)\b", "clickable": True},
    {"text": r"^start$", "clickable": True},
    {"rid": r"fab|start", "clickable": True},
]
_ADD_ALARM_CHAIN = [
    {"desc": r"add alarm|new alarm|create alarm", "clickable": True},
    {"rid": r"fab|add_alarm|alarm_add|add", "clickable": True},
    {"desc": r"^add$", "clickable": True},
    {"text": r"^\+$", "clickable": True},
]
_KEYBOARD_MODE_CHAIN = [
    {"desc": r"(switch to )?(text|keyboard) (input )?mode|keyboard", "clickable": True},
    {"rid": r"toggle_mode|keyboard|input_mode", "clickable": True},
]
_OK_CHAIN = [{"text": r"^(ok|save|done|set)$", "clickable": True}, {"desc": r"^(ok|save|done|set)$", "clickable": True}, {"rid": r"positive|ok|save", "clickable": True}]
_STOP_TIMER_CHAIN = [
    {"desc": r"^(delete|stop|reset|cancel|remove)( timer)?$", "clickable": True},
    {"text": r"^(delete|stop|reset|cancel|remove)$", "clickable": True},
    {"rid": r"delete|reset|stop", "clickable": True},
]


def timer_digits(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}{m:02d}{s:02d}".lstrip("0") or "0"


def ui_set_timer(ctx: WorkflowContext, pkg: str, seconds: int, label: str) -> bool:
    _open_clock(ctx, pkg, "timer")
    if label:
        logger.info(f"{TAG} (label {label!r} is not applied through the Clock UI; only the intent path supports it)")
    for d in timer_digits(seconds):
        node, nodes = _wait_for(ctx, _digit_chain(d), timeout=3.0, pkg=pkg)
        if node is None:
            logger.warning(f"{TAG} Timer keypad: digit {d!r} not found")
            return False
        _tap(ctx, nodes, node)
        _sleep(0.25)
    node, nodes = _wait_for(ctx, _START_CHAIN, timeout=4.0, pkg=pkg)
    if node is None:
        logger.warning(f"{TAG} Timer Start button not found")
        return False
    _tap(ctx, nodes, node)
    _sleep(1.0)
    return True


def ui_set_alarm(ctx: WorkflowContext, pkg: str, hour: int, minute: int, label: str, days: tuple[int, ...]) -> bool:
    _open_clock(ctx, pkg, "alarm")
    if label or days:
        logger.warning(f"{TAG} Label / repeat days are not applied through the Clock UI (intent path only)")
    node, nodes = _wait_for(ctx, _ADD_ALARM_CHAIN, timeout=4.0, pkg=pkg)
    if node is None:
        logger.warning(f"{TAG} 'Add alarm' button not found")
        return False
    _tap(ctx, nodes, node)
    _sleep(1.0)

    kb, nodes = _wait_for(ctx, _KEYBOARD_MODE_CHAIN, timeout=2.0, pkg=pkg, dismiss=False)
    if kb is not None:
        _tap(ctx, nodes, kb)
        _sleep(0.6)
        nodes = _clock_nodes(ctx, pkg)
    edits = sorted((n for n in nodes if "EditText" in n["class"]), key=lambda n: (n["bounds"][0], n["bounds"][1]))
    if len(edits) < 2:
        logger.warning(f"{TAG} Time picker has no hour/minute text fields (found {len(edits)})")
        return False
    ampm_nodes = [n for n in nodes if _AMPM_RE.match(n["text"] or n["content_desc"] or "")]
    hour_txt = str((hour % 12) or 12) if ampm_nodes else str(hour)
    for field, value in ((edits[0], hour_txt), (edits[1], f"{minute:02d}")):
        ctx.dispatcher.tap(field["x"], field["y"])
        _sleep(0.3)
        try:
            ctx.device.send_keys(value, clear=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{TAG} Typing into the time picker failed: {exc}")
            return False
        _sleep(0.3)
    if ampm_nodes:
        want = "pm" if hour >= 12 else "am"
        target = next((n for n in ampm_nodes if (n["text"] or n["content_desc"]).strip().lower() == want), None)
        if target:
            ctx.dispatcher.tap(target["x"], target["y"])
            _sleep(0.3)
    node, nodes = _wait_for(ctx, _OK_CHAIN, timeout=3.0, pkg=pkg)
    if node is None:
        logger.warning(f"{TAG} OK/Save button of the time picker not found")
        return False
    _tap(ctx, nodes, node)
    _sleep(1.0)
    return True


def ui_cancel_alarms(ctx: WorkflowContext, pkg: str, targets: list[tuple[int, int]] | None, switch_off_all: bool = False) -> bool:
    """Switch alarms off in the list. `targets` = [(h24, minute)]; switch_off_all = every enabled one."""
    _open_clock(ctx, pkg, "alarm")
    nodes = _clock_nodes(ctx, pkg)
    rows = alarm_rows(nodes)
    if not rows:
        logger.warning(f"{TAG} No alarms are readable in the Clock app")
        return False
    todo = [r for r in rows if r["switch"] is not None and r["switch"]["checked"]
            and (switch_off_all or (targets and (r["h24"], r["minute"]) in targets))]
    if not todo:
        logger.info(f"{TAG} Nothing to switch off (already off or not in the list)")
        return bool(targets and any((r["h24"], r["minute"]) in targets for r in rows)) or switch_off_all
    for r in todo:
        ctx.dispatcher.tap(r["switch"]["x"], r["switch"]["y"])
        _sleep(0.5)
    return True


def ui_stop_timers(ctx: WorkflowContext, pkg: str) -> bool:
    _open_clock(ctx, pkg, "timer")
    for _ in range(10):
        node, nodes = _wait_for(ctx, _STOP_TIMER_CHAIN, timeout=1.5, pkg=pkg, dismiss=False)
        if node is None:
            break
        _tap(ctx, nodes, node)
        _sleep(0.5)
    return True


# =============================================================================================
# Per-intent handlers
# =============================================================================================


def _forced_pkg() -> str:
    return config.CLOCK_PACKAGE


def _do_set_timer(ctx, intent: ClockIntent, pkg: str, state) -> bool:
    total = 5
    _step(2, total, f"Starting a {format_duration(intent.seconds)} timer via the Android Clock intent")
    extras = [("--ei", _X + "LENGTH", intent.seconds), ("--ez", _X + "SKIP_UI", "true")]
    if intent.label:
        extras.append(("--es", _X + "MESSAGE", _quote(intent.label)))
    started = time.time()
    accepted, out = am_start(ctx, ACTION_SET_TIMER, extras, _forced_pkg())
    if not accepted:
        logger.warning(f"{TAG} The intent was not accepted ({out[:120]!r})")
    _sleep(1.2)

    result: bool | None = None
    if accepted:
        _step(3, total, "Verifying the timer is running on the phone")
        result = verify_timer_running(ctx, pkg, intent.seconds, started)
    if result is False or not accepted:
        _step(4, total, "Intent path not confirmed: setting the timer through the Clock app UI")
        started = time.time()
        if ui_set_timer(ctx, pkg, intent.seconds, intent.label):
            result = verify_timer_running(ctx, pkg, intent.seconds, started)
        else:
            result = False
    if result is False or (result is None and not accepted):
        dump_failure_ui(ctx, f"timer {intent.seconds}s not confirmed", pkg)
        logger.error(f"{TAG} Could not set the timer. See {FAILURE_DUMP}")
        return False
    if result is None:
        logger.warning(f"{TAG} Timer sent but the Clock app showed no readable countdown: UNVERIFIED. Check the phone.")
    _step(5, total, "Recording the timer")
    state.add_timer(intent.seconds, intent.label, started_at=started)
    logger.info(f"{TAG} Timer set: {format_duration(intent.seconds)}" + (f" ({intent.label})" if intent.label else ""))
    return True


def _do_set_alarm(ctx, intent: ClockIntent, pkg: str, state) -> bool:
    total = 5
    days_txt = f", {format_days(intent.days)}" if intent.days else ""
    _step(2, total, f"Setting alarm {format_time12(intent.hour, intent.minute)}{days_txt} via the Android Clock intent")
    extras = [("--ei", _X + "HOUR", intent.hour), ("--ei", _X + "MINUTES", intent.minute), ("--ez", _X + "SKIP_UI", "true")]
    if intent.label:
        extras.append(("--es", _X + "MESSAGE", _quote(intent.label)))
    if intent.days:
        # Android uses java.util.Calendar numbering: Sunday=1 .. Saturday=7. Ours is Monday=0 .. Sunday=6.
        extras.append(("--eial", _X + "DAYS", ",".join(str(((d + 1) % 7) + 1) for d in intent.days)))
    accepted, out = am_start(ctx, ACTION_SET_ALARM, extras, _forced_pkg())
    if not accepted:
        logger.warning(f"{TAG} The intent was not accepted ({out[:120]!r})")
    _sleep(1.2)

    result: bool | None = None
    if accepted:
        _step(3, total, "Verifying the alarm exists on the phone")
        result = verify_alarm_present(ctx, pkg, intent.hour, intent.minute)
    if (result is False) or not accepted:
        _step(4, total, "Intent path not confirmed: setting the alarm through the Clock app UI")
        if ui_set_alarm(ctx, pkg, intent.hour, intent.minute, intent.label, intent.days):
            result = verify_alarm_present(ctx, pkg, intent.hour, intent.minute)
        else:
            result = False
    if result is False or (result is None and not accepted):
        dump_failure_ui(ctx, f"alarm {intent.hour:02d}:{intent.minute:02d} not confirmed", pkg)
        logger.error(f"{TAG} Could not set the alarm. See {FAILURE_DUMP}")
        return False
    if result is None:
        logger.warning(f"{TAG} Alarm sent but the phone showed nothing readable to confirm it: UNVERIFIED. Check the Clock app.")
    _step(5, total, "Recording the alarm")
    state.add_alarm(intent.hour, intent.minute, intent.label, intent.days, intent.when)
    logger.info(f"{TAG} Alarm set: {intent.summary}")
    return True


def _do_cancel_alarm(ctx, intent: ClockIntent, pkg: str, state) -> bool:
    total = 4
    ambiguous = intent.hour is not None and "no am/pm" in intent.note
    tracked = state.match_alarms(intent.hour, intent.minute, intent.label, ambiguous) if (intent.hour is not None or intent.label) else []
    if intent.cancel_all:
        targets = [(a.hour, a.minute) for a in state.active_alarms()]
    elif intent.hour is None and not intent.label:
        nxt = state.active_alarms()[:1]
        tracked = nxt
        targets = [(a.hour, a.minute) for a in nxt]
    else:
        targets = [(a.hour, a.minute) for a in tracked] or ([(intent.hour, intent.minute)] if intent.hour is not None else [])

    _step(2, total, "Dismissing via the Android Clock intent" if not intent.cancel_all else "Switching every alarm off in the Clock app")
    done: bool | None = None
    if not intent.cancel_all:
        if targets:
            h, m = targets[0]
            extras = [("--es", _X + "SEARCH_MODE", "android.time"), ("--ei", _X + "HOUR", h % 12 or 12), ("--ei", _X + "MINUTES", m),
                      ("--ez", _X + "IS_PM", "true" if h >= 12 else "false"), ("--ez", _X + "SKIP_UI", "true")]
        else:
            extras = [("--es", _X + "SEARCH_MODE", "android.next"), ("--ez", _X + "SKIP_UI", "true")]
        accepted, out = am_start(ctx, ACTION_DISMISS_ALARM, extras, _forced_pkg())
        _sleep(1.0)
        if accepted and targets:
            _step(3, total, "Verifying the alarm is off")
            done = verify_alarm_present(ctx, pkg, targets[0][0], targets[0][1], want_present=False)
    if done is not True:
        _step(3, total, "Switching the alarm off in the Clock app UI")
        if ui_cancel_alarms(ctx, pkg, targets or None, switch_off_all=intent.cancel_all or not targets):
            done = True if intent.cancel_all or not targets else verify_alarm_present(ctx, pkg, targets[0][0], targets[0][1], want_present=False)
        else:
            done = False
    if done is False:
        dump_failure_ui(ctx, "could not switch the alarm off", pkg)
        logger.error(f"{TAG} Could not cancel the alarm. See {FAILURE_DUMP}")
        return False
    if done is None:
        logger.warning(f"{TAG} Cancel sent but could not be confirmed on screen: UNVERIFIED.")
    _step(4, total, "Updating the record")
    ids = [a.id for a in (state.active_alarms() if intent.cancel_all else tracked)]
    state.cancel_alarms(ids)
    logger.info(f"{TAG} Alarm cancelled: {intent.summary}")
    return True


def timer_rows(nodes: list[dict]) -> list[dict]:
    """Running timers on the Timer tab: [{seconds, y, delete}] (delete = that row's delete/stop button, if found)."""
    rows = []
    for n in nodes:
        secs = timer_countdowns([n])
        if not secs:
            continue
        button = None
        for o in nodes:
            text = f"{o['text']} {o['content_desc']}".strip()
            if o["clickable"] and abs(o["y"] - n["y"]) <= 160 and re.search(r"delete|stop|reset|cancel|remove", text, re.IGNORECASE):
                button = o
                break
        rows.append({"seconds": secs[0], "y": n["y"], "delete": button})
    return rows


def ui_stop_one_timer(ctx: WorkflowContext, pkg: str, remaining_s: int) -> bool:
    """Delete the single timer row whose countdown is closest to `remaining_s`."""
    _open_clock(ctx, pkg, "timer")
    rows = [r for r in timer_rows(_clock_nodes(ctx, pkg)) if r["delete"] is not None]
    if not rows:
        return False
    row = min(rows, key=lambda r: abs(r["seconds"] - remaining_s))
    ctx.dispatcher.tap(row["delete"]["x"], row["delete"]["y"])
    _sleep(0.6)
    return True


def _do_stop_timer(ctx, intent: ClockIntent, pkg: str, state) -> bool:
    total = 4
    running = state.running_timers()
    specific = intent.seconds is not None or bool(intent.label)
    targets = [t for t in running if (intent.seconds is None or t.total_s == intent.seconds)
               and (not intent.label or t.label.lower() == intent.label.lower())]
    if specific and not targets:
        logger.error(f"{TAG} No running timer matches {intent.summary!r}. (Only timers set from Re-Node are tracked; "
                     "say 'stop timer' to clear every timer on the phone.)")
        return False
    if specific:
        targets = targets[:1]
    stop_all = not specific or len(targets) == len(running)

    if specific and not stop_all:
        _step(2, total, "Stopping just that timer through the Clock app UI")
        target = targets[0]
        ok = ui_stop_one_timer(ctx, pkg, state.remaining(target))
        if not ok:
            dump_failure_ui(ctx, "could not find that timer's delete button", pkg)
            logger.error(f"{TAG} Could not stop that timer. See {FAILURE_DUMP}")
            return False
        _step(3, total, "Verifying")
        _open_clock(ctx, pkg, "timer")
        left = timer_rows(_clock_nodes(ctx, pkg))
        if any(abs(r["seconds"] - state.remaining(target)) <= 3 for r in left):
            dump_failure_ui(ctx, "timer still listed after delete", pkg)
            logger.error(f"{TAG} The timer is still running. See {FAILURE_DUMP}")
            return False
        state.stop_timer(timer_id=target.id)
        _step(4, total, "Updating the record")
        logger.info(f"{TAG} Timer stopped: {format_duration(target.total_s)}")
        return True

    _step(2, total, "Dismissing timers via the Android Clock intent")
    accepted, out = am_start(ctx, ACTION_DISMISS_TIMER, [], _forced_pkg())
    _sleep(1.0)
    done = verify_no_timers(ctx, pkg) if accepted else None
    if done is not True:
        _step(3, total, "Stopping timers through the Clock app UI")
        ui_stop_timers(ctx, pkg)
        done = verify_no_timers(ctx, pkg)
    if done is False:
        dump_failure_ui(ctx, "timers still running after stop", pkg)
        logger.error(f"{TAG} Could not stop the timers. See {FAILURE_DUMP}")
        return False
    if done is None:
        logger.warning(f"{TAG} Stop sent but the Timer screen was not readable: UNVERIFIED.")
    _step(4, total, "Updating the record")
    state.stop_timer()
    logger.info(f"{TAG} All timers stopped")
    return True


def _do_show(ctx, intent: ClockIntent, pkg: str, state) -> bool:
    which = "timer" if intent.kind == SHOW_TIMERS else "alarm"
    _step(2, 3, f"Opening the Clock app's {which} screen")
    action = ACTION_SHOW_TIMERS if intent.kind == SHOW_TIMERS else ACTION_SHOW_ALARMS
    am_start(ctx, action, [], _forced_pkg())
    _sleep(1.0)
    _open_clock(ctx, pkg, which)
    nodes = _clock_nodes(ctx, pkg)
    if not nodes:
        dump_failure_ui(ctx, f"{which} screen not shown", pkg)
        logger.error(f"{TAG} The Clock app did not open. See {FAILURE_DUMP}")
        return False
    _step(3, 3, "Done")
    return True


def _do_pause(ctx, intent: ClockIntent, pkg: str, state) -> bool:
    logger.error(f"{TAG} Pausing a timer is not supported by Android's Clock intents; use 'stop timer' instead")
    return False


_HANDLERS = {
    SET_TIMER: _do_set_timer, SET_ALARM: _do_set_alarm, CANCEL_ALARM: _do_cancel_alarm, STOP_TIMER: _do_stop_timer,
    SHOW_TIMERS: _do_show, SHOW_ALARMS: _do_show, PAUSE_TIMER: _do_pause,
}


def run_clock(ctx: WorkflowContext, intent: ClockIntent, command: str = "", state: clock_state.ClockState | None = None) -> bool:
    """Execute one parsed clock command. Returns True only when it was carried out (and, wherever the phone
    allows it, confirmed). Never raises for an ordinary failure: the reason is logged and False returned."""
    state = state or clock_state.get_state()
    command = command or intent.raw
    if not intent.ok:
        logger.error(f"{TAG} {intent.error}")
        return False

    logger.info(f"{TAG} Understood: {intent.summary}")
    if intent.note:
        logger.info(f"{TAG} Interpretation: {intent.note}")
    handler = _HANDLERS.get(intent.kind)
    if handler is None:
        logger.error(f"{TAG} Unsupported clock action {intent.kind}")
        return False

    _step(1, 5, "Local model reasoning about the command (offline)")
    try:
        log_brain_reasoning(command, intent.family)
    except Exception as exc:  # noqa: BLE001 -- narration only; never block the action
        logger.warning(f"{TAG} Brain narration skipped: {exc}")

    try:
        pkg = _resolve_package(ctx, config.CLOCK_PACKAGE, config.CLOCK_PACKAGE_CANDIDATES, "clock")
    except WorkflowError as exc:
        logger.error(f"{TAG} {exc}")
        return False
    logger.info(f"{TAG} Clock app: {pkg}")
    try:
        return bool(handler(ctx, intent, pkg, state))
    except WorkflowError as exc:
        logger.error(f"{TAG} {exc}")
        dump_failure_ui(ctx, str(exc), pkg)
        return False
    except Exception as exc:  # noqa: BLE001 -- keep the agent alive; record what the phone showed
        logger.error(f"{TAG} Unexpected error: {type(exc).__name__}: {exc}")
        dump_failure_ui(ctx, f"{type(exc).__name__}: {exc}", pkg)
        return False
