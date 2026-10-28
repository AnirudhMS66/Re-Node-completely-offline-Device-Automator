"""A simulated Clock app for the timer / alarm tests (and the GUI --demo mode).

Builds on tests/fake_phone.py WITHOUT modifying it: ClockPhone / ClockDevice / ClockDispatcher are
subclasses. The Clock app is modelled on AOSP / Google Clock: an Alarm tab (list with switches + an add
button that opens a time picker with a keyboard-input mode) and a Timer tab (numpad + Start, then a
running countdown with a Delete button). `intents` chooses how the phone treats Android's standard Clock
intents:  "works" (honoured), "refused" (no handler -> 'unable to resolve'), "silent" (accepted but ignored).
"""

from __future__ import annotations

import re
from xml.sax.saxutils import quoteattr

from tests.fake_phone import FakeDevice, FakeDispatcher, H, N, Phone, W, _flatten

CLOCK = "com.google.android.deskclock"


def _xml2(node, idx=0):
    x1, y1, x2, y2 = node["b"]
    kids = "".join(_xml2(c, i) for i, c in enumerate(node["children"]))
    return (
        f'<node index="{idx}" text={quoteattr(node["text"])} resource-id={quoteattr(node["rid"])} class={quoteattr(node["cls"])} '
        f'package={quoteattr(node["pkg"])} content-desc={quoteattr(node["desc"])} clickable="{str(node["clickable"]).lower()}" '
        f'checked="{str(node.get("checked", False)).lower()}" enabled="true" focused="false" bounds="[{x1},{y1}][{x2},{y2}]">{kids}</node>'
    )


