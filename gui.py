"""Re-Node desktop control center.

    python gui.py           # live: drives the real phone through main.run_autonomous_agent()
    python gui.py --demo    # rehearsal: simulated phone + agent, no adb / Ollama needed

Standard library only (tkinter). Terminal mode (`python main.py`) is untouched and still works.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------------------------
# Edit the buttons here (order = order on screen).
#
# TOGGLE_BUTTONS: real on/off switches. Each press reads the phone's CURRENT state and switches to the
#   opposite (Wi-Fi and Bluetooth: read via adb settings, set via adb svc, verified; falls back to the
#   Quick Settings macro). Flashlight cannot be read over adb, so its state is tracked/assumed.
#   Kinds available: "wifi", "bluetooth", "flashlight".
# COMMAND_BUTTONS: label -> goal string sent to run_autonomous_agent().
# ---------------------------------------------------------------------------------------------
TOGGLE_BUTTONS: dict[str, str] = {
    "Wi-Fi": "wifi",
    "Bluetooth": "bluetooth",
    "Flashlight": "flashlight",
}

COMMAND_BUTTONS: dict[str, str] = {
    "Mirror": "mirror",
    "Mail": "mail",
    "Airplane mode": "airplane",
    "Quick Settings": "quick settings",
    "Home": "go home screen",
    "Back": "go back",
}
PRIMARY_BUTTONS = ("Mirror", "Mail")   # the two big buttons
BUTTON_HINTS = {"Mirror": "front camera", "Mail": "send first note"}

# Start `ollama serve` automatically if it is not already running (live mode only). It is stopped again
# when the window closes, but only if THIS program started it.
AUTO_START_OLLAMA = True

ROOT = Path(__file__).resolve().parent


def _enable_windows_dpi_awareness() -> None:
    """Crisp text on high-DPI laptops / projectors. Harmless no-op elsewhere."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _load_settings() -> dict:
    """Read the values the GUI needs from config.py (which also loads .env)."""
    try:
        import config
        return {
            "log_dir": Path(config.LOG_DIR),
            "ollama_endpoint": config.OLLAMA_ENDPOINT,
            "ollama_model": config.OLLAMA_MODEL,
            "adb_serial": config.ADB_SERIAL,
            "warmup": bool(getattr(config, "BRAIN_WARMUP", True)),
        }
    except Exception as exc:  # noqa: BLE001 -- e.g. python-dotenv missing; demo mode still works
        print(f"[GUI] config.py could not be loaded ({exc}); using defaults", file=sys.stderr)
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        return {"log_dir": log_dir, "ollama_endpoint": "http://localhost:11434", "ollama_model": "gemma:2b",
                "adb_serial": "", "warmup": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-Node desktop control center")
    parser.add_argument("--no-ollama-start", action="store_true", help="do not auto-start Ollama")
    parser.add_argument("--demo", action="store_true", help="simulated phone + agent for rehearsal (no phone needed)")
    args = parser.parse_args(argv)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    _enable_windows_dpi_awareness()
    import tkinter as tk
    from gui_app.app import ControlCenter

    root = tk.Tk()
    ControlCenter(root, demo=args.demo, buttons=COMMAND_BUTTONS, primary=PRIMARY_BUTTONS,
                  toggle_map=TOGGLE_BUTTONS, hints=BUTTON_HINTS, auto_start_ollama=AUTO_START_OLLAMA and not args.no_ollama_start, **_load_settings())
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
