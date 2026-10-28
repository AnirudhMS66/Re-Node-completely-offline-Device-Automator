"""Real on/off toggles for Wi-Fi, Bluetooth and Flashlight.

How each one works
  Wi-Fi / Bluetooth  Read the REAL state (`adb shell settings get global wifi_on|bluetooth_on`, read-only),
                     then command the OPPOSITE state with `svc wifi|bluetooth enable|disable`, then re-read
                     to verify. If the direct command did not take effect, fall back to the project's own
                     Quick Settings macro (which taps the tile) and verify again.
  Flashlight         Android exposes no reliable way to READ the torch state over adb, so this one runs the
                     project's Quick Settings macro and the GUI tracks an ASSUMED state (shown with a "?").

No Tk and no phone in here: the adb shell and the macro runner are injected, so everything is unit-tested.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

TOGGLE_PREFIX = "@toggle:"

# kind -> (display name, log prefix, macro goal run through the existing agent, can the state be read?)
KINDS: dict[str, tuple[str, str, str, bool]] = {
    "wifi": ("Wi-Fi", "[WIFI]", "turn on wifi", True),
    "bluetooth": ("Bluetooth", "[BT]", "bluetooth", True),
    "flashlight": ("Flashlight", "[TORCH]", "flashlight", False),
}

_SETTINGS_KEY = {"wifi": "wifi_on", "bluetooth": "bluetooth_on"}

Shell = Callable[[list[str]], str]          # shell(["settings", "get", ...]) -> stdout, raises on error
Macro = Callable[[str], bool]                # macro("turn on wifi") -> True/False (the existing agent)


def goal_for(kind: str) -> str:
    return f"{TOGGLE_PREFIX}{kind}"


def parse_goal(goal: str) -> str | None:
    """'@toggle:wifi' -> 'wifi'; anything else -> None."""
    if goal.startswith(TOGGLE_PREFIX):
        kind = goal[len(TOGGLE_PREFIX):].strip()
        return kind if kind in KINDS else None
    return None


_NAMES = {"wifi": "wifi", "wi fi": "wifi", "wlan": "wifi", "bluetooth": "bluetooth", "flashlight": "flashlight",
          "torch": "flashlight", "flash light": "flashlight"}
_SPOKEN = re.compile(
    r"^(?:please\s+)?(?:(?:turn|switch|set)\s+)?(?:(?P<w1>on|off)\s+)?(?:the\s+)?(?P<name>wi\s?-?\s?fi|wlan|bluetooth|flash\s?light|torch)"
    r"(?:\s+(?P<w2>on|off))?(?:\s+please)?$", re.IGNORECASE)
_VERBS = re.compile(r"^(?:please\s+)?(?P<verb>enable|disable|toggle|turn|switch)\s+(?:(?P<w>on|off)\s+)?(?:the\s+)?(?P<name>wi\s?-?\s?fi|wlan|bluetooth|flash\s?light|torch)(?:\s+(?P<w2>on|off))?$", re.IGNORECASE)


def parse_spoken(goal: str) -> tuple[str, bool | None] | None:
    """Plain commands for the three switches -> (kind, wanted state or None = flip).
    "turn on wifi" -> ("wifi", True); "bluetooth off" -> ("bluetooth", False); "flashlight" -> ("flashlight", None).
    Anything else (including longer sentences) -> None, so it keeps its normal routing."""
    text = " ".join(goal.lower().replace(",", " ").split())
    m = _VERBS.match(text)
    want = None
    if m:
        verb = m.group("verb").lower()
        word = (m.group("w") or m.group("w2") or "").lower()
        want = {"enable": True, "disable": False}.get(verb)
        if word:
            want = word == "on"
        name = m.group("name")
    else:
        m = _SPOKEN.match(text)
        if not m:
            return None
        word = (m.group("w1") or m.group("w2") or "").lower()
        want = (word == "on") if word else None
        name = m.group("name")
    key = re.sub(r"[\s-]+", " ", name.lower()).strip()
    kind = _NAMES.get(key) or _NAMES.get(key.replace(" ", ""))
    return (kind, want) if kind else None


def friendly(goal: str) -> str:
    """Name shown in the history table and status line."""
    kind = parse_goal(goal)
    return f"{KINDS[kind][0]} toggle" if kind else goal


def parse_radio_state(kind: str, value: str) -> bool | None:
    """wifi_on: 1/2 = on, 0/3 = off (same mapping as AndroidDispatcher.wifi_enabled).
    bluetooth_on: 1 = on, 0 = off. Anything else (null, empty) = unknown."""
    value = (value or "").strip()
    if kind == "wifi":
        return {"1": True, "2": True, "0": False, "3": False}.get(value)
    if kind == "bluetooth":
        return {"1": True, "0": False}.get(value)
    return None


def read_state(kind: str, shell: Shell) -> bool | None:
    key = _SETTINGS_KEY.get(kind)
    if key is None:
        return None
    try:
        return parse_radio_state(kind, shell(["settings", "get", "global", key]))
    except Exception:  # noqa: BLE001 -- no phone / adb error => unknown
        return None


def command_state(kind: str, want: bool, shell: Shell) -> None:
    """Direct switch through the `svc` service (Wi-Fi: same fail-safe the mail workflow already uses)."""
    verb = "enable" if want else "disable"
    if kind == "wifi":
        try:
            shell(["svc", "wifi", verb])
        except Exception:  # noqa: BLE001
            shell(["cmd", "wifi", "set-wifi-enabled", "enabled" if want else "disabled"])
    elif kind == "bluetooth":
        try:
            shell(["svc", "bluetooth", verb])
        except Exception:  # noqa: BLE001
            shell(["cmd", "bluetooth_manager", verb])
    else:
        raise ValueError(f"{kind} cannot be switched directly")


@dataclass
class ToggleResult:
    ok: bool
    kind: str
    was: bool | None        # state before (None = unknown / flashlight)
    now: bool | None        # state after  (None = unknown / flashlight)
    method: str             # "adb", "macro", "macro (state unknown)", "failed"
    message: str


def wait_for_state(kind: str, want: bool, shell: Shell, timeout: float = 5.0, interval: float = 0.4,
                   sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    deadline = clock() + timeout
    while True:
        if read_state(kind, shell) is want:
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


def toggle_radio(kind: str, shell: Shell, macro: Macro | None, log: Callable[[str, str], None],
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 timeout: float = 5.0, want: bool | None = None) -> ToggleResult:
    """Flip Wi-Fi or Bluetooth to the opposite of its CURRENT real state and verify it.
    With `want` given ("turn on wifi") the radio is set to that state instead (no-op if it is already there)."""
    name, tag, goal, _ = KINDS[kind]
    was = read_state(kind, shell)
    if want is not None and was is want:
        log("INFO", f"{tag} {name} is already {'ON' if was else 'OFF'}")
        return ToggleResult(True, kind, was, was, "adb", f"{name} already {'ON' if was else 'OFF'}")

    if was is None:
        # Can't read the state (unusual ROM / no phone). The macro still flips it, we just can't verify.
        if macro is None:
            log("ERROR", f"{tag} Cannot read the {name} state from the phone (is it connected?)")
            return ToggleResult(False, kind, None, None, "failed", f"Cannot read the {name} state; check the USB connection")
        log("WARNING", f"{tag} {name} state unreadable; flipping it with the Quick Settings macro (cannot verify)")
        ok = bool(macro(goal))
        return ToggleResult(ok, kind, None, None, "macro (state unknown)", f"{name} toggled via Quick Settings (unverified)" if ok else f"{name} macro failed")

    want = not was
    label = "ON" if want else "OFF"
    log("INFO", f"{tag} {name} is {'ON' if was else 'OFF'} -> turning {label}")
    try:
        command_state(kind, want, shell)
        reached = wait_for_state(kind, want, shell, timeout, sleep=sleep, clock=clock)
    except Exception as exc:  # noqa: BLE001
        log("WARNING", f"{tag} Direct switch failed ({exc}); trying the Quick Settings macro")
        reached = False
    if reached:
        log("INFO", f"{tag} {name} is {label}")
        return ToggleResult(True, kind, was, want, "adb", f"{name} turned {label}")

    if macro is not None:
        log("WARNING", f"{tag} Direct switch did not change {name}; trying the Quick Settings macro")
        macro_ok = bool(macro(goal))
        reached = wait_for_state(kind, want, shell, timeout, sleep=sleep, clock=clock)
        if reached:
            log("INFO", f"{tag} {name} is {label} (via Quick Settings)")
            return ToggleResult(True, kind, was, want, "macro", f"{name} turned {label} (Quick Settings)")
        now = read_state(kind, shell)
        why = "macro ran but the state did not change" if macro_ok else "macro failed"
        log("ERROR", f"{tag} {name} did not switch {label}: {why}")
        return ToggleResult(False, kind, was, now, "failed", f"{name} did not switch {label} ({why})")

    log("ERROR", f"{tag} {name} did not switch {label}")
    return ToggleResult(False, kind, was, read_state(kind, shell), "failed", f"{name} did not switch {label}")


def toggle_flashlight(macro: Macro | None, assumed: bool | None, log: Callable[[str, str], None],
                      want: bool | None = None) -> ToggleResult:
    """Run the Quick Settings flashlight macro. The torch state can't be read, so `assumed` (what we
    believe it is now; None = unknown, treated as OFF) decides the label of the new state."""
    name, tag, goal, _ = KINDS["flashlight"]
    if macro is None:
        log("ERROR", f"{tag} The agent is not loaded, so the flashlight macro cannot run")
        return ToggleResult(False, "flashlight", assumed, assumed, "failed", "Agent not loaded")
    if want is not None and bool(assumed) is want and assumed is not None:
        log("INFO", f"{tag} The flashlight is already {'ON' if want else 'OFF'} (as far as we know)")
        return ToggleResult(True, "flashlight", assumed, assumed, "macro", f"Flashlight already {'ON' if want else 'OFF'}")
    now = not bool(assumed)
    log("INFO", f"{tag} Toggling the flashlight (assumed now {'ON' if now else 'OFF'}; Android does not report torch state)")
    ok = bool(macro(goal))
    if ok:
        return ToggleResult(True, "flashlight", assumed, now, "macro", f"Flashlight toggled (assumed {'ON' if now else 'OFF'})")
    return ToggleResult(False, "flashlight", assumed, assumed, "failed", "Flashlight macro failed")
