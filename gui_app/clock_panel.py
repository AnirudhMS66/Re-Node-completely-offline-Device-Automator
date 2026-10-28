"""The "Timers & alarms" panel of the control center.

Every button builds a plain-English command ("set timer for 5 minutes", "set alarm for 7:30 am") and sends it
through exactly the same path as a command typed in the box (ControlCenter.run_goal -> agent -> clock parser ->
clock workflow). Nothing here talks to the phone. The countdown list reads clock_state.ClockState, which records
what the agent itself set.

The command-building / formatting helpers at the top are pure functions (unit-tested without Tk).
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk
from typing import Callable

from clock_skill import ClockIntent, format_days, format_duration, format_time12, parse_clock_command, stop_timer_command
from clock_state import AlarmRecord, ClockState, TimerRecord
from gui_app import widgets as W

PRESET_MINUTES = (1, 5, 10, 15)
MAX_ROWS = 3


# =============================================================================================
# Pure helpers
# =============================================================================================


def timer_command(minutes: int, seconds: int) -> str:
    parts = []
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if seconds:
        parts.append(f"{seconds} second" + ("s" if seconds != 1 else ""))
    return "set timer for " + " ".join(parts) if parts else ""


def alarm_command(hour: str, minute: str, ampm: str, label: str = "") -> tuple[str, str]:
    """-> (command, error). `hour` 1-12 and `minute` 0-59 come from text boxes, so they are validated here."""
    try:
        h, m = int(hour), int(minute or 0)
    except ValueError:
        return "", "Hour and minute must be numbers"
    if not 1 <= h <= 12:
        return "", "Hour must be 1 to 12 (use the AM / PM switch)"
    if not 0 <= m <= 59:
        return "", "Minute must be 0 to 59"
    cmd = f"set alarm for {h}:{m:02d} {ampm.lower()}"
    label = " ".join((label or "").split())
    if label:
        cmd += f" called {label}"
    return cmd, ""


def parse_spin(text: str, low: int, high: int) -> int | None:
    try:
        value = int(str(text).strip() or 0)
    except ValueError:
        return None
    return value if low <= value <= high else None


def clock_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def rings_in(when_ts: float, now: float) -> str:
    seconds = max(0, int(when_ts - now))
    if seconds < 90:
        return f"in {seconds} s"
    minutes = seconds // 60
    if minutes < 120:
        return f"in {minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"in {hours} h {minutes:02d} m"
    return f"in {hours // 24} days"


def timer_row_text(rec: TimerRecord, remaining: int) -> tuple[str, str]:
    return (rec.label or format_duration(rec.total_s), clock_hms(remaining))


def alarm_row_text(rec: AlarmRecord, now: float) -> tuple[str, str, str]:
    detail = rec.label or ""
    if rec.days:
        detail = (detail + "  " if detail else "") + format_days(tuple(rec.days))
    return (format_time12(rec.hour, rec.minute), detail, rings_in(rec.when_ts, now))


# =============================================================================================
# The panel
# =============================================================================================


class ClockPanel:
    def __init__(self, parent: tk.Widget, *, state: ClockState, run_command: Callable[[str], bool],
                 parse: Callable[[str], ClockIntent | None] = parse_clock_command):
        self.state = state
        self.run_command = run_command
        self.parse = parse
        self._enabled = True
        self._timer_ids: tuple = ()
        self._alarm_ids: tuple = ()
        self._timer_labels: dict[int, tuple[tk.Label, tk.Label]] = {}
        self._alarm_labels: dict[int, tk.Label] = {}
        self._buttons: list[ttk.Button] = []

        self.card = W.Card(parent, "Timers & alarms", W.ORANGE)
        body = self.card.body
        body.columnconfigure(0, weight=1)

        self._section(body, 0, "TIMER")
        row = ttk.Frame(body, style="Card.TFrame")
        row.configure(borderwidth=0)
        row.grid(row=1, column=0, sticky="ew")
        self.min_var, self.sec_var = tk.StringVar(value="5"), tk.StringVar(value="0")
        self.min_spin = self._spin(row, self.min_var, 0, 1439, 5)
        tk.Label(row, text="min", bg=W.CARD, fg=W.MUTED, font=W.FONT).pack(side="left", padx=(3, 8))
        self.sec_spin = self._spin(row, self.sec_var, 0, 59, 3)
        tk.Label(row, text="sec", bg=W.CARD, fg=W.MUTED, font=W.FONT).pack(side="left", padx=(3, 8))
        self.start_button = self._button(row, "Start", self.on_start_timer, accent=True)
        self.start_button.pack(side="left", padx=(2, 0))
        self.stop_all_button = self._button(row, "Stop all", self.on_stop_all)
        self.stop_all_button.pack(side="left", padx=(6, 0))

        presets = ttk.Frame(body, style="Card.TFrame")
        presets.configure(borderwidth=0)
        presets.grid(row=2, column=0, sticky="w", pady=(4, 2))
        tk.Label(presets, text="Quick:", bg=W.CARD, fg=W.MUTED, font=W.FONT).pack(side="left", padx=(0, 6))
        for minutes in PRESET_MINUTES:
            b = self._button(presets, f"{minutes} min", lambda m=minutes: self.on_preset(m))
            b.pack(side="left", padx=(0, 4))

        self.timer_list = ttk.Frame(body, style="Card.TFrame")
        self.timer_list.configure(borderwidth=0)
        self.timer_list.grid(row=3, column=0, sticky="ew")
        self.timer_list.columnconfigure(0, weight=1)

        self._section(body, 4, "ALARM", top=6)
        arow = ttk.Frame(body, style="Card.TFrame")
        arow.configure(borderwidth=0)
        arow.grid(row=5, column=0, sticky="ew")
        self.hour_var, self.alarm_min_var = tk.StringVar(value="7"), tk.StringVar(value="30")
        self.hour_spin = self._spin(arow, self.hour_var, 1, 12, 3)
        tk.Label(arow, text=":", bg=W.CARD, fg=W.TEXT, font=W.FONT_BOLD).pack(side="left", padx=2)
        self.alarm_min_spin = self._spin(arow, self.alarm_min_var, 0, 59, 3, fmt="%02.0f")
        self.ampm = tk.StringVar(value="AM")
        self.ampm_buttons = {}
        seg = tk.Frame(arow, bg=W.CARD)
        seg.pack(side="left", padx=(8, 8))
        for value in ("AM", "PM"):
            lab = tk.Label(seg, text=value, font=W.FONT_BOLD, padx=7, pady=3, cursor="hand2")
            lab.pack(side="left")
            lab.bind("<Button-1>", lambda _e, v=value: self._set_ampm(v))
            self.ampm_buttons[value] = lab
        self._set_ampm("AM")
        self.label_var = tk.StringVar()
        self.label_entry = ttk.Entry(arow, textvariable=self.label_var, width=6, font=W.FONT)
        self.label_entry.pack(side="left", fill="x", expand=True)
        self.label_entry.bind("<Return>", lambda _e: self.on_set_alarm())
        W.Tooltip(self.label_entry, "Optional label, e.g. gym")
        self.set_alarm_button = self._button(arow, "Set", self.on_set_alarm, accent=True)
        self.set_alarm_button.pack(side="left", padx=(6, 0))

        self.alarm_list = ttk.Frame(body, style="Card.TFrame")
        self.alarm_list.configure(borderwidth=0)
        self.alarm_list.grid(row=6, column=0, sticky="ew", pady=(2, 0))
        self.alarm_list.columnconfigure(1, weight=1)

        self.status = tk.Label(body, text="Type a command or use the buttons.", bg=W.CARD, fg=W.MUTED, font=W.FONT,
                               anchor="w", justify="left", wraplength=300)
        self.status.grid(row=7, column=0, sticky="ew", pady=(6, 0))
        self.refresh()

    # ---------------- widget factories ----------------
    def _section(self, parent, row: int, text: str, top: int = 0) -> None:
        tk.Label(parent, text=text, bg=W.CARD, fg=W.MUTED, font=W.FONT_BOLD, anchor="w").grid(row=row, column=0, sticky="w", pady=(top, 3))

    def _spin(self, parent, var: tk.StringVar, low: int, high: int, width: int, fmt: str = "") -> tk.Spinbox:
        kw = dict(from_=low, to=high, width=width, textvariable=var, font=W.FONT, bg=W.INPUT, fg=W.TEXT, insertbackground=W.TEXT,
                  buttonbackground=W.BTN, relief="flat", highlightthickness=1, highlightbackground=W.BORDER, highlightcolor=W.ACCENT,
                  justify="center", wrap=True, disabledbackground=W.BTN_DIS, disabledforeground="#566579")
        if fmt:
            kw["format"] = fmt
        spin = tk.Spinbox(parent, **kw)
        spin.pack(side="left")
        return spin

    def _button(self, parent, text: str, command, accent: bool = False) -> ttk.Button:
        b = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if accent else "Small.TButton")
        if accent:
            b.configure(padding=(10, 3))
        self._buttons.append(b)
        return b

    def _set_ampm(self, value: str) -> None:
        self.ampm.set(value)
        for name, lab in self.ampm_buttons.items():
            on = name == value
            lab.configure(bg=W.ACCENT_LO if on else W.BTN, fg="#ffffff" if on else W.MUTED)

    # ---------------- actions ----------------
    def submit(self, command: str) -> bool:
        """Show how the command is understood, then run it through the normal path. False = not started."""
        intent = self.parse(command)
        if intent is None:
            self.show_text("Not a timer or alarm command.", W.WARN)
            return False
        if not intent.ok:
            self.show_text(intent.error, W.BAD)
            return False
        self.show_interpretation(intent)
        return bool(self.run_command(command))

    def on_start_timer(self) -> None:
        m, s = parse_spin(self.min_var.get(), 0, 1439), parse_spin(self.sec_var.get(), 0, 59)
        if m is None or s is None:
            self.show_text("Minutes must be 0 to 1439 and seconds 0 to 59", W.BAD)
            return
        cmd = timer_command(m, s)
        if not cmd:
            self.show_text("Pick a duration longer than 0 seconds", W.BAD)
            return
        self.submit(cmd)

    def on_preset(self, minutes: int) -> None:
        self.min_var.set(str(minutes))
        self.sec_var.set("0")
        self.submit(timer_command(minutes, 0))

    def on_stop_all(self) -> None:
        self.submit("stop timer")

    def on_set_alarm(self) -> None:
        cmd, error = alarm_command(self.hour_var.get(), self.alarm_min_var.get(), self.ampm.get(), self.label_var.get())
        if error:
            self.show_text(error, W.BAD)
            return
        self.submit(cmd)

    # ---------------- status line ----------------
    def show_text(self, text: str, colour: str = W.MUTED) -> None:
        self.status.configure(text=text, fg=colour)

    def show_interpretation(self, intent: ClockIntent) -> None:
        reason = re.search(r"\(([^()]*)\)\s*$", intent.note or "")
        extra = f"  -  {reason.group(1)}" if reason else ""
        self.show_text(f"Understood: {intent.summary}{extra}", W.GREEN)

    def show_typed(self, text: str) -> None:
        """Live interpretation of whatever is in the main command box (called on every keystroke)."""
        text = text.strip()
        if not text:
            return
        intent = self.parse(text)
        if intent is None:
            return
        if intent.ok:
            self.show_interpretation(intent)
        else:
            self.show_text(intent.error, W.BAD)

    def show_result(self, ok: bool, text: str) -> None:
        self.show_text(text, W.OK if ok else W.BAD)

    # ---------------- enabling ----------------
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        state = "normal" if enabled else "disabled"
        self._buttons = [b for b in self._buttons if b.winfo_exists()]
        for b in self._buttons:
            b.configure(state=state)
        for spin in (self.min_spin, self.sec_spin, self.hour_spin, self.alarm_min_spin):
            spin.configure(state="normal" if enabled else "disabled")
        self.label_entry.configure(state="normal" if enabled else "disabled")

    # ---------------- live lists ----------------
    def refresh(self) -> None:
        try:
            self._refresh_timers()
            self._refresh_alarms()
        except tk.TclError:
            pass

    def _refresh_timers(self) -> None:
        timers = self.state.all_timers()
        running = [t for t in timers if t.status == "running"][:MAX_ROWS]
        extra = len([t for t in timers if t.status == "running"]) - len(running)
        done = [t for t in timers if t.status == "done"][:1] if not running else []
        shown = running + done
        ids = tuple((t.id, t.status) for t in shown) + (extra,)
        if ids != self._timer_ids:
            self._timer_ids = ids
            for w in self.timer_list.winfo_children():
                w.destroy()
            self._timer_labels.clear()
            if not shown:
                tk.Label(self.timer_list, text="No timers running", bg=W.CARD, fg=W.MUTED, font=W.FONT, anchor="w").grid(row=0, column=0, sticky="w")
            for i, t in enumerate(shown):
                name = tk.Label(self.timer_list, bg=W.CARD, fg=W.TEXT, font=W.FONT, anchor="w")
                name.grid(row=i, column=0, sticky="w")
                left = tk.Label(self.timer_list, bg=W.CARD, fg=W.CYAN, font=(W.MONO_FAMILY, 12, "bold"), width=8, anchor="e")
                left.grid(row=i, column=1, padx=(6, 6))
                self._timer_labels[t.id] = (name, left)
                if t.status == "running":
                    b = ttk.Button(self.timer_list, text="Stop", style="Small.TButton",
                                   command=lambda t=t: self.submit(stop_timer_command(t.total_s, t.label)))
                    b.configure(padding=(8, 1))
                    b.grid(row=i, column=2)
                    W.Tooltip(b, "Stops just this timer on the phone")
                    b.configure(state="normal" if self._enabled else "disabled")
                    self._buttons.append(b)
            if extra > 0:
                tk.Label(self.timer_list, text=f"+{extra} more running", bg=W.CARD, fg=W.MUTED, font=W.FONT).grid(row=len(shown), column=0, sticky="w")
        for t in shown:
            name, left = self._timer_labels.get(t.id, (None, None))
            if name is None:
                continue
            title, _ = timer_row_text(t, 0)
            name.configure(text=title if len(title) <= 16 else title[:15] + "~")
            if t.status == "done":
                left.configure(text="DONE", fg=W.OK)
            else:
                left.configure(text=clock_hms(self.state.remaining(t)), fg=W.CYAN)

    def _refresh_alarms(self) -> None:
        alarms = self.state.active_alarms()
        shown = alarms[:MAX_ROWS - 1]
        extra = len(alarms) - len(shown)
        ids = tuple(a.id for a in shown) + (extra,)
        now = self.state.now()
        if ids != self._alarm_ids:
            self._alarm_ids = ids
            for w in self.alarm_list.winfo_children():
                w.destroy()
            self._alarm_labels.clear()
            if not shown:
                tk.Label(self.alarm_list, text="No alarms set", bg=W.CARD, fg=W.MUTED, font=W.FONT, anchor="w").grid(row=0, column=0, sticky="w")
            for i, a in enumerate(shown):
                time_label, detail, _ = alarm_row_text(a, now)
                tk.Label(self.alarm_list, text=time_label, bg=W.CARD, fg=W.TEXT, font=(W.MONO_FAMILY, 12, "bold"), anchor="w").grid(row=i, column=0, sticky="w")
                d = tk.Label(self.alarm_list, bg=W.CARD, fg=W.MUTED, font=W.FONT, anchor="w")
                d.grid(row=i, column=1, sticky="ew", padx=(8, 4))
                self._alarm_labels[a.id] = d
                b = ttk.Button(self.alarm_list, text="Cancel", style="Small.TButton",
                               command=lambda a=a: self.submit(f"cancel alarm {format_time12(a.hour, a.minute).lower()}"))
                b.configure(padding=(8, 1), state="normal" if self._enabled else "disabled")
                b.grid(row=i, column=2)
                self._buttons.append(b)
            if extra > 0:
                tk.Label(self.alarm_list, text=f"+{extra} more", bg=W.CARD, fg=W.MUTED, font=W.FONT).grid(row=len(shown), column=0, sticky="w")
        for a in shown:
            lab = self._alarm_labels.get(a.id)
            if lab is not None:
                _, detail, ring = alarm_row_text(a, now)
                text = f"{detail}  {ring}".strip() if detail else ring
                lab.configure(text=text if len(text) <= 26 else text[:25] + "~")