def _mmss(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class ClockPhone(Phone):
    def __init__(self, *args, clock_pkg=CLOCK, intents="works", next_alarm_readable=True, ampm=True, tab_names=("Alarm", "Timer"),
                 timer_ui_works=True, **kw):
        super().__init__(*args, **kw)
        self.clock_pkg = clock_pkg
        self.installed.add(clock_pkg)
        self.intents = intents
        self.next_alarm_readable = next_alarm_readable
        self.ampm = ampm
        self.tab_names = tab_names
        self.timer_ui_works = timer_ui_works
        self.alarms: list[dict] = []
        self.timers: list[dict] = []
        self.now = 1_000_000.0
        self.am_calls: list[tuple] = []
        self.picker = {"keyboard": False, "hour": "", "minute": "", "ampm": "AM"}
        self.timer_buf = ""

    # ---- clock logic ---------------------------------------------------------------------
    def add_alarm(self, h, m, label="", days=()):
        self.alarms = [a for a in self.alarms if not (a["h"] == h and a["m"] == m and a["label"] == label)]
        self.alarms.append({"h": h, "m": m, "label": label, "days": list(days), "on": True})

    def add_timer(self, seconds, label=""):
        self.timers.append({"total": int(seconds), "start": self.now, "label": label})

    def remaining(self, t):
        return max(0, int(t["total"] - (self.now - t["start"])))

    # ---- screens -------------------------------------------------------------------------
    def _tabs(self):
        P = self.clock_pkg
        a, t = self.tab_names
        return [
            N(P, "android.widget.TextView", text=a, desc=a, rid=f"{P}:id/tab_alarm", clickable=True, b=(0, 2250, 540, 2400),
              on_click=lambda: self._go("clock_alarm", P)),
            N(P, "android.widget.TextView", text=t, desc=t, rid=f"{P}:id/tab_timer", clickable=True, b=(540, 2250, 1080, 2400),
              on_click=lambda: self._go("clock_timer", P)),
        ]

    def _screen_tree(self):
        s = self.screen
        P = self.clock_pkg
        if not s.startswith("clock_"):
            return super()._screen_tree()
        kids = self._tabs()
        if s == "clock_alarm":
            for i, a in enumerate(sorted(self.alarms, key=lambda a: (a["h"], a["m"]))):
                top = 300 + i * 220
                h12 = (a["h"] % 12) or 12
                kids += [
                    N(P, "android.widget.TextView", text=f"{h12}:{a['m']:02d}" if self.ampm else f"{a['h']:02d}:{a['m']:02d}", b=(60, top, 400, top + 100)),
                    *([N(P, "android.widget.TextView", text="AM" if a["h"] < 12 else "PM", b=(410, top + 20, 520, top + 100))] if self.ampm else []),
                    dict(N(P, "android.widget.Switch", rid=f"{P}:id/onoff", clickable=True, b=(880, top + 10, 1040, top + 110),
                           on_click=lambda a=a: a.__setitem__("on", not a["on"])), checked=a["on"]),
                ]
            kids.append(N(P, "android.widget.ImageButton", desc="Add alarm", rid=f"{P}:id/fab", clickable=True, b=(880, 2000, 1040, 2160),
                          on_click=self._open_picker))
        elif s == "clock_picker":
            pk = self.picker
            if not pk["keyboard"]:
                kids.append(N(P, "android.widget.ImageButton", desc="Switch to text input mode", rid=f"{P}:id/toggle_mode", clickable=True,
                              b=(100, 1300, 220, 1420), on_click=lambda: pk.__setitem__("keyboard", True)))
            else:
                kids += [
                    N(P, "android.widget.EditText", text=pk["hour"], rid=f"{P}:id/input_hour", clickable=True, b=(200, 500, 440, 640),
                      on_click=lambda: setattr(self, "focus", "p_hour")),
                    N(P, "android.widget.EditText", text=pk["minute"], rid=f"{P}:id/input_minute", clickable=True, b=(520, 500, 760, 640),
                      on_click=lambda: setattr(self, "focus", "p_minute")),
                ]
                if self.ampm:
                    kids += [
                        N(P, "android.widget.Button", text="AM", clickable=True, b=(800, 480, 960, 560), on_click=lambda: pk.__setitem__("ampm", "AM")),
                        N(P, "android.widget.Button", text="PM", clickable=True, b=(800, 580, 960, 660), on_click=lambda: pk.__setitem__("ampm", "PM")),
                    ]
            kids += [
                N(P, "android.widget.Button", text="Cancel", clickable=True, b=(500, 1500, 700, 1600), on_click=lambda: self._go("clock_alarm", P)),
                N(P, "android.widget.Button", text="OK", clickable=True, b=(740, 1500, 940, 1600), on_click=self._picker_ok),
            ]
        elif s == "clock_timer":
            running = [t for t in self.timers if self.remaining(t) > 0]
            if running:
                for i, t in enumerate(running):
                    top = 300 + i * 260
                    kids += [
                        N(P, "android.widget.TextView", text=_mmss(self.remaining(t)), rid=f"{P}:id/timer_text", b=(60, top, 600, top + 140)),
                        N(P, "android.widget.ImageButton", desc="Delete", rid=f"{P}:id/timer_delete", clickable=True, b=(800, top, 960, top + 140),
                          on_click=lambda t=t: self.timers.remove(t)),
                    ]
            else:
                digits = self.timer_buf.rjust(6, "0")
                kids.append(N(P, "android.widget.TextView", text=f"{digits[0:2]}h {digits[2:4]}m {digits[4:6]}s", rid=f"{P}:id/timer_setup_time", b=(60, 300, 900, 440)))
                for i, d in enumerate("123456789"):
                    kids.append(N(P, "android.widget.Button", text=d, clickable=True, b=(60 + (i % 3) * 330, 600 + (i // 3) * 180, 360 + (i % 3) * 330, 760 + (i // 3) * 180),
                                  on_click=lambda d=d: self._digit(d)))
                kids.append(N(P, "android.widget.Button", text="0", clickable=True, b=(390, 1140, 690, 1300), on_click=lambda: self._digit("0")))
                if self.timer_ui_works:
                    kids.append(N(P, "android.widget.ImageButton", desc="Start", rid=f"{P}:id/fab", clickable=True, b=(880, 2000, 1040, 2160), on_click=self._timer_start))
        return [N(P, "android.widget.FrameLayout", b=(0, 0, W, H), children=kids)]

    # ---- transitions ---------------------------------------------------------------------
    def _open_picker(self):
        self.picker = {"keyboard": False, "hour": "", "minute": "", "ampm": "AM"}
        self._go("clock_picker", self.clock_pkg)

    def _picker_ok(self):
        pk = self.picker
        try:
            h, m = int(pk["hour"]), int(pk["minute"])
        except ValueError:
            return
        if self.ampm:
            h = h % 12 + (12 if pk["ampm"] == "PM" else 0)
        self.add_alarm(h, m)
        self._go("clock_alarm", self.clock_pkg)

    def _digit(self, d):
        if len(self.timer_buf) < 6:
            self.timer_buf += d

    def _timer_start(self):
        b = self.timer_buf.rjust(6, "0")
        total = int(b[0:2]) * 3600 + int(b[2:4]) * 60 + int(b[4:6])
        if total > 0:
            self.add_timer(total)
            self.timer_buf = ""

    def set_field(self, text, append=False):
        key = {"p_hour": "hour", "p_minute": "minute"}.get(self.focus)
        if self.screen == "clock_picker" and key:
            self.picker[key] = (self.picker[key] + text) if append else text
            return True
        return super().set_field(text, append)

    def next_alarm_text(self):
        on = sorted((a for a in self.alarms if a["on"]), key=lambda a: (a["h"], a["m"]))
        if not on:
            return "null"
        a = on[0]
        return f"Wed {(a['h'] % 12) or 12}:{a['m']:02d} {'AM' if a['h'] < 12 else 'PM'}" if self.ampm else f"Wed {a['h']:02d}:{a['m']:02d}"


class ClockDevice(FakeDevice):
    def dump_hierarchy(self):
        return "<hierarchy>" + "".join(_xml2(r) for r in self.phone.tree()) + "</hierarchy>"

    def app_start(self, package, activity=None, wait=False, stop=False, use_monkey=False):
        p = self.phone
        if package == p.clock_pkg:
            p.log.append(f"start {package}")
            p.qs_open = False
            p._go("clock_alarm", package)
            return
        super().app_start(package, activity, wait, stop, use_monkey)


class ClockDispatcher(FakeDispatcher):
    def shell(self, *args):
        p = self.phone
        if args[:3] == ("settings", "get", "system") and args[3] == "next_alarm_formatted":
            return (p.next_alarm_text() if p.next_alarm_readable else "") + "\n"
        if args[:2] == ("am", "start") and "-a" in args:
            return self._am(args)
        return super().shell(*args)

    def _am(self, args):
        p = self.phone
        p.am_calls.append(tuple(args))
        action = args[args.index("-a") + 1]
        if not action.startswith("android.intent.action.") or action.rsplit(".", 1)[1] not in (
                "SET_TIMER", "SET_ALARM", "SHOW_TIMERS", "SHOW_ALARMS", "DISMISS_ALARM", "DISMISS_TIMER"):
            return super().shell(*args)
        if p.intents == "refused":
            return f"Starting: Intent {{ act={action} }}\nError: Activity not started, unable to resolve Intent {{ act={action} flg=0x10000000 }}\n"
        ok = f"Starting: Intent {{ act={action} flg=0x10000000 }}\n"
        if p.intents == "silent":
            return ok
        extras = {}
        rest = list(args)
        for i, a in enumerate(rest):
            if a in ("--ei", "--ez", "--es", "--eial") and i + 2 < len(rest):
                extras[rest[i + 1].rsplit(".", 1)[-1]] = rest[i + 2]
        label = extras.get("MESSAGE", "").strip("'")
        name = action.rsplit(".", 1)[1]
        if name == "SET_TIMER":
            p.add_timer(int(extras["LENGTH"]), label)
        elif name == "SET_ALARM":
            days = [int(d) for d in extras.get("DAYS", "").split(",") if d]
            p.add_alarm(int(extras["HOUR"]), int(extras["MINUTES"]), label, days)
        elif name == "SHOW_TIMERS":
            p._go("clock_timer", p.clock_pkg)
        elif name == "SHOW_ALARMS":
            p._go("clock_alarm", p.clock_pkg)
        elif name == "DISMISS_TIMER":
            p.timers.clear()
        elif name == "DISMISS_ALARM":
            on = sorted((a for a in p.alarms if a["on"]), key=lambda a: (a["h"], a["m"]))
            if extras.get("SEARCH_MODE") == "android.time":
                h12, m = int(extras["HOUR"]), int(extras["MINUTES"])
                pm = extras.get("IS_PM") == "true"
                h24 = h12 % 12 + (12 if pm else 0)
                for a in on:
                    if (a["h"], a["m"]) == (h24, m):
                        a["on"] = False
            elif on:
                on[0]["on"] = False
        return ok


def make_clock_env(**kw):
    """-> (phone, WorkflowContext-like pieces) helper for tests: (phone, device, dispatcher)."""
    phone = ClockPhone(**kw)
    return phone, ClockDevice(phone), ClockDispatcher(phone)
