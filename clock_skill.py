"""Timer / alarm command parser for Re-Node (pure Python, standard library only).

    parse_clock_command("set alarm for 7:30 am called gym") -> ClockIntent(kind=SET_ALARM, hour=7, minute=30, ...)
    parse_clock_command("mirror")                           -> None   (not a clock command: routing is unchanged)

Design rules
  * Deterministic: regexes and word-number handling decide every number. The local LLM is never involved.
  * Plain text in -> ClockIntent out. A speech-to-text engine can feed the very same function.
  * A command is "claimed" only when its whole sentence is understood: the words that are left after the
    time / duration / day / label parts are removed must all be harmless filler ("please", "set", "a", ...).
    "my alarm app keeps crashing" or "set the table" therefore return None and fall through to the old routing.
  * A command that is clearly a clock command but impossible or incomplete ("alarm at 25:00", "timer 0 seconds",
    "set timer") is returned as an intent with `error` set. The caller shows the message and touches nothing.
  * "alarm at 6" (no am/pm) = the next upcoming occurrence of 06:00 or 18:00 counted from `now`, and the
    interpretation is spelled out in `note` so it can be logged.

Weekday numbers in `days` follow Python: Monday=0 ... Sunday=6.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

SET_TIMER = "SET_TIMER"
STOP_TIMER = "STOP_TIMER"
PAUSE_TIMER = "PAUSE_TIMER"
SET_ALARM = "SET_ALARM"
CANCEL_ALARM = "CANCEL_ALARM"
SHOW_TIMERS = "SHOW_TIMERS"
SHOW_ALARMS = "SHOW_ALARMS"

TIMER_KINDS = (SET_TIMER, STOP_TIMER, PAUSE_TIMER, SHOW_TIMERS)
ALARM_KINDS = (SET_ALARM, CANCEL_ALARM, SHOW_ALARMS)

MAX_TIMER_SECONDS = 24 * 3600
MAX_LABEL_CHARS = 40

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class ClockIntent:
    kind: str
    seconds: int | None = None          # timers: length in seconds
    hour: int | None = None             # alarms: 0-23
    minute: int | None = None           # alarms: 0-59
    label: str = ""
    days: tuple[int, ...] = ()          # repeat days (Mon=0). Empty = one-off
    summary: str = ""                   # human-readable, e.g. "Alarm for 06:30 AM tomorrow"
    error: str = ""                     # non-empty = understood but impossible/incomplete; do not execute
    note: str = ""                      # how an ambiguous time was interpreted (log it)
    when: datetime | None = None        # resolved alarm moment (first occurrence)
    relative: bool = False              # "alarm in 2 hours"
    cancel_all: bool = False
    raw: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def family(self) -> str:
        return "TIMER" if self.kind in TIMER_KINDS else "ALARM"


# =============================================================================================
# Text normalisation
# =============================================================================================

_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

_DAY_PREFIX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_WORD = (r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|days|sday|nesday|rsday|urday)?s?")
_UNIT = r"(?:hours?|hrs?|minutes?|mins?|seconds?|secs?)"


def _words_to_digits(text: str) -> str:
    toks = text.split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        w = toks[i]
        if w in _TENS and i + 1 < len(toks) and toks[i + 1] in _ONES and 1 <= _ONES[toks[i + 1]] <= 9:
            out.append(str(_TENS[w] + _ONES[toks[i + 1]]))
            i += 2
            continue
        if w in _ONES:
            out.append(str(_ONES[w]))
        elif w in _TENS:
            out.append(str(_TENS[w]))
        else:
            out.append(w)
        i += 1
    return " ".join(out)


def _normalize(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"\b([ap])\.m\.?", r"\1m", t)
    t = re.sub(r"\bo'?clock\b", "oclock", t)
    t = t.replace("-", " ")
    t = t.replace("'", "")
    t = re.sub(r"[^a-z0-9:.\s]", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _words_to_digits(t)
    t = re.sub(r"\bnoon\b", "12:00 pm", t)
    t = re.sub(r"\bmidnight\b", "12:00 am", t)
    t = re.sub(r"\bhalf\s+(?:an?\s+)?hour\b", "30 minutes", t)
    t = re.sub(r"\b(?:a|an)\s+(hour|hr|minute|min|second|sec)s?\b", r"1 \1", t)
    t = re.sub(r"\b(\d+)\s+(?:hours?|hrs?)\s+and\s+(?:a\s+)?half\b", r"\1 hour 30 minutes", t)
    t = re.sub(r"\b(?:a\s+)?quarter\s+(past|after|to|till|before)\b", r"15 \1", t)
    t = re.sub(r"\bhalf\s+(past|after)\b", r"30 \1", t)
    t = re.sub(r"\bto\s+(\d{1,2})(?=\s*(?:am|pm|oclock|$))", r"to \1", t)
    return re.sub(r"\s+", " ", t).strip()


# =============================================================================================
# Pieces: label, days, duration, time of day
# =============================================================================================

_LABEL_RE = re.compile(
    r"\b(?:called|named|labell?ed|label|titled)\s+(?P<label>.+?)(?=\s+(?:for|at|in|on|every|tomorrow|today)\b|$)",
    re.IGNORECASE,
)


def _extract_label(raw: str) -> tuple[str, str, str]:
    """(label, text-without-label, error). The label keeps its original capitalisation but is reduced to
    characters that are safe to hand to `adb shell` (no quotes, no shell metacharacters)."""
    m = _LABEL_RE.search(raw)
    if not m:
        return "", raw, ""
    label = re.sub(r"[^A-Za-z0-9 _.,:-]", "", m.group("label"))
    label = re.sub(r"\s+", " ", label).strip()[:MAX_LABEL_CHARS].strip()
    rest = raw[:m.start()] + " " + raw[m.end():]
    if not label:
        return "", rest, "The label is empty after removing unsupported characters"
    return label, rest, ""


def _expand_days(tokens: list[str]) -> tuple[int, ...]:
    out: list[int] = []
    for tok in tokens:
        key = tok[:3]
        if key in _DAY_PREFIX and _DAY_PREFIX[key] not in out:
            out.append(_DAY_PREFIX[key])
    return tuple(sorted(out))


def _extract_days(norm: str) -> tuple[str, tuple[int, ...], str | None, str | None]:
    """-> (text without day words, repeat_days, day_word 'today'/'tomorrow'/None, single weekday name/None)."""
    repeat: tuple[int, ...] = ()
    m = re.search(r"\b(?:every\s+day|everyday|daily|each\s+day)\b", norm)
    if m:
        repeat = tuple(range(7))
        norm = norm[:m.start()] + " " + norm[m.end():]
    m = re.search(r"\b(?:every\s+)?weekdays?\b", norm)
    if m:
        repeat = (0, 1, 2, 3, 4)
        norm = norm[:m.start()] + " " + norm[m.end():]
    m = re.search(r"\b(?:every\s+)?weekends?\b", norm)
    if m:
        repeat = (5, 6)
        norm = norm[:m.start()] + " " + norm[m.end():]
    m = re.search(rf"\bevery\s+(?P<list>{_DAY_WORD}(?:\s*(?:,|and|&)?\s*(?:and\s+)?{_DAY_WORD})*)\b", norm)
    if m:
        names = re.findall(_DAY_WORD, m.group("list"))
        repeat = _expand_days(names)
        norm = norm[:m.start()] + " " + norm[m.end():]

    day_word = None
    m = re.search(r"\b(tomorrow|today|tonight)\b", norm)
    if m:
        day_word = "tomorrow" if m.group(1) == "tomorrow" else "today"
        norm = norm[:m.start()] + " " + norm[m.end():]

    weekday = None
    if not repeat:
        m = re.search(rf"\b(?:on\s+)?(?:next\s+|this\s+)?(?P<d>{_DAY_WORD})\b", norm)
        if m:
            weekday = m.group("d")
            norm = norm[:m.start()] + " " + norm[m.end():]
    return re.sub(r"\s+", " ", norm).strip(), repeat, day_word, weekday


_DURATION_PART = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*(?P<u>hours?|hrs?|minutes?|mins?|seconds?|secs?)\b")
_DURATION_COMPACT = re.compile(r"(?<![\d.])(?:(?<=\d[hms])|(?<![a-z]))(?P<n>\d+(?:\.\d+)?)(?P<u>h|m|s)(?![a-z])")


def _unit_seconds(unit: str) -> int:
    u = unit[0]
    return 3600 if u == "h" else 60 if u == "m" else 1


def _extract_duration(norm: str) -> tuple[str, int | None]:
    """Sum every 'N unit' piece ('2 min 30 sec', '1 hour', '90 seconds', '1.5 hours'). Returns the text with
    those pieces (and the 'and' / ',' between them) removed, and the total seconds (None if none found)."""
    total = 0.0
    found = False
    spans: list[tuple[int, int]] = []
    for rx in (_DURATION_PART, _DURATION_COMPACT):
        for m in rx.finditer(norm):
            if any(s <= m.start() < e for s, e in spans):
                continue
            total += float(m.group("n")) * _unit_seconds(m.group("u"))
            spans.append((m.start(), m.end()))
            found = True
    if not found:
        return norm, None
    out, last = [], 0
    for s, e in sorted(spans):
        out.append(norm[last:s])
        last = e
    out.append(norm[last:])
    text = " ".join(out)
    text = re.sub(r"\b(?:and)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip(), int(round(total))


@dataclass
class _Time:
    hour: int
    minute: int
    ampm: str | None          # "am" / "pm" / None
    exact24: bool             # written as 24-hour time (13:00, 0:30, 07:30)


_TIME_PATTERNS = [
    ("rel", re.compile(r"\b(?P<m>\d{1,2})\s+(?P<dir>past|after|to|till|before)\s+(?P<h>\d{1,2})(?:\s*(?P<ap>am|pm))?\b")),
    ("hhmm", re.compile(r"\b(?P<h>\d{1,2})[:.](?P<m>\d{2})\s*(?P<ap>am|pm)?\b")),
    ("h_mm", re.compile(r"\b(?P<h>\d{1,2})\s+(?P<m>\d{2})\s*(?P<ap>am|pm)?\b")),
    ("h_ap", re.compile(r"\b(?P<h>\d{1,2})\s*(?P<ap>am|pm)\b")),
    ("oclock", re.compile(r"\b(?P<h>\d{1,2})\s*oclock(?:\s*(?P<ap>am|pm))?\b")),
    ("hm4", re.compile(r"\b(?P<hm>\d{3,4})\s*(?P<ap>am|pm)?\b")),
    ("h", re.compile(r"\b(?P<h>\d{1,2})\b")),
]


def _extract_time(norm: str) -> tuple[str, _Time | None, str]:
    """-> (text without the time, _Time or None, error)."""
    for kind, rx in _TIME_PATTERNS:
        m = rx.search(norm)
        if not m:
            continue
        rest = (norm[:m.start()] + " " + norm[m.end():]).strip()
        ap = (m.groupdict().get("ap") or None)
        if kind == "rel":
            minutes, direction, hour = int(m.group("m")), m.group("dir"), int(m.group("h"))
            if not 0 < minutes < 60:
                return norm, None, f"{minutes} minutes past/to the hour is not a valid time"
            if not 1 <= hour <= 12 and not (ap is None and 0 <= hour <= 23):
                return norm, None, f"{hour} is not a valid hour"
            if direction in ("to", "till", "before"):
                hour = (hour - 1) % 24 if hour != 0 else 23
                minutes = 60 - minutes
            return re.sub(r"\s+", " ", rest), _Time(hour, minutes, ap, ap is None and hour > 12), ""
        if kind == "hm4":
            digits = m.group("hm")
            hour, minute = int(digits[:-2]), int(digits[-2:])
            exact = True
        else:
            hour = int(m.group("h"))
            minute = int(m.group("m")) if "m" in m.groupdict() and m.group("m") else 0
            exact = (m.group("h").startswith("0") and len(m.group("h")) == 2) or hour == 0 or hour > 12
        if minute > 59:
            return norm, None, f"{minute} is not a valid minute"
        if ap:
            if not 1 <= hour <= 12:
                return norm, None, f"'{hour} {ap}' is not a valid 12-hour time"
            exact = False
        elif hour > 23:
            return norm, None, f"{hour}:{minute:02d} is not a valid time"
        return re.sub(r"\s+", " ", rest), _Time(hour, minute, ap, exact), ""
    return norm, None, ""


# =============================================================================================
# Formatting helpers
# =============================================================================================


def format_time12(hour: int, minute: int) -> str:
    return f"{(hour % 12) or 12:02d}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def format_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} min")
    if s:
        parts.append(f"{s} sec")
    return " ".join(parts) or "0 sec"


def format_days(days: tuple[int, ...]) -> str:
    if len(days) == 7:
        return "every day"
    if days == (0, 1, 2, 3, 4):
        return "weekdays"
    if days == (5, 6):
        return "weekends"
    return "every " + ", ".join(WEEKDAY_NAMES[d] for d in days)


def _day_label(when: datetime, now: datetime) -> str:
    delta = (when.date() - now.date()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return when.strftime("%a %d %b")


# =============================================================================================
# Alarm time resolution
# =============================================================================================


def _resolve_alarm_time(t: _Time, now: datetime, day_word: str | None, weekday: str | None,
                        repeat: tuple[int, ...]) -> tuple[datetime | None, str, str]:
    """-> (when, reason, error)."""
    if t.ampm:
        h24 = t.hour % 12 + (12 if t.ampm == "pm" else 0)
        cands = [(h24, t.minute)]
        reason = "exact time"
    elif t.exact24:
        cands = [(t.hour, t.minute)]
        reason = "24-hour time"
    else:
        base = t.hour % 12
        cands = [(base, t.minute), (base + 12, t.minute)]
        reason = "next occurrence"

    def at(date, hm):
        return datetime(date.year, date.month, date.day, hm[0], hm[1])

    today = now.date()
    if repeat:
        for offset in range(8):
            date = today + timedelta(days=offset)
            if date.weekday() not in repeat:
                continue
            options = sorted(at(date, hm) for hm in cands)
            options = [o for o in options if o > now]
            if options:
                return options[0], reason + ", repeating", ""
        return None, "", "Could not find the next repeat day"
    if day_word == "tomorrow":
        date = today + timedelta(days=1)
        options = sorted(at(date, hm) for hm in cands)
        return options[0], (reason if len(cands) == 1 else "assumed the earlier time"), ""
    if day_word == "today":
        options = sorted(o for o in (at(today, hm) for hm in cands) if o > now)
        if not options:
            return None, "", "That time has already passed today (say 'tomorrow' or drop 'today')"
        return options[0], reason, ""
    if weekday:
        target = _DAY_PREFIX[weekday[:3]]
        for offset in range(8):
            date = today + timedelta(days=offset)
            if date.weekday() != target:
                continue
            options = sorted(o for o in (at(date, hm) for hm in cands) if o > now)
            if options:
                return options[0], reason, ""
        return None, "", "Could not resolve that weekday"
    options = []
    for offset in (0, 1):
        options += [at(today + timedelta(days=offset), hm) for hm in cands]
    options = sorted(o for o in options if o > now)
    return options[0], reason, ""


# =============================================================================================
# Intent builders
# =============================================================================================

_TIMER_SET_OK = {
    "set", "start", "begin", "create", "make", "run", "put", "launch", "a", "an", "the", "my", "me", "new",
    "for", "of", "timer", "timers", "please", "can", "could", "you", "would", "i", "want", "need", "like",
    "to", "kindly", "hey", "ok", "okay", "up", "countdown", "go", "on", "then",
}
_STOP_VERBS = {"stop", "cancel", "delete", "end", "dismiss", "remove", "kill", "clear", "reset", "off"}
_STOP_OK = _STOP_VERBS | {
    "turn", "switch", "the", "my", "a", "all", "running", "current", "active", "timer", "timers", "please",
    "now", "it", "can", "could", "you", "kindly", "this", "that", "every", "any",
}
_SHOW_VERBS = {"show", "open", "list", "view", "see", "display", "check", "what", "which"}
_SHOW_OK = _SHOW_VERBS | {
    "my", "the", "any", "all", "active", "running", "timer", "timers", "alarm", "alarms", "me", "please",
    "go", "to", "launch", "current", "are", "is", "there", "do", "i", "have", "set", "scheduled",
}
_ALARM_SET_OK = {
    "set", "start", "create", "make", "put", "add", "schedule", "a", "an", "the", "my", "me", "new", "for",
    "at", "by", "on", "in", "alarm", "alarms", "wake", "up", "please", "can", "could", "you", "would", "i",
    "want", "need", "like", "to", "kindly", "hey", "ok", "okay", "oclock", "of", "next", "this", "from", "now",
}
_ALARM_CANCEL_VERBS = {"cancel", "delete", "remove", "dismiss", "stop", "disable", "clear", "off"}
_ALARM_CANCEL_OK = _ALARM_CANCEL_VERBS | {
    "turn", "switch", "the", "my", "a", "an", "alarm", "alarms", "please", "all", "at", "for", "next", "that",
    "this", "it", "now", "can", "could", "you", "kindly", "oclock", "ringing", "current", "on",
}


def _only(tokens, allowed) -> bool:
    return all(t in allowed for t in tokens)


def _err(kind: str, message: str, raw: str) -> ClockIntent:
    return ClockIntent(kind=kind, error=message, summary="", raw=raw)


def _parse_timer(raw: str, norm: str, label: str, label_error: str) -> ClockIntent | None:
    toks = norm.split()
    verbs = set(toks)
    if verbs & _STOP_VERBS and not (verbs & {"set", "start", "begin", "create", "make"}):
        stop_rest, stop_secs = _extract_duration(norm)
        if _only(stop_rest.split(), _STOP_OK):
            if label_error:
                return _err(STOP_TIMER, label_error, raw)
            if stop_secs is not None and stop_secs <= 0:
                return _err(STOP_TIMER, "Say which timer to stop, e.g. 'stop the 5 minute timer'", raw)
            which = f" ({format_duration(stop_secs)})" if stop_secs else (f" ({label})" if label else "")
            return ClockIntent(kind=STOP_TIMER, seconds=stop_secs, label=label, summary="Stop timer" + which, raw=raw)
        return None
    if "pause" in verbs:
        if _only(toks, (_STOP_OK | {"pause"}) - _STOP_VERBS):
            return ClockIntent(kind=PAUSE_TIMER, summary="Pause timer", raw=raw)
        return None
    if verbs & _SHOW_VERBS:
        if _only(toks, _SHOW_OK):
            return ClockIntent(kind=SHOW_TIMERS, summary="Show timers", raw=raw)
        return None

    rest, seconds = _extract_duration(norm)
    rest_toks = rest.split()
    if seconds is None:
        digits = [t for t in rest_toks if re.fullmatch(r"\d+(?:\.\d+)?", t)]
        others = [t for t in rest_toks if t not in digits]
        if len(digits) == 1 and _only(others, _TIMER_SET_OK):
            return _err(SET_TIMER, f"Say the unit for the timer, e.g. '{digits[0]} minutes' or '{digits[0]} seconds'", raw)
        if not digits and _only(others, _TIMER_SET_OK) and (verbs & {"set", "start", "begin", "create", "make"}):
            return _err(SET_TIMER, "How long should the timer run? e.g. 'set timer for 10 minutes'", raw)
        return None
    if not _only(rest_toks, _TIMER_SET_OK):
        return None
    if label_error:
        return _err(SET_TIMER, label_error, raw)
    if seconds <= 0:
        return _err(SET_TIMER, "A timer needs a duration longer than 0 seconds", raw)
    if seconds > MAX_TIMER_SECONDS:
        return _err(SET_TIMER, "Timers longer than 24 hours are not supported", raw)
    summary = f"Timer for {format_duration(seconds)}" + (f" ({label})" if label else "")
    return ClockIntent(kind=SET_TIMER, seconds=seconds, label=label, summary=summary, raw=raw)


def _parse_alarm(raw: str, norm: str, label: str, label_error: str, now: datetime) -> ClockIntent | None:
    toks = norm.split()
    verbs = set(toks)
    is_cancel = bool(verbs & _ALARM_CANCEL_VERBS) and not (verbs & {"set", "add", "create", "make", "schedule", "wake"})
    if is_cancel and "turn" in verbs and "off" not in verbs and "stop" not in verbs and not (verbs & _ALARM_CANCEL_VERBS - {"off"}):
        return None
    is_show = bool(verbs & _SHOW_VERBS) and not is_cancel and not (verbs & {"wake", "add", "create", "make", "schedule"})

    if is_show:
        if _only(toks, _SHOW_OK):
            return ClockIntent(kind=SHOW_ALARMS, summary="Show alarms", raw=raw)
        return None

    rest, repeat, day_word, weekday = _extract_days(norm)
    rest, seconds = _extract_duration(rest)
    relative = False
    if seconds is not None:
        # "in 45 minutes": the word 'in' directly precedes the duration; it is harmless filler either way
        relative = True
    rest, tm, time_error = ("", None, "") if relative else _extract_time(rest)
    rest_toks = rest.split() if rest else []
    if relative:
        rest_toks = rest_toks

    if is_cancel:
        if not _only(rest_toks, _ALARM_CANCEL_OK) or relative or repeat or day_word or weekday:
            return None
        if label_error:
            return _err(CANCEL_ALARM, label_error, raw)
        if time_error:
            return _err(CANCEL_ALARM, time_error, raw)
        cancel_all = "all" in toks
        if tm is None:
            summary = "Cancel all alarms" if cancel_all else "Cancel the next alarm"
            return ClockIntent(kind=CANCEL_ALARM, label=label, cancel_all=cancel_all, summary=summary, raw=raw)
        if tm.ampm:
            hour = tm.hour % 12 + (12 if tm.ampm == "pm" else 0)
            summary = f"Cancel alarm {format_time12(hour, tm.minute)}"
        elif tm.exact24:
            hour = tm.hour
            summary = f"Cancel alarm {format_time12(hour, tm.minute)}"
        else:
            hour = tm.hour % 12  # ambiguous: matched by minute + hour-of-12 later; record the AM reading
            summary = f"Cancel alarm {tm.hour}:{tm.minute:02d}"
        return ClockIntent(kind=CANCEL_ALARM, hour=hour, minute=tm.minute, label=label,
                           note="" if (tm.ampm or tm.exact24) else "no am/pm given: matches either 12-hour reading",
                           summary=summary, raw=raw)

    # ---- set alarm ---------------------------------------------------------------------
    if time_error:
        return _err(SET_ALARM, time_error, raw)
    if not _only(rest_toks, _ALARM_SET_OK):
        return None
    if label_error:
        return _err(SET_ALARM, label_error, raw)

    if relative:
        if seconds is None or seconds < 60:
            return _err(SET_ALARM, "Alarms have minute precision; use a timer for less than a minute", raw)
        if seconds > MAX_TIMER_SECONDS:
            return _err(SET_ALARM, "'alarm in ...' supports up to 24 hours; give a clock time instead", raw)
        when = now + timedelta(seconds=seconds)
        if when.second >= 30:
            when += timedelta(minutes=1)
        when = when.replace(second=0, microsecond=0)
        note = f"{raw!r} -> {format_time12(when.hour, when.minute)} {_day_label(when, now)} (now + {format_duration(seconds)})"
        summary = f"Alarm for {format_time12(when.hour, when.minute)} {_day_label(when, now)}" + (f" ({label})" if label else "")
        return ClockIntent(kind=SET_ALARM, seconds=seconds, hour=when.hour, minute=when.minute, label=label, when=when,
                           relative=True, note=note, summary=summary, raw=raw)

    if tm is None:
        return _err(SET_ALARM, "What time? e.g. 'set alarm for 7:30 am'", raw)
    when, reason, error = _resolve_alarm_time(tm, now, day_word, weekday, repeat)
    if error or when is None:
        return _err(SET_ALARM, error or "Could not work out that time", raw)
    day = _day_label(when, now)
    time12 = format_time12(when.hour, when.minute)
    repeat_text = f", {format_days(repeat)}" if repeat else ""
    note = f"{raw!r} -> {time12} {day} ({reason})"
    summary = f"Alarm for {time12} {day}{repeat_text}" + (f" ({label})" if label else "")
    return ClockIntent(kind=SET_ALARM, hour=when.hour, minute=when.minute, label=label, days=repeat, when=when,
                       note=note, summary=summary, raw=raw)


def parse_clock_command(text: str, now: datetime | None = None) -> ClockIntent | None:
    """Parse a typed (or transcribed) timer/alarm command. None = not a clock command (route as before)."""
    if not isinstance(text, str):
        return None
    raw = " ".join(text.split())
    if not raw or len(raw) > 200:
        return None
    now = now or datetime.now()
    label, without_label, label_error = _extract_label(raw)
    norm = _normalize(without_label)
    toks = set(norm.split())
    has_timer = bool(toks & {"timer", "timers"})
    has_alarm = bool(toks & {"alarm", "alarms"})
    wake_me = "wake" in toks and "me" in toks
    if has_timer and has_alarm:
        return None
    if has_timer:
        return _parse_timer(raw, norm, label, label_error)
    if has_alarm or wake_me:
        return _parse_alarm(raw, norm, label, label_error, now)
    return None


def stop_timer_command(seconds: int, label: str = "") -> str:
    """The typed command that stops one specific timer (used by the control center's per-timer Stop button)."""
    return f"stop timer called {label}" if label else f"stop the {int(seconds)} second timer"
