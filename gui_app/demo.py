"""`python gui.py --demo`: a simulated phone + agent for rehearsing the UI with no phone, no adb and
no Ollama. It prints the SAME log prefixes the real workflows print ([BRAIN], [STEP n/N], [NOTE],
[WIFI], [DONE], [FAILED], [SAFETY]) so every dashboard panel lights up exactly as in a live run.

Nothing here touches the real agent, adb or the network. Special demo goals for rehearsal:
  "fail"  -> the run returns False      "crash" -> the agent raises an exception
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import clock_state as clock_state_module
from clock_skill import (CANCEL_ALARM, PAUSE_TIMER, SET_ALARM, SET_TIMER, SHOW_ALARMS, SHOW_TIMERS, STOP_TIMER,
                         ClockIntent, format_days, format_duration, format_time12, parse_clock_command)
from gui_app.workers import DeviceInfo, OllamaStatus


class DemoPhone:
    def __init__(self, model: str = "gemma:2b"):
        self.lock = threading.Lock()
        self.wifi = False
        self.bluetooth = False
        self.torch = False
        self.airplane = False
        self.model = model

    # probes used by the pollers ---------------------------------------------------------------

    def device_info(self) -> DeviceInfo:
        return DeviceInfo(True, serial="DEMO-0001", model="Simulated realme RMX3381", android="13", sdk="33",
                          resolution="1080x2400")

    def wifi_state(self) -> bool:
        with self.lock:
            return self.wifi

    def bt_state(self) -> bool:
        with self.lock:
            return self.bluetooth

    def shell(self, args: list[str]) -> str:
        """Simulated `adb shell` for the real toggle logic: settings get + svc wifi|bluetooth."""
        time.sleep(0.15)
        with self.lock:
            if args[:3] == ["settings", "get", "global"]:
                state = {"wifi_on": self.wifi, "bluetooth_on": self.bluetooth}[args[3]]
                return "1\n" if state else "0\n"
            if args[:2] in (["svc", "wifi"], ["svc", "bluetooth"]):
                time.sleep(0.5)
                setattr(self, "wifi" if args[1] == "wifi" else "bluetooth", args[2] == "enable")
                return ""
        raise RuntimeError(f"demo shell: unsupported {args}")

    def ollama_status(self) -> OllamaStatus:
        return OllamaStatus(True, self.model, True, [f"{self.model}"])


def make_demo_agent(phone: DemoPhone, emit: Callable[[str, str], None], speed: float = 1.0,
                    state: "clock_state_module.ClockState | None" = None) -> Callable[[str], bool]:
    """Return a fake run_autonomous_agent(goal) -> bool that logs through `emit(level, message)`.
    Timer / alarm commands go through the REAL parser (clock_skill) and write to the shared ClockState;
    only the phone side (the Clock app) is simulated."""

    def pause(seconds: float) -> None:
        time.sleep(seconds * speed)

    def brain(command: str, intent: str, plan: list[str]) -> None:
        emit("INFO", f"[BRAIN] Input received: {command!r}")
        emit("INFO", f"[BRAIN] Querying local model {phone.model!r} at http://localhost:11434 (offline, no cloud)...")
        pause(1.2)
        emit("INFO", f"[BRAIN] Intent detected: {intent} (confidence 0.93, source=ollama:{phone.model}, 1.2s)")
        emit("INFO", f"[BRAIN] Reasoning: The user said {command!r}, which maps to the {intent} workflow.")
        emit("INFO", "[BRAIN] Execution plan:")
        for i, step in enumerate(plan, 1):
            emit("INFO", f"[BRAIN]   {i}. {step}")

    def step(n: int, total: int, text: str, wait: float = 0.9) -> None:
        emit("INFO", f"[STEP {n}/{total}] {text}")
        pause(wait)

    def set_wifi(on: bool) -> None:
        with phone.lock:
            phone.wifi = on

    def mirror(goal: str) -> bool:
        brain(goal, "MIRROR", ["Launch the camera", "Verify the active lens", "Flip to the front lens if needed"])
        step(1, 3, "Local brain reasoning", 0.3)
        step(2, 3, "Opening the camera app (front-lens intent hint)", 1.5)
        step(3, 3, "Verifying lens via UI hierarchy / camera service; flipping to the selfie camera if needed")
        emit("INFO", "[PERCEPTION] Flip control found: 'Switch to front camera' (com.oplus.camera:id/switch_btn)")
        emit("INFO", "[PERCEPTION] Active lens: rear (camera service reports active camera ID 0)")
        emit("INFO", "[ACT] Tapping flip control 'Switch to front camera' (attempt 1)")
        pause(1.0)
        emit("INFO", "[DONE] Switched to the front lens (camera service reports active camera ID 1) - mirror ready")
        return True

    def mail(goal: str) -> bool:
        total = 11
        brain(goal, "MAIL", ["Open Notes", "Copy the first note", "Open Gmail and compose",
                             "Wi-Fi ON only for the send", "Send", "Wi-Fi OFF", "Home"])
        try:
            step(1, total, "Local brain reasoning", 0.3)
            step(2, total, "Opening the Notes app")
            emit("INFO", "[PERCEPTION] Notes app: com.oplus.note")
            step(3, total, "Opening the first note and extracting its full text from the UI hierarchy")
            emit("INFO", "[NOTE] Copied note content (stored in Python variable + device clipboard):")
            for line in ("Project review - 29 Oct", "Bring the demo phone and charger", "Send slides to guide@college.edu"):
                emit("INFO", f"[NOTE]   | {line}")
            step(4, total, "Pressing Home", 0.5)
            step(5, total, "Opening Gmail")
            step(6, total, "Tapping Compose")
            step(7, total, "Filling recipient, subject and body", 1.2)
            emit("INFO", "[ACT] Recipient: guide@college.edu (parsed from note)")
            step(8, total, "Opening the Control Panel and turning Wi-Fi ON", 0.6)
            emit("INFO", "[WIFI] Tapping Wi-Fi tile to turn ON")
            set_wifi(True)
            emit("INFO", "[WIFI] Wi-Fi is ON")
            emit("INFO", "[WIFI] Waiting 5s for the connection to establish")
            pause(5.0)
            step(9, total, "Sending the mail", 0.5)
            emit("INFO", "[ACT] Send tapped; waiting 5s for the message to leave the Outbox")
            pause(4.0)
            step(10, total, "Opening the Control Panel and turning Wi-Fi OFF (restoring the air-gap)", 0.6)
            emit("INFO", "[WIFI] Tapping Wi-Fi tile to turn OFF")
            set_wifi(False)
            emit("INFO", "[WIFI] Wi-Fi is OFF")
            step(11, total, "Returning Home", 0.5)
            emit("INFO", "[DONE] mail workflow complete - note sent, Wi-Fi back OFF")
            return True
        finally:
            if phone.wifi_state():
                emit("WARNING", "[SAFETY] Workflow ended with Wi-Fi possibly ON - forcing it OFF to keep the air-gap")
                set_wifi(False)

    def toggle(goal: str, attr: str, label: str) -> bool:
        emit("INFO", f"Matched known skill: 'toggle_{attr}' - running fixed macro")
        pause(1.0)
        with phone.lock:
            setattr(phone, attr, not getattr(phone, attr))
            state = getattr(phone, attr)
        emit("INFO", f"[ACT] Tapped the {label} tile; {label} is now {'ON' if state else 'OFF'}")
        pause(0.8)
        return True

    def clock(goal: str, intent: ClockIntent) -> bool:
        st = state or clock_state_module.get_state()
        tag = "[CLOCK]"
        if not intent.ok:
            emit("ERROR", f"{tag} {intent.error}")
            return False
        emit("INFO", f"{tag} Understood: {intent.summary}")
        if intent.note:
            emit("INFO", f"{tag} Interpretation: {intent.note}")
        brain(goal, intent.family, ["Parse the command with the deterministic clock parser",
                                    "Send it to the Clock app through Android's Clock intents",
                                    "Verify on the phone; fall back to Clock-app UI automation if needed"])
        step(1, 5, "Local model reasoning about the command (offline)", 0.3)
        emit("INFO", f"{tag} Clock app: com.google.android.deskclock (simulated)")
        if intent.kind == SET_TIMER:
            step(2, 5, f"Starting a {format_duration(intent.seconds)} timer via the Android Clock intent")
            step(3, 5, "Verifying the timer is running on the phone")
            emit("INFO", f"{tag} Confirmed in the Clock app: a {format_duration(intent.seconds)} countdown is running")
            step(5, 5, "Recording the timer", 0.3)
            st.add_timer(intent.seconds, intent.label)
        elif intent.kind == SET_ALARM:
            days = f", {format_days(intent.days)}" if intent.days else ""
            step(2, 5, f"Setting alarm {format_time12(intent.hour, intent.minute)}{days} via the Android Clock intent")
            step(3, 5, "Verifying the alarm exists on the phone")
            emit("INFO", f"{tag} Confirmed by the phone's next-alarm setting: {format_time12(intent.hour, intent.minute)}")
            step(5, 5, "Recording the alarm", 0.3)
            st.add_alarm(intent.hour, intent.minute, intent.label, intent.days, intent.when)
        elif intent.kind == STOP_TIMER:
            running = st.running_timers()
            hit = [t for t in running if (intent.seconds is None or t.total_s == intent.seconds)
                   and (not intent.label or t.label.lower() == intent.label.lower())]
            if (intent.seconds is not None or intent.label) and not hit:
                emit("ERROR", f"{tag} No running timer matches {intent.summary!r}")
                return False
            step(2, 4, "Stopping the timer through the Clock app")
            step(3, 4, "Verifying")
            st.stop_timer(timer_id=hit[0].id) if (intent.seconds is not None or intent.label) else st.stop_timer()
            step(4, 4, "Updating the record", 0.3)
        elif intent.kind == CANCEL_ALARM:
            ambiguous = intent.hour is not None and "no am/pm" in intent.note
            if intent.cancel_all:
                ids = [a.id for a in st.active_alarms()]
            elif intent.hour is None and not intent.label:
                ids = [a.id for a in st.active_alarms()[:1]]
            else:
                ids = [a.id for a in st.match_alarms(intent.hour, intent.minute, intent.label, ambiguous)]
            step(2, 4, "Switching the alarm off in the Clock app")
            step(3, 4, "Verifying the alarm is off")
            st.cancel_alarms(ids)
            step(4, 4, "Updating the record", 0.3)
        elif intent.kind in (SHOW_TIMERS, SHOW_ALARMS):
            step(2, 3, "Opening the Clock app screen")
            step(3, 3, "Done", 0.3)
        elif intent.kind == PAUSE_TIMER:
            emit("ERROR", f"{tag} Pausing a timer is not supported by Android's Clock intents; use 'stop timer' instead")
            return False
        emit("INFO", f"[DONE] {intent.summary}")
        return True

    def reactive(goal: str) -> bool:
        emit("INFO", "No known/learned skill matched - falling back to reactive LLM loop")
        for n in range(1, 4):
            emit("INFO", f"--- Step {n}/10 ---")
            pause(1.0)
            action = "complete" if n == 3 else "click"
            emit("INFO", f"Action: {action} | Reasoning: (demo) working towards {goal!r}")
        emit("INFO", "Goal reported complete by brain.")
        return True

    def agent(goal: str) -> bool:
        g = goal.lower()
        emit("INFO", "Connected: Simulated realme RMX3381 (1080x2400, Android SDK 33) - currently in com.android.launcher")
        if g == "crash":
            pause(0.5)
            raise RuntimeError("simulated crash inside the agent (demo)")
        if g == "fail":
            pause(0.5)
            emit("ERROR", "[FAILED] demo: simulated failure")
            return False
        intent = parse_clock_command(goal)
        if intent is not None:
            return clock(goal, intent)
        if "mirror" in g:
            return mirror(goal)
        if "mail" in g:
            return mail(goal)
        if "wifi" in g or "wi-fi" in g:
            with phone.lock:
                phone.wifi = not phone.wifi
                state = phone.wifi
            emit("INFO", "Matched known skill: 'toggle_wifi' - running fixed macro")
            pause(1.0)
            emit("INFO", f"[WIFI] (demo) Wi-Fi toggled {'ON' if state else 'OFF'}")
            return True
        if "bluetooth" in g:
            return toggle(goal, "bluetooth", "Bluetooth")
        if "flashlight" in g:
            return toggle(goal, "torch", "Flashlight")
        if "airplane" in g:
            return toggle(goal, "airplane", "Airplane mode")
        if g in ("quick settings", "go home screen", "go back"):
            emit("INFO", f"Matched known skill for {goal!r} - running fixed macro")
            pause(0.8)
            return True
        return reactive(goal)

    return agent
