"""Theme and reusable Tk widgets: a deep-navy dark theme with indigo/cyan accents, rounded buttons, switch-style
toggle tiles and a slim progress bar. High contrast, all text >= 11 pt (projector friendly). Everything is drawn
with the standard library (ttk + Canvas): no images, no web fonts, no downloads.
Only the Tk main thread may use anything in this module."""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable

# --- palette ---------------------------------------------------------------------------------
BG = "#0a0e14"          # window
SIDEBAR = "#0e131b"     # left control panel
PANEL = SIDEBAR
CARD = "#131a25"        # cards
CARD_HI = "#192232"     # hovered / raised
INPUT = "#0a0f16"       # text areas, entries
BORDER = "#222d3d"
TEXT = "#e8eef6"
MUTED = "#8fa0b5"
ACCENT = "#7c83fd"      # indigo
ACCENT_HI = "#939aff"
ACCENT_LO = "#5b62e0"
CYAN = "#3cd7e6"
OK = "#34d399"
WARN = "#fbbf24"
BAD = "#f87171"
ORANGE = "#fb923c"
BLUE = "#7cb4ff"
GREEN = "#6ee7a0"
LAMP_OFF = "#2b3646"
BTN = "#1b2536"
BTN_HI = "#25324a"
BTN_LO = "#2f3f5c"
BTN_DIS = "#121926"

# Log console colours, keyed by gui_app.log_bus.classify() tags.
LOG_COLOURS = {
    "brain": BLUE,
    "step": "#ffffff",
    "note": GREEN,
    "wifi": CYAN,
    "warning": WARN,
    "error": BAD,
    "done": GREEN,
    "safety": ORANGE,
    "debug": "#74869b",
    "gui": "#c4a8ff",
    "info": "#cfd8e3",
}

if sys.platform.startswith("win"):
    UI_FAMILY, MONO_FAMILY = "Segoe UI", "Consolas"
else:
    UI_FAMILY, MONO_FAMILY = "DejaVu Sans", "DejaVu Sans Mono"

FONT = (UI_FAMILY, 11)
FONT_BOLD = (UI_FAMILY, 11, "bold")
FONT_SMALL = (UI_FAMILY, 11)
FONT_TITLE = (UI_FAMILY, 20, "bold")
FONT_HEADING = (UI_FAMILY, 16, "bold")
FONT_CARD_TITLE = (UI_FAMILY, 11, "bold")
FONT_BIG = (UI_FAMILY, 14, "bold")
FONT_BUTTON = (UI_FAMILY, 12, "bold")
FONT_MONO = (MONO_FAMILY, 11)


def apply_theme(root: tk.Tk) -> ttk.Style:
    root.configure(bg=BG)
    root.option_add("*Font", FONT)
    root.option_add("*TCombobox*Listbox.font", FONT)
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=BG, foreground=TEXT, fieldbackground=INPUT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, font=FONT, focuscolor=ACCENT)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=SIDEBAR)
    style.configure("Card.TFrame", background=CARD, relief="solid", borderwidth=1, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Panel.TLabel", background=SIDEBAR, foreground=TEXT)
    style.configure("PanelMuted.TLabel", background=SIDEBAR, foreground=MUTED)
    style.configure("Title.TLabel", background=SIDEBAR, foreground=TEXT, font=FONT_TITLE)
    style.configure("Heading.TLabel", background=BG, foreground=TEXT, font=FONT_HEADING)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)
    style.configure("Section.TLabel", background=SIDEBAR, foreground=MUTED, font=FONT_BOLD)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
    style.configure("CardTitle.TLabel", background=CARD, foreground=MUTED, font=FONT_CARD_TITLE)
    style.configure("CardBig.TLabel", background=CARD, foreground=TEXT, font=FONT_BIG)

    style.configure("TButton", background=BTN, foreground=TEXT, padding=(10, 8), font=FONT_BUTTON,
                    borderwidth=0, focusthickness=1, focuscolor=ACCENT)
    style.map("TButton",
              background=[("disabled", BTN_DIS), ("pressed", BTN_LO), ("active", BTN_HI)],
              foreground=[("disabled", "#566579")])
    style.configure("Accent.TButton", background=ACCENT_LO, foreground="#ffffff", padding=(10, 11))
    style.map("Accent.TButton",
              background=[("disabled", BTN_DIS), ("pressed", "#4a50c4"), ("active", ACCENT)],
              foreground=[("disabled", "#566579")])
    style.configure("Small.TButton", padding=(10, 4), font=FONT)

    style.configure("TEntry", fieldbackground=INPUT, foreground=TEXT, insertcolor=TEXT, padding=8,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
    style.configure("TCheckbutton", background=CARD, foreground=TEXT)
    style.map("TCheckbutton", background=[("active", CARD)],
              indicatorcolor=[("selected", ACCENT), ("!selected", INPUT)])

    style.configure("Treeview", background=INPUT, fieldbackground=INPUT, foreground=TEXT, rowheight=28,
                    font=FONT, bordercolor=BORDER, borderwidth=0)
    style.configure("Treeview.Heading", background=CARD, foreground=MUTED, font=FONT_BOLD, relief="flat",
                    padding=(6, 6))
    style.map("Treeview", background=[("selected", "#2a3a63")], foreground=[("selected", "#ffffff")])
    style.map("Treeview.Heading", background=[("active", CARD)])

    style.configure("TPanedwindow", background=BG)
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background="#27344a", troughcolor=INPUT, arrowcolor=MUTED,
                        bordercolor=INPUT, lightcolor="#27344a", darkcolor="#27344a", gripcount=0)
        style.map(f"{orient}.TScrollbar", background=[("active", "#3a4d6b")])
    return style


