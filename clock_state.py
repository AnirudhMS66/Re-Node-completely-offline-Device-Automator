"""What Re-Node has set on the phone's Clock app (timers + alarms), kept on the PC.

Why this exists: Android gives no clean, vendor-independent way to LIST running timers over adb, so the
control center's countdown list is driven by what the agent itself successfully set. A timer started by
hand on the phone is not visible here (see README_CLOCK.md). Pure standard library, thread-safe, and the
clock is injectable so every rule is unit-tested without waiting.

State is stored as JSON (default: logs/clock_state.json) so a restart of the GUI does not forget timers
that are still running on the phone.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

DONE_KEEP_S = 600          # finished timers stay listed (as "done") for 10 minutes, then are pruned


@dataclass
class TimerRecord:
    id: int
    label: str
    total_s: int
    started_at: float
    status: str = "running"            # running | stopped | done

    @property
    def ends_at(self) -> float:
        return self.started_at + self.total_s


@dataclass
class AlarmRecord:
    id: int
    hour: int
    minute: int
    label: str = ""
    days: list[int] = field(default_factory=list)     # Mon=0 .. Sun=6; empty = one-off
    when_ts: float = 0.0                               # next time it rings (epoch seconds)
    status: str = "active"                             # active | cancelled | fired


def next_ring(hour: int, minute: int, days: list[int], after: datetime) -> datetime:
    """First moment strictly after `after` at hour:minute (on one of `days` if repeating)."""
    for offset in range(8):
        day = after.date() + timedelta(days=offset)
        if days and day.weekday() not in days:
            continue
        cand = datetime(day.year, day.month, day.day, hour, minute)
        if cand > after:
            return cand
    return after + timedelta(days=1)


class ClockState:
    def __init__(self, path: str | os.PathLike | None = None, clock: Callable[[], float] = time.time):
        self._path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._next_id = 1
        self.timers: list[TimerRecord] = []
        self.alarms: list[AlarmRecord] = []
        self._load()

    # ---------------- persistence ----------------
    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self.timers = [TimerRecord(**t) for t in data.get("timers", [])]
            self.alarms = [AlarmRecord(**a) for a in data.get("alarms", [])]
            self._next_id = int(data.get("next_id", 1))
        except Exception:  # noqa: BLE001 -- a corrupt file must never stop the app; start empty
            self.timers, self.alarms, self._next_id = [], [], 1

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"next_id": self._next_id, "timers": [asdict(t) for t in self.timers],
                                       "alarms": [asdict(a) for a in self.alarms]}, indent=1), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass

    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def now(self) -> float:
        return self._clock()

    # ---------------- housekeeping ----------------
    def sweep(self) -> None:
        """Mark finished timers/alarms and prune old ones. Called by every read, cheap."""
        with self._lock:
            now = self._clock()
            changed = False
            for t in self.timers:
                if t.status == "running" and t.ends_at <= now:
                    t.status, changed = "done", True
            keep = [t for t in self.timers if t.status == "running" or (t.status == "done" and now - t.ends_at < DONE_KEEP_S)]
            if len(keep) != len(self.timers):
                self.timers, changed = keep, True
            for a in self.alarms:
                if a.status == "active" and a.when_ts <= now:
                    if a.days:
                        a.when_ts = next_ring(a.hour, a.minute, a.days, datetime.fromtimestamp(now)).timestamp()
                    else:
                        a.status = "fired"
                    changed = True
            kept = [a for a in self.alarms if a.status == "active"]
            if len(kept) != len(self.alarms):
                self.alarms, changed = kept, True
            if changed:
                self._save()

    # ---------------- timers ----------------
    def add_timer(self, seconds: int, label: str = "", started_at: float | None = None) -> TimerRecord:
        with self._lock:
            rec = TimerRecord(self._new_id(), label, int(seconds), self._clock() if started_at is None else started_at)
            self.timers.append(rec)
            self._save()
            return rec

    def remaining(self, rec: TimerRecord) -> int:
        return max(0, int(round(rec.ends_at - self._clock())))

    def running_timers(self) -> list[TimerRecord]:
        self.sweep()
        with self._lock:
            return sorted((t for t in self.timers if t.status == "running"), key=lambda t: t.ends_at)

    def all_timers(self) -> list[TimerRecord]:
        self.sweep()
        with self._lock:
            return sorted(self.timers, key=lambda t: (t.status != "running", t.ends_at))

    def stop_timer(self, timer_id: int | None = None, label: str = "") -> list[TimerRecord]:
        """Stop one timer (by id), the timers with a label, or -- with neither -- every running timer."""
        self.sweep()
        with self._lock:
            hit = [t for t in self.timers if t.status == "running"
                   and (timer_id is None or t.id == timer_id)
                   and (not label or t.label.lower() == label.lower())]
            for t in hit:
                t.status = "stopped"
            self.timers = [t for t in self.timers if t.status != "stopped"]
            if hit:
                self._save()
            return hit

    # ---------------- alarms ----------------
    def add_alarm(self, hour: int, minute: int, label: str = "", days: tuple[int, ...] | list[int] = (),
                  when: datetime | None = None) -> AlarmRecord:
        """Record an alarm. The same time + label + repeat is replaced, not duplicated."""
        with self._lock:
            days = sorted(days)
            if when is None:
                when = next_ring(hour, minute, days, datetime.fromtimestamp(self._clock()))
            self.alarms = [a for a in self.alarms if not (a.hour == hour and a.minute == minute and a.label == label and a.days == days)]
            rec = AlarmRecord(self._new_id(), hour, minute, label, days, when.timestamp())
            self.alarms.append(rec)
            self._save()
            return rec

    def active_alarms(self) -> list[AlarmRecord]:
        self.sweep()
        with self._lock:
            return sorted((a for a in self.alarms if a.status == "active"), key=lambda a: a.when_ts)

    def match_alarms(self, hour: int | None, minute: int | None, label: str = "", ambiguous: bool = False) -> list[AlarmRecord]:
        """Alarms matching a time (hour None = any). `ambiguous` = the user gave no am/pm, so hour and hour+12 both match."""
        out = []
        for a in self.active_alarms():
            if minute is not None and a.minute != minute:
                continue
            if hour is not None:
                if ambiguous and a.hour % 12 != hour % 12:
                    continue
                if not ambiguous and a.hour != hour:
                    continue
            if label and a.label.lower() != label.lower():
                continue
            out.append(a)
        return out

    def cancel_alarms(self, ids: list[int]) -> list[AlarmRecord]:
        with self._lock:
            hit = [a for a in self.alarms if a.id in ids and a.status == "active"]
            for a in hit:
                a.status = "cancelled"
            self.alarms = [a for a in self.alarms if a.status == "active"]
            if hit:
                self._save()
            return hit

    def clear(self) -> None:
        with self._lock:
            self.timers, self.alarms = [], []
            self._save()


_DEFAULT: ClockState | None = None
_DEFAULT_LOCK = threading.Lock()


def get_state() -> ClockState:
    """Process-wide state shared by the workflow (writer) and the GUI (reader)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            try:
                import config
                path = Path(config.LOG_DIR) / "clock_state.json"
            except Exception:  # noqa: BLE001
                path = None
            _DEFAULT = ClockState(path)
        return _DEFAULT


def set_state(state: ClockState | None) -> None:
    """Swap the shared state (tests, --demo)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = state
