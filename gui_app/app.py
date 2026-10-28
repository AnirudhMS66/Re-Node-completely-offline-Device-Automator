"""The Re-Node control-center window.

Threading model (the one rule that keeps Tk stable):
  * Worker threads (agent runner, pollers, loader, mic) NEVER touch a widget. They put tuples on
    `self.events`; loguru lines go onto `self.bus.queue`.
  * The Tk main thread drains both queues every 100 ms in `_tick()` and updates the widgets.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import clock_state
from clock_skill import parse_clock_command
from gui_app import toggles
from gui_app.clock_panel import ClockPanel
from gui_app import widgets as W
from gui_app.log_bus import BrainTracker, LogBus, LogLine, NoteTracker, StepState, parse_step
from gui_app.state import CommandHistory, ExposureTimer, format_mmss
from gui_app.workers import (AgentRunner, DeviceInfo, OllamaStatus, Poller, RunResult, ToggleAgent, ensure_ollama,
                             make_adb_shell, stop_process)

MAX_CONSOLE_LINES = 5000
TICK_MS = 100
CLOCK_MS = 500
DEVICE_POLL_S = 3.0
WIFI_POLL_S = 2.0
OLLAMA_POLL_S = 5.0

ABOUT_TEXT = """Re-Node: An Edge-AI Driven Autonomous Mobile Agent
for Offline Digital Accessibility and E-Waste Upcycling

ARCHITECTURE (4 layers)
- Perception: reads the phone screen through the uiautomator2 XML hierarchy, with an
  EasyOCR screenshot fallback for screens that hide their UI tree.
- Brain: a small quantised LLM (gemma:2b / phi3) served locally by Ollama. It classifies
  commands, narrates a plan and, for new goals, picks the next action as strict JSON.
- Actuator: raw ADB shell commands (tap, swipe, type, key events, intents, svc wifi).
- Skills: deterministic macros and flagship workflows ("mirror", "mail") with multi-selector
  fallback chains, plus a cache of goals the LLM has solved before.

ROUTING
A command is tried as: flagship workflow -> timer / alarm command -> known skill -> learned skill ->
reactive LLM loop. The timer / alarm parser is deterministic (regexes, no LLM) and only claims a command it
fully understands.

OFFLINE / ZERO-TRUST DESIGN
- No cloud: the LLM runs on this PC; this control center makes no internet requests (the only
  HTTP call is to the local Ollama server, with proxies disabled).
- The phone stays air-gapped: the "mail" workflow switches Wi-Fi ON only for the send and OFF
  immediately after, and a safety block forces it OFF if anything fails mid-way.
- The Wi-Fi card measures every exposure window so the air-gap claim can be shown live.

ABOUT THIS WINDOW
- Commands run one at a time in a background thread. A running command cannot be stopped
  safely mid-step, so there is no Stop button.
- Device-info polling pauses while a command runs; Wi-Fi polling (read-only) keeps running
  so the exposure timer stays accurate.
