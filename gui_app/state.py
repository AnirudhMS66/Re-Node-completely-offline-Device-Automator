"""Pure-Python state for the control center: the Wi-Fi exposure timer and the
free-text command history. No Tk, no threads, no phone -- everything here is unit-tested in
tests/test_gui_logic.py.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON via a temp file + rename so a crash mid-write never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def format_mmss(seconds: float) -> str:
    """00:07, 01:05, 75:00 -- minutes are not wrapped into hours on purpose (short windows)."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# =============================================================================================
# Wi-Fi exposure timer (zero-trust / air-gap evidence)
# =============================================================================================


@dataclass
class ExposureTimer:
    """Measures how long the phone's Wi-Fi radio is ON.

    Feed it every poll with update(state, now). `state` is True/False, or None when the state could
    not be read (phone unplugged, adb error) -- None keeps the previous state rather than inventing an
    OFF edge, so a flaky poll never shortens or splits a window.
    """

    on_since: float | None = None
    last_window_s: float | None = None
    closed_total_s: float = 0.0
    windows: list[float] = field(default_factory=list)
    state: bool | None = None

    def update(self, state: bool | None, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if state is None:
            return
        if state and self.on_since is None:
            self.on_since = now
        elif not state and self.on_since is not None:
            window = max(0.0, now - self.on_since)
            self.windows.append(window)
            self.last_window_s = window
            self.closed_total_s += window
            self.on_since = None
        self.state = state

    @property
    def is_on(self) -> bool:
        return self.on_since is not None

    def current_s(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, now - self.on_since) if self.on_since is not None else 0.0

    def total_s(self, now: float | None = None) -> float:
        return self.closed_total_s + self.current_s(now)

    def labels(self, now: float | None = None) -> dict[str, str]:
        return {
            "current": format_mmss(self.current_s(now)) if self.is_on else "--:--",
            "last": f"{self.last_window_s:.1f} s" if self.last_window_s is not None else "none yet",
            "total": f"{self.total_s(now):.1f} s",
            "windows": str(len(self.windows) + (1 if self.is_on else 0)),
        }


# =============================================================================================
# Free-text command history (Up/Down arrows)
# =============================================================================================


class CommandHistory:
    """Shell-style history for the free-text entry, persisted to logs/gui_history.json."""

    MAX_ENTRIES = 100

    def __init__(self, path: Path | None = None):
        self.path = path
        raw = _read_json(path, []) if path is not None else []
        self.entries: list[str] = [str(e) for e in raw if isinstance(e, str) and e.strip()][-self.MAX_ENTRIES :]
        self._cursor = len(self.entries)

    def add(self, command: str) -> None:
        command = command.strip()
        if not command:
            return
        if not self.entries or self.entries[-1] != command:
            self.entries.append(command)
            del self.entries[: -self.MAX_ENTRIES]
            if self.path is not None:
                try:
                    _atomic_write_json(self.path, self.entries)
                except OSError:
                    pass
        self.reset_cursor()

    def reset_cursor(self) -> None:
        self._cursor = len(self.entries)

    def previous(self) -> str | None:
        """Up arrow: older entry, or None when the history is empty. Stops at the oldest entry."""
        if not self.entries:
            return None
        self._cursor = max(0, self._cursor - 1)
        return self.entries[self._cursor]

    def next(self) -> str:
        """Down arrow: newer entry; past the newest returns "" (an empty entry box)."""
        if self._cursor < len(self.entries) - 1:
            self._cursor += 1
            return self.entries[self._cursor]
        self._cursor = len(self.entries)
        return ""