# =============================================================================================
# Drawing helpers
# =============================================================================================


def round_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, r: float, **kw) -> int:
    """Rounded rectangle as one smoothed polygon (Tk has no native rounded corners)."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def mix(c1: str, c2: str, t: float) -> str:
    """Blend two #rrggbb colours (t=0 -> c1, t=1 -> c2)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


# =============================================================================================
# Widgets
# =============================================================================================


class Tooltip:
    """Hover tooltip that also works on disabled widgets."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 400):
        self.widget, self.text, self.delay_ms = widget, text, delay_ms
        self._tip: tk.Toplevel | None = None
        self._after: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self) -> None:
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self.text, bg="#222d3d", fg=TEXT, relief="solid", borderwidth=1,
                 font=FONT, padx=10, pady=5).pack()

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class RoundedButton(tk.Canvas):
    """A rounded, hoverable button drawn on a Canvas. Supports configure(state="normal"|"disabled")."""

    def __init__(self, parent: tk.Widget, text: str, command: Callable[[], None], *, surface: str = SIDEBAR,
                 fill: str = BTN, hover: str = BTN_HI, pressed: str = BTN_LO, fg: str = TEXT, height: int = 46,
                 radius: int = 12, font=FONT_BUTTON, subtitle: str = ""):
        super().__init__(parent, height=height, bg=surface, highlightthickness=0, bd=0, takefocus=1, cursor="hand2")
        self.text, self.subtitle, self.command = text, subtitle, command
        self.fill, self.hover, self.pressed, self.fg = fill, hover, pressed, fg
        self.radius, self.font = radius, font
        self._disabled = False
        self._over = False
        self._down = False
        self._focus = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set(over=True))
        self.bind("<Leave>", lambda _e: self._set(over=False, down=False))
        self.bind("<ButtonPress-1>", lambda _e: self._set(down=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda _e: self._set(focus=True))
        self.bind("<FocusOut>", lambda _e: self._set(focus=False))
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())

    def _set(self, over=None, down=None, focus=None) -> None:
        if over is not None:
            self._over = over
        if down is not None:
            self._down = down
        if focus is not None:
            self._focus = focus
        self._draw()

    def _release(self, event) -> None:
        was_down = self._down
        self._set(down=False)
        if was_down and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self.invoke()

    def invoke(self) -> None:
        if not self._disabled:
            self.command()

    def configure(self, cnf=None, **kw):  # noqa: D401 -- mimic ttk: configure(state=...)
        if "state" in kw:
            self._disabled = kw.pop("state") == "disabled"
            self.configure_cursor()
            self._draw()
        if "text" in kw:
            self.text = kw.pop("text")
            self._draw()
        if cnf or kw:
            return super().configure(cnf, **kw)
        return None

    config = configure

    def configure_cursor(self) -> None:
        super().configure(cursor="arrow" if self._disabled else "hand2")

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        if self._disabled:
            fill, fg = BTN_DIS, "#566579"
        elif self._down:
            fill, fg = self.pressed, self.fg
        elif self._over:
            fill, fg = self.hover, self.fg
        else:
            fill, fg = self.fill, self.fg
        outline = ACCENT if self._focus and not self._disabled else fill
        round_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=fill, outline=outline, width=2 if self._focus else 1)
        if self.subtitle:
            self.create_text(w / 2, h / 2 - 8, text=self.text, fill=fg, font=self.font)
            self.create_text(w / 2, h / 2 + 11, text=self.subtitle, fill=fg if not self._disabled else "#566579",
                             font=(UI_FAMILY, 10))
        else:
            self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)


class ToggleTile(tk.Canvas):
    """A switch-style tile: title + status text on the left, an ON/OFF pill on the right. Clicking calls
    `command`. States: True (ON), False (OFF), None (unknown); `busy` shows a half-way knob."""

    HEIGHT = 66

    def __init__(self, parent: tk.Widget, title: str, command: Callable[[], None], *, surface: str = SIDEBAR):
        super().__init__(parent, height=self.HEIGHT, bg=surface, highlightthickness=0, bd=0, takefocus=1, cursor="hand2")
        self.title, self.command = title, command
        self.state: bool | None = None
        self.status = "checking..."
        self.busy = False
        self._disabled = True
        self._over = False
        self._down = False
        self._focus = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set(over=True))
        self.bind("<Leave>", lambda _e: self._set(over=False, down=False))
        self.bind("<ButtonPress-1>", lambda _e: self._set(down=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda _e: self._set(focus=True))
        self.bind("<FocusOut>", lambda _e: self._set(focus=False))
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())

    # --- public ---------------------------------------------------------------------------
    def set_state(self, state: bool | None, status: str = "") -> None:
        self.state, self.status = state, status
        self._draw()

    def set_busy(self, busy: bool, status: str | None = None) -> None:
        self.busy = busy
        if status is not None:
            self.status = status
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._disabled = not enabled
        super().configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def invoke(self) -> None:
        if not self._disabled and not self.busy:
            self.command()

    # --- internals -----------------------------------------------------------------------
    def _set(self, over=None, down=None, focus=None) -> None:
        if over is not None:
            self._over = over
        if down is not None:
            self._down = down
        if focus is not None:
            self._focus = focus
        self._draw()

    def _release(self, event) -> None:
        was_down = self._down
        self._set(down=False)
        if was_down and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self.invoke()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40:
            return
        dim = self._disabled
        body = CARD_HI if (self._over and not dim) else CARD
        edge = ACCENT if self._focus and not dim else (mix(BORDER, OK, 0.45) if self.state and not dim else BORDER)
        round_rect(self, 1, 1, w - 1, h - 1, 14, fill=body, outline=edge, width=1)

        title_fill = TEXT if not dim else "#6d7d92"
        self.create_text(16, h / 2 - 11, text=self.title, anchor="w", fill=title_fill, font=FONT_BOLD)
        status_colour = MUTED
        if self.state is True and not dim:
            status_colour = OK
        elif self.busy:
            status_colour = WARN
        self.create_text(16, h / 2 + 11, text=self.status, anchor="w", fill=status_colour if not dim else "#566579",
                         font=(UI_FAMILY, 10))

        # the switch
        pw, ph = 56, 30
        x2, y1 = w - 16, (h - ph) / 2
        x1 = x2 - pw
        if self.state is True:
            pill = OK if not dim else mix(OK, BG, 0.65)
            knob_x = x2 - ph / 2
        elif self.state is False:
            pill = "#334157" if not dim else "#232d3d"
            knob_x = x1 + ph / 2
        else:
            pill = "#2a3446"
            knob_x = (x1 + x2) / 2
        if self.busy:
            pill, knob_x = WARN if not dim else pill, (x1 + x2) / 2
        round_rect(self, x1, y1, x2, y1 + ph, ph / 2, fill=pill, outline=pill)
        knob_fill = "#ffffff" if self.state is True or self.busy else "#b8c4d4"
        self.create_oval(knob_x - 11, y1 + 4, knob_x + 11, y1 + ph - 4, fill=knob_fill if not dim else "#6d7d92",
                         outline="")
        if self.state is None and not self.busy:
            self.create_text(knob_x, y1 + ph / 2, text="?", fill="#334157", font=(UI_FAMILY, 11, "bold"))


class SlimProgress(tk.Canvas):
    """Thin rounded progress bar with a determinate fill and an indeterminate sliding segment."""

    def __init__(self, parent: tk.Widget, surface: str = CARD, height: int = 12):
        super().__init__(parent, height=height, bg=surface, highlightthickness=0, bd=0)
        self.fraction = 0.0
        self.tone = ACCENT
        self._indeterminate = False
        self._phase = 0.0
        self._job: str | None = None
        self.bind("<Configure>", lambda _e: self._draw())

    def set_fraction(self, fraction: float, tone: str | None = None) -> None:
        self._stop()
        self.fraction = max(0.0, min(1.0, fraction))
        if tone:
            self.tone = tone
        self._draw()

    def start_indeterminate(self, tone: str = ACCENT) -> None:
        self.tone = tone
        if self._indeterminate:
            return
        self._indeterminate = True
        self._tick()

    def _stop(self) -> None:
        self._indeterminate = False
        if self._job:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _tick(self) -> None:
        if not self._indeterminate or not self.winfo_exists():
            return
        self._phase = (self._phase + 0.025) % 1.0
        self._draw()
        self._job = self.after(30, self._tick)

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        round_rect(self, 0, 0, w, h, h / 2, fill=INPUT, outline=BORDER)
        if self._indeterminate:
            seg = w * 0.28
            x1 = -seg + (w + seg) * self._phase
            lo, hi = max(2.0, x1), min(w - 2.0, x1 + seg)
            if hi - lo > h:
                round_rect(self, lo, 2, hi, h - 2, (h - 4) / 2, fill=self.tone, outline=self.tone)
        elif self.fraction > 0:
            x2 = max(h, (w - 4) * self.fraction)
            round_rect(self, 2, 2, 2 + x2, h - 2, (h - 4) / 2, fill=self.tone, outline=self.tone)


class Card(ttk.Frame):
    """A bordered panel with an accent-barred title row. Put content in `.body`; extra header widgets in `.header`."""

    def __init__(self, parent: tk.Widget, title: str, accent: str = ACCENT, **kwargs):
        super().__init__(parent, style="Card.TFrame", padding=(12, 10, 12, 12), **kwargs)
        self.header = ttk.Frame(self, style="Card.TFrame", padding=0)
        self.header.configure(borderwidth=0)
        self.header.pack(fill="x")
        tk.Frame(self.header, width=4, height=16, bg=accent).pack(side="left", padx=(0, 8))
        ttk.Label(self.header, text=title.upper(), style="CardTitle.TLabel").pack(side="left")
        self.body = ttk.Frame(self, style="Card.TFrame", padding=(0, 8, 0, 0))
        self.body.configure(borderwidth=0)
        self.body.pack(fill="both", expand=True)


class KeyValue:
    """A two-column 'Label: value' grid inside a card, values updatable by key."""

    def __init__(self, parent: tk.Widget, keys: list[str], value_width: int = 22):
        self.values: dict[str, ttk.Label] = {}
        for row, key in enumerate(keys):
            ttk.Label(parent, text=key, style="CardMuted.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=2)
            value = ttk.Label(parent, text="-", style="Card.TLabel", width=value_width, anchor="w")
            value.grid(row=row, column=1, sticky="w", pady=2)
            self.values[key] = value
        parent.columnconfigure(1, weight=1)

    def set(self, key: str, text: str, colour: str | None = None) -> None:
        self.values[key].configure(text=text, foreground=colour or TEXT)


class Lamp(tk.Canvas):
    """Big round ON/OFF indicator with a soft halo."""

    def __init__(self, parent: tk.Widget, size: int = 88):
        super().__init__(parent, width=size, height=size, bg=CARD, highlightthickness=0)
        self._size = size
        self._halo = self.create_oval(2, 2, size - 2, size - 2, fill=CARD, outline="")
        self._circle = self.create_oval(10, 10, size - 10, size - 10, fill=LAMP_OFF, outline=BORDER, width=2)
        self._text = self.create_text(size / 2, size / 2, text="?", fill=TEXT, font=FONT_BIG)

    def set(self, state: bool | None) -> None:
        if state is True:
            self.itemconfigure(self._halo, fill=mix(CARD, WARN, 0.18))
            self.itemconfigure(self._circle, fill=WARN, outline="#ffe08a")
            self.itemconfigure(self._text, text="ON", fill="#241a00")
        elif state is False:
            self.itemconfigure(self._halo, fill=mix(CARD, OK, 0.14))
            self.itemconfigure(self._circle, fill=mix(CARD, OK, 0.30), outline=mix(CARD, OK, 0.7))
            self.itemconfigure(self._text, text="OFF", fill=OK)
        else:
            self.itemconfigure(self._halo, fill=CARD)
            self.itemconfigure(self._circle, fill="#222a36", outline=BORDER)
            self.itemconfigure(self._text, text="?", fill=MUTED)


class Chip(tk.Label):
    """Small rounded-looking status label."""

    def __init__(self, parent: tk.Widget, text: str = "", bg: str = BTN, fg: str = TEXT):
        super().__init__(parent, text=text, bg=bg, fg=fg, font=FONT_BOLD, padx=10, pady=3)

    def set(self, text: str, bg: str, fg: str = TEXT) -> None:
        self.configure(text=text, bg=bg, fg=fg)


def readonly_text(parent: tk.Widget, height: int, fg: str = TEXT, font=FONT, wrap: str = "word", width: int = 80) -> tk.Text:
    widget = tk.Text(parent, height=height, width=width, bg=INPUT, fg=fg, insertbackground=TEXT, relief="flat",
                     font=font, wrap=wrap, padx=10, pady=8, borderwidth=0, highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=BORDER, spacing1=1, spacing3=1)
    widget.configure(state="disabled")
    return widget


def set_text(widget: tk.Text, content: str) -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", content)
    widget.configure(state="disabled")