"""


class ControlCenter:
    def __init__(self, root: tk.Tk, *, demo: bool, buttons: dict[str, str], primary: tuple[str, ...],
                 log_dir: Path, ollama_endpoint: str, ollama_model: str, adb_serial: str, warmup: bool, auto_start_ollama: bool = True,
                 toggle_map: dict[str, str] | None = None, hints: dict[str, str] | None = None):
        self.root = root
        self.demo = demo
        self.buttons_map = buttons
        self.toggle_map = toggle_map or {}
        self.hints = hints or {}
        self.primary = primary
        self.log_dir = log_dir
        self.ollama_endpoint = ollama_endpoint
        self.ollama_model = ollama_model
        self.adb_serial = adb_serial
        self.warmup = warmup
        self.auto_start_ollama = auto_start_ollama
        self._ollama_proc = None
        self._ollama_ready = threading.Event()

        self.bus = LogBus()
        self.events: queue.Queue = queue.Queue()
        suffix = "_demo" if demo else ""
        # Timers/alarms the agent set. Demo mode keeps them in memory only, so every rehearsal starts clean.
        self.clock_state = clock_state.ClockState(None) if demo else clock_state.get_state()
        if demo:
            clock_state.set_state(self.clock_state)
        self.cmd_history = CommandHistory(log_dir / f"gui_history{suffix}.json")
        self.exposure = ExposureTimer()
        self.brain = BrainTracker()
        self.note = NoteTracker()
        self.step = StepState()

        self.runner: AgentRunner | None = None
        self.agent_error = ""
        self.pollers: list[Poller] = []
        self._device_poller: Poller | None = None
        self._spin = 0
        self._closing = False
        self._last_result: RunResult | None = None
        self._wifi_state: bool | None = None
        self._bt_state: bool | None = None
        self._torch_state: bool | None = None
        self._agent_fn = None
        self._agent_loaded = False
        self.toggle_agent: ToggleAgent | None = None
        self._busy_toggle: str | None = None

        self.style = W.apply_theme(root)
        root.title("Re-Node Control Center" + ("  [DEMO]" if demo else ""))
        # Fit small laptop screens (1366x768): never ask for more than the screen offers.
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.minsize(min(1180, screen_w), min(640, screen_h - 90))
        root.geometry(f"{min(1400, screen_w - 10)}x{min(860, screen_h - 90)}+0+0")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._on_tk_exception

        self._build_menu()
        self._build_layout()
        self._set_buttons_enabled(False)
        self._log_gui("INFO", f"[GUI] Control center started in {'DEMO' if demo else 'LIVE'} mode")

        if demo:
            self._start_demo_backend()
        else:
            self._start_live_backend()

        root.after(TICK_MS, self._tick)
        root.after(CLOCK_MS, self._clock)

    # =========================================================================================
    # Back ends
    # =========================================================================================

    def _start_demo_backend(self) -> None:
        from gui_app.demo import DemoPhone, make_demo_agent

        phone = DemoPhone(self.ollama_model)
        agent = make_demo_agent(phone, self.bus.emit, state=self.clock_state)
        self._agent_fn, self._agent_loaded = agent, True
        self.toggle_agent = ToggleAgent(phone.shell, self.bus.emit, lambda: self._agent_fn)
        self.runner = AgentRunner(self.toggle_agent, self.events)
        self._add_poller("device", DEVICE_POLL_S, phone.device_info, pause_while_running=True)
        self._add_poller("wifi", WIFI_POLL_S, phone.wifi_state)
        self._add_poller("bt", WIFI_POLL_S, phone.bt_state)
        self._add_poller("ollama", OLLAMA_POLL_S, phone.ollama_status)
        self._agent_ready("Demo agent ready (simulated phone, no adb, no Ollama)")

    def _start_live_backend(self) -> None:
        from gui_app.workers import load_real_agent, probe_bluetooth, probe_device, probe_ollama, probe_wifi

        try:
            from utils.logger import logger
            if not self.bus.attach_loguru(logger):
                self._log_gui("WARNING", "[GUI] Could not attach to the project logger; live log will be empty")
        except Exception as exc:  # noqa: BLE001
            self._log_gui("ERROR", f"[GUI] Could not import utils.logger: {exc}")

        serial = self.adb_serial
        self._add_poller("device", DEVICE_POLL_S, lambda: probe_device(serial), pause_while_running=True)
        self._add_poller("wifi", WIFI_POLL_S, lambda: probe_wifi(serial))
        self._add_poller("bt", WIFI_POLL_S, lambda: probe_bluetooth(serial))
        self._add_poller("ollama", OLLAMA_POLL_S, lambda: probe_ollama(self.ollama_endpoint, self.ollama_model, 1.0))
        # Wi-Fi / Bluetooth toggles talk to adb directly, so they work even before (or without) the heavy agent.
        self.toggle_agent = ToggleAgent(make_adb_shell(serial), self.bus.emit, lambda: self._agent_fn)
        self.runner = AgentRunner(self.toggle_agent, self.events)
        self._set_buttons_enabled(True)

        def load() -> None:
            try:
                fn = load_real_agent()
            except BaseException as exc:  # noqa: BLE001
                self.events.put(("agent_failed", f"{type(exc).__name__}: {exc}", traceback.format_exc()))
                return
            self.events.put(("agent_loaded", fn))
            if self.warmup:
                self._ollama_ready.wait(30)   # don't warm up before the server is up
                try:
                    from brain.brain import warm_up_brain
                    if warm_up_brain():
                        self.bus.emit("INFO", "[GUI] Local model warmed up")
                except Exception as exc:  # noqa: BLE001
                    self.bus.emit("WARNING", f"[GUI] Model warm-up skipped: {exc}")

        if self.auto_start_ollama:
            self.brain_kv.set("Ollama", "starting...", W.WARN)
            threading.Thread(target=self._start_ollama, name="renode-ollama-start", daemon=True).start()
        else:
            self._ollama_ready.set()
        self._set_status("LOADING", W.MUTED, "Loading the agent (uiautomator2, OCR)...")
        threading.Thread(target=load, name="renode-loader", daemon=True).start()

    def _start_ollama(self) -> None:
        """Background thread: start `ollama serve` if the local server isn't answering yet."""
        try:
            result = ensure_ollama(self.ollama_endpoint, self.ollama_model)
            self._ollama_proc = result.process
            self.events.put(("ollama_start", result))
        except BaseException as exc:  # noqa: BLE001
            self.events.put(("ollama_start", None, f"{type(exc).__name__}: {exc}"))
        finally:
            self._ollama_ready.set()

    def _add_poller(self, name: str, interval: float, probe, pause_while_running: bool = False) -> None:
        paused = (lambda: self.runner is not None and self.runner.busy) if pause_while_running else None
        poller = Poller(name, interval, probe, self.events, paused)
        poller.start()
        self.pollers.append(poller)
        if name == "device":
            self._device_poller = poller

    def _poller_named(self, name: str):
        return next((p for p in self.pollers if p.name == name), None)

    def _agent_ready(self, message: str) -> None:
        self._agent_loaded = True
        self._set_buttons_enabled(True)
        self._set_status("IDLE", W.MUTED, message)

    # =========================================================================================
    # Layout
    # =========================================================================================

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Save log...", command=self.save_log, accelerator="Ctrl+S")
        file_menu.add_command(label="Clear log", command=self.clear_log)
        file_menu.add_separator()
        file_menu.add_command(label="Forget tracked timers and alarms...", command=self.clear_clock_records)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About / Architecture", command=self.show_about, accelerator="F1")
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menubar)
        self.root.bind("<F1>", lambda _e: self.show_about())
        self.root.bind("<Control-s>", lambda _e: self.save_log())

    def _build_layout(self) -> None:
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)
        self._build_left()
        main = ttk.Frame(self.root, padding=(14, 10, 14, 10))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)
        self._build_header(main)
        self._build_cards(main)
        self._build_progress(main)
        self._build_lower(main)

    def _build_header(self, main: ttk.Frame) -> None:
        bar = ttk.Frame(main)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        bar.columnconfigure(1, weight=1)
        titles = ttk.Frame(bar)
        titles.grid(row=0, column=0, sticky="w")
        ttk.Label(titles, text="Control Center", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Offline edge-AI agent  |  everything runs on this PC, nothing leaves it",
                  style="Muted.TLabel").pack(anchor="w")
        right = ttk.Frame(bar)
        right.grid(row=0, column=2, sticky="e")
        self.clock_label = ttk.Label(right, text="", style="Muted.TLabel")
        self.clock_label.pack(side="left", padx=(0, 12))
        self.mode_chip = W.Chip(right)
        if self.demo:
            self.mode_chip.set("DEMO  simulated phone", W.ORANGE, "#1a1000")
        else:
            self.mode_chip.set("LIVE  real phone", W.ACCENT_LO, "#ffffff")
        self.mode_chip.pack(side="left")

    def _build_left(self) -> None:
        left = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 14, 16, 12))
        left.grid(row=0, column=0, sticky="ns")
        left.configure(width=310)
        left.pack_propagate(False)

        brand = ttk.Frame(left, style="Panel.TFrame")
        brand.pack(fill="x")
        tk.Frame(brand, width=5, height=30, bg=W.ACCENT).pack(side="left", padx=(0, 10))
        names = ttk.Frame(brand, style="Panel.TFrame")
        names.pack(side="left")
        ttk.Label(names, text="RE-NODE", style="Title.TLabel").pack(anchor="w")
        ttk.Label(names, text="Edge-AI mobile agent", style="PanelMuted.TLabel").pack(anchor="w")

        ttk.Label(left, text="QUICK TOGGLES", style="Section.TLabel").pack(anchor="w", pady=(12, 4))
        self.toggle_tiles: dict[str, W.ToggleTile] = {}
        for label, kind in self.toggle_map.items():
            tile = W.ToggleTile(left, label, lambda k=kind: self.run_toggle(k))
            tile.pack(fill="x", pady=2)
            self.toggle_tiles[kind] = tile
            W.Tooltip(tile, f"Click to switch {label} on or off")
        self._refresh_tiles()

        ttk.Label(left, text="ACTIONS", style="Section.TLabel").pack(anchor="w", pady=(10, 4))
        self.command_buttons: list[W.RoundedButton] = []
        primary_row = ttk.Frame(left, style="Panel.TFrame")
        primary_row.pack(fill="x")
        primary_row.columnconfigure((0, 1), weight=1, uniform="p")
        secondary = ttk.Frame(left, style="Panel.TFrame")
        secondary.pack(fill="x", pady=(6, 0))
        secondary.columnconfigure((0, 1), weight=1, uniform="s")
        palette = [(W.ACCENT_LO, W.ACCENT, "#4a50c4"), ("#0f766e", "#14948a", "#0b5f59")]
        p_i = s_i = 0
        for label, goal in self.buttons_map.items():
            if label in self.primary:
                fill, hover, pressed = palette[p_i % 2]
                btn = W.RoundedButton(primary_row, label, lambda g=goal: self.run_goal(g), surface=W.SIDEBAR, fill=fill,
                                      hover=hover, pressed=pressed, fg="#ffffff", height=50,
                                      subtitle=self.hints.get(label, ""))
                btn.grid(row=0, column=p_i, sticky="ew", padx=(0, 4) if p_i == 0 else (4, 0))
                p_i += 1
            else:
                btn = W.RoundedButton(secondary, label, lambda g=goal: self.run_goal(g), surface=W.SIDEBAR, height=36,
                                      font=W.FONT_BOLD)
                btn.grid(row=s_i // 2, column=s_i % 2, sticky="ew", pady=2,
                         padx=(0, 4) if s_i % 2 == 0 else (4, 0))
                s_i += 1
            W.Tooltip(btn, f'Runs: "{goal}"')
            self.command_buttons.append(btn)

        ttk.Label(left, text="ANY COMMAND", style="Section.TLabel").pack(anchor="w", pady=(10, 4))
        entry_row = ttk.Frame(left, style="Panel.TFrame")
        entry_row.pack(fill="x")
        self.entry_var = tk.StringVar()
        self.entry = ttk.Entry(entry_row, textvariable=self.entry_var, font=W.FONT)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry_var.trace_add("write", lambda *_a: self.clock_panel.show_typed(self.entry_var.get()) if hasattr(self, "clock_panel") else None)
        self.entry.bind("<Return>", lambda _e: self.run_entry())
        self.entry.bind("<KP_Enter>", lambda _e: self.run_entry())
        self.entry.bind("<Up>", self._history_up)
        self.entry.bind("<Down>", self._history_down)
        self.mic_button = ttk.Button(entry_row, text="Mic", style="Small.TButton", command=self.on_mic, width=4)
        self.mic_button.pack(side="left", padx=(6, 0))
        self.run_button = ttk.Button(entry_row, text="Run", style="Accent.TButton", command=self.run_entry, width=5)
        self.run_button.pack(side="left", padx=(6, 0))
        ttk.Label(left, text="Enter runs it, Up/Down recalls history", style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 0))
        self._setup_mic()

        ttk.Label(left, text="STATUS", style="Section.TLabel").pack(anchor="w", pady=(10, 4))
        self.status_badge = tk.Label(left, text="LOADING", font=W.FONT_BIG, bg=W.BTN, fg=W.TEXT, padx=12, pady=5, anchor="w")
        self.status_badge.pack(fill="x")
        self.status_detail = ttk.Label(left, text="", style="PanelMuted.TLabel", wraplength=270, justify="left")
        self.status_detail.pack(anchor="w", fill="x", pady=(4, 0))

    def _build_cards(self, main: ttk.Frame) -> None:
        cards = ttk.Frame(main)
        cards.grid(row=1, column=0, sticky="ew")
        cards.columnconfigure((0, 1, 2), weight=1, uniform="card")

        # 1. Connection
        conn = W.Card(cards, "Phone connection", W.ACCENT)
        conn.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.conn_kv = W.KeyValue(conn.body, ["Status", "Model", "Android", "Resolution", "ADB serial"], 17)
        self.conn_note = ttk.Label(conn.body, text="", style="CardMuted.TLabel", wraplength=250, justify="left")
        self.conn_note.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.reconnect_button = ttk.Button(conn.header, text="Reconnect", style="Small.TButton", command=self.reconnect)
        self.reconnect_button.pack(side="right")
        self.conn_kv.set("Status", "checking...", W.MUTED)

        # 2. Wi-Fi / exposure
        wifi = W.Card(cards, "Wi-Fi / network exposure", W.CYAN)
        wifi.grid(row=0, column=1, sticky="nsew", padx=6)
        self.lamp = W.Lamp(wifi.body, 72)
        self.lamp.grid(row=0, column=0, rowspan=4, sticky="n", padx=(0, 10))
        inner = ttk.Frame(wifi.body, style="Card.TFrame")
        inner.configure(borderwidth=0)
        inner.grid(row=0, column=1, sticky="nw")
        self.wifi_kv = W.KeyValue(inner, ["Wi-Fi", "Current", "Last window", "Total", "Windows"], 15)
        ttk.Label(wifi.body, text="Air-gap: Wi-Fi should be OFF except during a send.",
                  style="CardMuted.TLabel", wraplength=260, justify="left").grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # 3. Brain / Ollama status
        brain = W.Card(cards, "Brain (local LLM)", W.ORANGE)
        brain.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        self.brain_kv = W.KeyValue(brain.body, ["Ollama", "Model", "Intent", "Confidence", "Source"], 21)
        self.brain_kv.set("Ollama", "checking...", W.MUTED)
        self.brain_kv.set("Model", self.ollama_model)

    def _build_progress(self, main: ttk.Frame) -> None:
        card = W.Card(main, "Progress", W.GREEN)
        card.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        card.body.columnconfigure(0, weight=1)
        self.progress = W.SlimProgress(card.body, surface=W.CARD)
        self.progress.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        # the step text sits in the card header (right of the title) so the card stays one bar tall
        self.progress_label = ttk.Label(card.header, text="Waiting for a command", style="Card.TLabel", font=W.FONT_BOLD)
        self.progress_label.pack(side="left", padx=(14, 0))

    def _build_lower(self, main: ttk.Frame) -> None:
        upper = ttk.PanedWindow(main, orient="horizontal")
        upper.grid(row=3, column=0, sticky="nsew", pady=(8, 0))

        # 5. Live log console
        log_card = W.Card(upper, "Live log", W.BLUE)
        upper.add(log_card, weight=5)
        tools = log_card.header
        ttk.Button(tools, text="Save log", style="Small.TButton", command=self.save_log).pack(side="right", padx=(6, 0))
        ttk.Button(tools, text="Clear", style="Small.TButton", command=self.clear_log).pack(side="right", padx=(6, 0))
        self.pause_scroll = tk.BooleanVar(value=False)
        ttk.Checkbutton(tools, text="Pause scroll", variable=self.pause_scroll).pack(side="right", padx=(6, 10))
        log_frame = ttk.Frame(log_card.body, style="Card.TFrame")
        log_frame.configure(borderwidth=0)
        log_frame.pack(fill="both", expand=True)
        self.console = tk.Text(log_frame, bg=W.INPUT, fg=W.TEXT, font=W.FONT_MONO, wrap="none", relief="flat",
                               borderwidth=0, padx=8, pady=6, insertbackground=W.TEXT, height=8, width=22,
                               highlightthickness=1, highlightbackground=W.BORDER)
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.console.yview)
        xscroll = ttk.Scrollbar(log_frame, orient="horizontal", command=self.console.xview)
        self.console.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set, state="disabled")
        self.console.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        for tag, colour in W.LOG_COLOURS.items():
            self.console.tag_configure(tag, foreground=colour)
        self.console.tag_configure("step", foreground="#ffffff", font=(W.MONO_FAMILY, 11, "bold"))

        # 3b + 6. Brain decision + copied note
        side = ttk.Frame(upper)
        upper.add(side, weight=3)
        side.rowconfigure(0, weight=3)
        side.rowconfigure(1, weight=2)
        side.columnconfigure(0, weight=1)
        decision = W.Card(side, "Brain decision", W.ORANGE)
        decision.grid(row=0, column=0, sticky="nsew", padx=(8, 0))
        self.brain_tag = tk.Label(decision.header, text="", font=W.FONT_BOLD, padx=8, pady=1, bg=W.CARD)
        self.brain_tag.pack(side="right")
        self.brain_text = W.readonly_text(decision.body, height=5, width=18)
        self.brain_text.pack(fill="both", expand=True)
        self.brain_text.tag_configure("head", foreground=W.BLUE, font=W.FONT_BOLD)
        self.brain_text.tag_configure("muted", foreground=W.MUTED)
        W.set_text(self.brain_text, "No brain decision yet. Run Mirror, Mail or a timer / alarm to see the local model's reasoning.")

        note = W.Card(side, "Copied note (mail)", W.GREEN)
        note.grid(row=1, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
        self.note_text = W.readonly_text(note.body, height=3, fg=W.GREEN, width=18)
        self.note_text.pack(fill="both", expand=True)
        W.set_text(self.note_text, "The note extracted by the mail workflow appears here.")

        # 8. Timers & alarms
        self.clock_panel = ClockPanel(upper, state=self.clock_state, run_command=self.run_goal)
        upper.add(self.clock_panel.card, weight=4)

    # =========================================================================================
    # Voice hook (section 5)
    # =========================================================================================

    def _setup_mic(self) -> None:
        try:
            import voice
            self._voice = voice
            available = bool(voice.is_available())
        except Exception:  # noqa: BLE001
            self._voice, available = None, False
        self._mic_available = available
        self.mic_button.configure(state="normal" if available else "disabled")
        W.Tooltip(self.mic_button, "Speak a command" if available else "Voice coming soon")

    def on_mic(self) -> None:
        if not self._mic_available or self._voice is None or (self.runner and self.runner.busy):
            return
        self.mic_button.configure(state="disabled")
        self._set_status("LISTENING", W.ACCENT, "Listening...")
        voice = self._voice

        def listen() -> None:
            try:
                self.events.put(("voice", voice.listen_once(), None))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("voice", None, f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=listen, name="renode-voice", daemon=True).start()

    # =========================================================================================
    # Running commands
    # =========================================================================================

    def run_entry(self) -> None:
        goal = self.entry_var.get().strip()
        if not goal:
            return
        intent = parse_clock_command(goal)
        if intent is not None and not intent.ok:
            self.clock_panel.show_text(intent.error, W.BAD)      # understood but impossible: nothing is sent to the phone
            self._log_gui("WARNING", f"[GUI] [CLOCK] {intent.error}")
            return
        if intent is not None:
            self.clock_panel.show_interpretation(intent)
        if self.run_goal(goal):
            self.cmd_history.add(goal)
            self.entry_var.set("")

    def run_toggle(self, kind: str) -> bool:
        """Switch Wi-Fi / Bluetooth / Flashlight to the opposite of its current state."""
        if kind == "flashlight" and not self._agent_loaded:
            self._log_gui("WARNING", "[GUI] The flashlight toggle needs the agent, which is not loaded yet")
            self.root.bell()
            return False
        tile = self.toggle_tiles.get(kind)
        started = self.run_goal(toggles.goal_for(kind))
        if started and tile is not None:
            self._busy_toggle = kind
            tile.set_busy(True, "Switching...")
        return started

    def run_goal(self, goal: str) -> bool:
        if self.runner is None:
            self.root.bell()
            self._log_gui("WARNING", f"[GUI] Agent not ready{': ' + self.agent_error if self.agent_error else ''}")
            return False
        if toggles.parse_goal(goal) is None and toggles.parse_spoken(goal) is None and not self._agent_loaded:
            self.root.bell()
            self._log_gui("WARNING", "[GUI] The agent is not loaded, so only the Wi-Fi and Bluetooth toggles work"
                          + (f" ({self.agent_error})" if self.agent_error else ""))
            return False
        if self.runner.busy or not self.runner.start(goal):
            self.root.bell()  # double-click / second command while one is running
            return False
        name = toggles.friendly(goal)
        self._set_buttons_enabled(False)
        self.step = StepState()
        self.progress.start_indeterminate(W.ACCENT)
        self.progress_label.configure(text=f"Running {name!r}...")
        self._set_status("RUNNING", W.ACCENT, f"{name}")
        self._log_gui("INFO", f"[GUI] Running command {name!r}")
        return True

    def _on_run_done(self, result: RunResult) -> None:
        name = toggles.friendly(result.goal)
        clock_intent = parse_clock_command(result.goal)
        if clock_intent is not None and clock_intent.ok:
            self.clock_panel.show_result(result.ok, f"{'Done' if result.ok else 'Failed'}: {clock_intent.summary}"
                                         + ("" if result.ok else " (see the log)"))
        if result.ok:
            self.progress.set_fraction(1.0, W.OK)
            self.progress_label.configure(text=f"Done: {name!r} in {result.duration_s:.1f} s")
            self._set_status("OK", W.OK, f"{name} - {result.duration_s:.1f} s")
            self._log_gui("INFO", f"[GUI] [DONE] {name!r} succeeded in {result.duration_s:.1f} s")
        else:
            self.progress.set_fraction(self.step.fraction if self.step.total else 1.0, W.BAD)
            where = f" at step {self.step.current}/{self.step.total}" if self.step.total else ""
            self.progress_label.configure(text=f"Failed{where}: {result.error or 'see log'}")
            self._set_status("FAILED", W.BAD, f"{name} - {result.error or 'see log'}")
            self._log_gui("ERROR", f"[GUI] [FAILED] {name!r} after {result.duration_s:.1f} s: {result.error}")
            if result.details:
                for line in result.details.rstrip().splitlines():
                    self._append_lines([LogLine(datetime.now().strftime("%H:%M:%S"), "ERROR", line, "error", True)])
        self._last_result = result
        if self._busy_toggle:
            tile = self.toggle_tiles.get(self._busy_toggle)
            self._busy_toggle = None
            if tile is not None:
                tile.set_busy(False)
        spoken = toggles.parse_spoken(result.goal)
        if ("flashlight" in (toggles.parse_goal(result.goal), spoken[0] if spoken else None)) and self.toggle_agent and self.toggle_agent.last_result:
            self._torch_state = self.toggle_agent.torch_assumed
        self._refresh_tiles()
        self._set_buttons_enabled(True)
        for poller_name in ("device", "wifi", "bt"):
            poller = self._poller_named(poller_name)
            if poller:
                poller.poll_now()

    def _set_buttons_enabled(self, enabled: bool) -> None:
        """`enabled` = the app is idle. Actions also need the agent; Wi-Fi/Bluetooth toggles only need adb."""
        idle = enabled and self.runner is not None
        action_state = "normal" if (idle and self._agent_loaded) else "disabled"
        for btn in self.command_buttons:
            btn.configure(state=action_state)
        self.run_button.configure(state=action_state)
        for kind, tile in self.toggle_tiles.items():
            tile.set_enabled(idle and (self._agent_loaded or kind != "flashlight"))
        self.reconnect_button.configure(state="normal" if enabled else "disabled")
        if hasattr(self, "clock_panel"):
            self.clock_panel.set_enabled(idle and self._agent_loaded)
        if hasattr(self, "_mic_available"):
            self.mic_button.configure(state="normal" if (idle and self._agent_loaded and self._mic_available) else "disabled")

    def _set_status(self, text: str, colour: str, detail: str = "") -> None:
        dark_text = colour in (W.OK, W.WARN, W.ORANGE, W.CYAN)
        if colour == W.MUTED:
            bg, fg = W.BTN, W.TEXT
        else:
            bg, fg = colour, "#08130d" if dark_text else "#ffffff"
        self.status_badge.configure(text=text, bg=bg, fg=fg)
        self.status_detail.configure(text=detail)

    def _refresh_tiles(self) -> None:
        """Update the three toggle tiles from the latest known real state."""
        busy = self._busy_toggle
        specs = {
            "wifi": (self._wifi_state, {True: "ON - network exposed", False: "OFF - air-gapped", None: "State unknown"}),
            "bluetooth": (self._bt_state, {True: "ON", False: "OFF", None: "State unknown"}),
            "flashlight": (self._torch_state, {True: "ON (assumed)", False: "OFF (assumed)", None: "State unknown - tap to toggle"}),
        }
        for kind, tile in self.toggle_tiles.items():
            state, texts = specs[kind]
            if kind == busy:
                continue
            tile.set_state(state, texts[state])

    # =========================================================================================
    # Connection
    # =========================================================================================

    def reconnect(self) -> None:
        if self.runner is not None and self.runner.busy:
            return
        self.conn_kv.set("Status", "reconnecting...", W.WARN)
        if self.demo:
            if self._device_poller:
                self._device_poller.poll_now()
            return
        self.reconnect_button.configure(state="disabled")

        def work() -> None:
            # Same entry point main.py uses, so a success here means the agent can connect too.
            try:
                from perception.perception import connect_device
                device = connect_device()
                info = device.info
                self.events.put(("reconnect", True, f"uiautomator2 OK: {info.get('productName', 'device')}"))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("reconnect", False, f"{type(exc).__name__}: {exc}"[:200]))

        threading.Thread(target=work, name="renode-reconnect", daemon=True).start()

    def _show_device(self, info) -> None:
        if info == "paused":
            self.conn_note.configure(text="Polling paused while a command runs")
            return
        if isinstance(info, Exception):
            info = DeviceInfo(False, error=str(info))
        if info.connected:
            self.conn_kv.set("Status", "Connected", W.OK)
            self.conn_kv.set("Model", info.model)
            self.conn_kv.set("Android", f"{info.android} (SDK {info.sdk})" if info.sdk else info.android or "-")
            self.conn_kv.set("Resolution", info.resolution or "-")
            self.conn_kv.set("ADB serial", info.serial)
            self.conn_note.configure(text="")
        else:
            self.conn_kv.set("Status", "Not connected", W.BAD)
            for key in ("Model", "Android", "Resolution", "ADB serial"):
                self.conn_kv.set(key, "-", W.MUTED)
            self.conn_note.configure(text=info.error)

    # =========================================================================================
    # Wi-Fi + Ollama
    # =========================================================================================

    def _show_wifi(self, state) -> None:
        if isinstance(state, Exception):
            state = None
        if state == "paused":
            return
        previous = self._wifi_state
        self.exposure.update(state, time.monotonic())
        self._wifi_state = state
        self.lamp.set(state)
        if state is True:
            self.wifi_kv.set("Wi-Fi", "ON (exposed)", W.WARN)
        elif state is False:
            self.wifi_kv.set("Wi-Fi", "OFF (air-gap)", W.OK)
        else:
            self.wifi_kv.set("Wi-Fi", "unknown", W.MUTED)
        if previous is not None and state is not None and state != previous:
            if state:
                self._log_gui("WARNING", "[GUI] [WIFI] Wi-Fi turned ON - exposure window started")
            else:
                self._log_gui("INFO", f"[GUI] [WIFI] Wi-Fi turned OFF - exposure window {self.exposure.last_window_s:.1f} s")
        self._refresh_exposure()
        self._refresh_tiles()

    def _show_bt(self, state) -> None:
        if isinstance(state, Exception) or state == "paused":
            state = None if isinstance(state, Exception) else self._bt_state
        self._bt_state = state
        self._refresh_tiles()

    def _refresh_exposure(self) -> None:
        labels = self.exposure.labels(time.monotonic())
        self.wifi_kv.set("Current", labels["current"], W.WARN if self.exposure.is_on else W.TEXT)
        self.wifi_kv.set("Last window", labels["last"])
        self.wifi_kv.set("Total", labels["total"])
        self.wifi_kv.set("Windows", labels["windows"])

    def _show_ollama(self, status) -> None:
        if isinstance(status, Exception):
            status = OllamaStatus(False, self.ollama_model, error=str(status))
        if not isinstance(status, OllamaStatus):
            return
        if status.reachable and status.model_present:
            self.brain_kv.set("Ollama", "online", W.OK)
            self.brain_kv.set("Model", f"{status.model} (installed)")
        elif status.reachable:
            self.brain_kv.set("Ollama", "online - model not pulled", W.WARN)
            self.brain_kv.set("Model", f"{status.model} (run: ollama pull {status.model})", W.WARN)
        else:
            self.brain_kv.set("Ollama", "offline (fallback mode)", W.BAD)
            self.brain_kv.set("Model", status.model)

    # =========================================================================================
    # Log handling
    # =========================================================================================

    def _log_gui(self, level: str, message: str) -> None:
        self.bus.emit(level, message)

    def _append_lines(self, lines: list[LogLine]) -> None:
        if not lines:
            return
        console = self.console
        console.configure(state="normal")
        for line in lines:
            console.insert("end", line.render() + "\n", line.tag)
        count = int(console.index("end-1c").split(".")[0])
        if count > MAX_CONSOLE_LINES:
            console.delete("1.0", f"{count - MAX_CONSOLE_LINES + 1}.0")
        console.configure(state="disabled")
        if not self.pause_scroll.get():
            console.see("end")

    def _consume_line(self, line: LogLine) -> None:
        if line.continuation:
            return
        msg = line.message
        step = parse_step(msg)
        if step:
            current, total, text = step
            self.step = StepState(current, total, text)
            self.progress.set_fraction(current / total if total else 0.0, W.ACCENT)
            self.progress_label.configure(text=f"Step {current}/{total}: {text}")
        if self.brain.feed(msg):
            self._refresh_brain()
        if self.note.feed(msg):
            W.set_text(self.note_text, self.note.text or "(empty note)")
        else:
            self.note.end_block()

    def _refresh_brain(self) -> None:
        s = self.brain.state
        self.brain_kv.set("Intent", s.intent or "-")
        self.brain_kv.set("Confidence", f"{s.confidence:.2f}" if s.confidence is not None else "-")
        if s.source:
            colour = W.WARN if s.is_fallback else W.BLUE
            self.brain_kv.set("Source", "deterministic fallback" if s.is_fallback else s.source, colour)
            self.brain_tag.configure(text="FALLBACK" if s.is_fallback else "LLM", bg=colour, fg="#000000")
        else:
            self.brain_kv.set("Source", "-")
            self.brain_tag.configure(text="", bg=W.CARD)
        t = self.brain_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", "Command: ", "head")
        t.insert("end", f"{s.command or '-'}\n")
        if s.intent:
            t.insert("end", "Intent: ", "head")
            extra = f"  (confidence {s.confidence:.2f}" if s.confidence is not None else "  ("
            extra += f", {s.elapsed_s:.1f} s)" if s.elapsed_s is not None else ")"
            t.insert("end", f"{s.intent}{extra}\n")
        if s.reasoning:
            t.insert("end", "Reasoning: ", "head")
            t.insert("end", f"{s.reasoning}\n")
        if s.disagreement:
            t.insert("end", f"{s.disagreement}\n", "muted")
        if s.plan:
            t.insert("end", "Plan:\n", "head")
            for i, item in enumerate(s.plan, 1):
                t.insert("end", f"  {i}. {item}\n")
        t.configure(state="disabled")

    def clear_log(self) -> None:
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def save_log(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save log", initialdir=str(self.log_dir),
            initialfile=f"gui_log_{datetime.now():%Y%m%d_%H%M%S}.txt", defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.console.get("1.0", "end-1c"), encoding="utf-8")
            self._log_gui("INFO", f"[GUI] Log saved to {path}")
        except OSError as exc:
            messagebox.showerror("Save log", f"Could not save the log:\n{exc}", parent=self.root)

    # =========================================================================================
    # Command recall + clock records
    # =========================================================================================

    def clear_clock_records(self) -> None:
        if messagebox.askyesno("Forget timers and alarms",
                               "Forget the timers and alarms this window is tracking?\n\n"
                               "This does NOT touch the phone; it only empties the lists here.", parent=self.root):
            self.clock_state.clear()
            self.clock_panel.refresh()

    def _history_up(self, _event=None):
        entry = self.cmd_history.previous()
        if entry is not None:
            self.entry_var.set(entry)
            self.entry.icursor("end")
        return "break"

    def _history_down(self, _event=None):
        self.entry_var.set(self.cmd_history.next())
        self.entry.icursor("end")
        return "break"

    # =========================================================================================
    # Main-thread loops
    # =========================================================================================

    def _tick(self) -> None:
        if self._closing:
            return
        try:
            lines = self.bus.drain(500)
            self._append_lines(lines)
            for line in lines:
                self._consume_line(line)
            self._drain_events()
        except Exception:  # noqa: BLE001 -- a UI bug must never stop the loop
            self._safe_console_error(traceback.format_exc())
        self.root.after(TICK_MS, self._tick)

    def _drain_events(self) -> None:
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            kind = event[0]
            if kind == "run_done":
                self._on_run_done(event[1])
            elif kind == "device":
                self._show_device(event[1])
            elif kind == "wifi":
                self._show_wifi(event[1])
            elif kind == "bt":
                self._show_bt(event[1])
            elif kind == "ollama":
                self._show_ollama(event[1])
            elif kind == "ollama_start":
                result = event[1]
                if result is None:
                    self._log_gui("ERROR", f"[GUI] Ollama auto-start crashed: {event[2]}")
                else:
                    level = "INFO" if result.ready else "WARNING"
                    self._log_gui(level, f"[GUI] Ollama: {result.message}"
                                  + ("" if result.ready else " - the brain will use its deterministic fallback"))
                if self._poller_named("ollama"):
                    self._poller_named("ollama").poll_now()
            elif kind == "agent_loaded":
                self._agent_fn = event[1]
                self._agent_ready("Agent loaded. Ready for a command.")
                self._log_gui("INFO", "[GUI] Agent loaded (main.run_autonomous_agent)")
            elif kind == "agent_failed":
                self.agent_error = event[1]
                self._set_status("NO AGENT", W.BAD, f"Could not load main.py: {event[1]}. Wi-Fi and Bluetooth toggles still work. Rehearse with: python gui.py --demo")
                self._log_gui("ERROR", f"[GUI] [FAILED] Could not import the agent: {event[1]}")
                for line in event[2].rstrip().splitlines():
                    self._append_lines([LogLine(datetime.now().strftime("%H:%M:%S"), "ERROR", line, "error", True)])
            elif kind == "reconnect":
                ok, message = event[1], event[2]
                self._log_gui("INFO" if ok else "ERROR", f"[GUI] Reconnect: {message}")
                if not (self.runner and self.runner.busy):
                    self.reconnect_button.configure(state="normal")
                if self._device_poller:
                    self._device_poller.poll_now()
            elif kind == "voice":
                text, error = event[1], event[2]
                self._set_buttons_enabled(self.runner is not None and not self.runner.busy)
                if error:
                    self._set_status("IDLE", W.MUTED, f"Voice: {error}")
                elif text:
                    self.entry_var.set(text)
                    self.run_entry()
                else:
                    self._set_status("IDLE", W.MUTED, "Voice: nothing heard")

    def _clock(self) -> None:
        if self._closing:
            return
        try:
            self._refresh_exposure()
            self.clock_panel.refresh()
            self.clock_label.configure(text=datetime.now().strftime("%a %d %b   %H:%M:%S"))
            if self.runner is not None and self.runner.busy:
                self._spin = (self._spin + 1) % 4
                dots = ("   ", ".  ", ".. ", "...")[self._spin]
                elapsed = format_mmss(time.monotonic() - self.runner.started_at)
                self.status_badge.configure(text=f"RUNNING {dots} {elapsed}")
        except Exception:  # noqa: BLE001
            self._safe_console_error(traceback.format_exc())
        self.root.after(CLOCK_MS, self._clock)

    def _safe_console_error(self, text: str) -> None:
        try:
            stamp = datetime.now().strftime("%H:%M:%S")
            self._append_lines([LogLine(stamp, "ERROR", line, "error", i > 0) for i, line in enumerate(text.splitlines())])
        except Exception:  # noqa: BLE001
            pass

    def _on_tk_exception(self, exc_type, exc, tb) -> None:
        self._safe_console_error("[GUI] UI error (the app keeps running):\n" + "".join(traceback.format_exception(exc_type, exc, tb)))

    # =========================================================================================
    # Dialogs + shutdown
    # =========================================================================================

    def show_about(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("About Re-Node / Architecture")
        win.configure(bg=W.BG)
        win.transient(self.root)
        text = W.readonly_text(win, height=32, font=W.FONT)
        text.configure(width=92)
        W.set_text(text, ABOUT_TEXT)
        text.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 12))
        win.bind("<Escape>", lambda _e: win.destroy())
        win.focus_set()

    def on_close(self) -> None:
        if self.runner is not None and self.runner.busy:
            sure = messagebox.askyesno(
                "Command still running",
                f"{self.runner.current_goal!r} is still running on the phone.\n\n"
                "Closing now abandons it mid-step. If the mail workflow is between Wi-Fi ON and "
                "Wi-Fi OFF, the phone's Wi-Fi may be left ON.\n\nQuit anyway?",
                icon="warning", parent=self.root,
            )
            if not sure:
                return
        self.shutdown()

    def shutdown(self) -> None:
        self._closing = True
        for poller in self.pollers:
            poller.stop()
        self.bus.detach()
        stop_process(self._ollama_proc)   # only set when WE started it; a pre-existing Ollama is left alone
        try:
            self.root.destroy()
        except tk.TclError:
            pass
