# Re-Node Control Center (gui.py)

Desktop dashboard for the existing agent. Standard library only (tkinter) - no new pip packages.
Terminal mode (`python main.py`) is unchanged.

## Run (Windows PowerShell, from the project folder)

    # 1. rehearse with NO phone, NO adb, NO Ollama (simulated agent)
    python gui.py --demo

    # 2. real thing: phone connected by USB, USB debugging on, `ollama serve` running
    python gui.py

    # tests (no display / phone needed)
    python -m pytest tests -q

Demo goals worth trying: `mail` (11 steps, Wi-Fi exposure window ~10 s), `mirror` (3 steps),
`fail` (returns False), `crash` (agent raises - the app must survive).

## Ollama auto-start
In live mode the window checks http://localhost:11434 on startup. If nothing answers it runs
`ollama serve` hidden in the background (from PATH, or %LOCALAPPDATA%\Programs\Ollama\ollama.exe),
waits up to 20 s, and the model warm-up waits for it. If Ollama was already running (tray app) nothing
is started, and nothing is stopped on exit. If the GUI started it, it is stopped when you close the window.
Turn it off with `python gui.py --no-ollama-start` or `AUTO_START_OLLAMA = False` in gui.py.
Ollama must still be installed and `ollama pull gemma:2b` done once. Not started in --demo mode.

## Quick toggles (Wi-Fi, Bluetooth, Flashlight)
The three switches at the top-left are real on/off toggles. Each click reads the phone's CURRENT state and
switches to the opposite one:
- Wi-Fi / Bluetooth: state read with `adb shell settings get global wifi_on|bluetooth_on` (read-only), switched
  with `svc wifi|bluetooth enable|disable`, then re-read to verify. If the direct command does nothing, the
  existing Quick Settings macro is tried. The switch always shows the real state (polled every 2 s).
- Flashlight: Android does not report the torch state over adb, so this runs the Quick Settings macro and
  the switch shows an ASSUMED state ("ON (assumed)"). If the real torch disagrees, click once more.
- Wi-Fi and Bluetooth work even if the agent failed to load (they talk to adb directly).

## Edit the buttons
`TOGGLE_BUTTONS` and `COMMAND_BUTTONS` at the top of `gui.py` hold the toggles and the action buttons.

## Files
gui.py, voice.py, gui_app/{state,log_bus,workers,toggles,widgets,app,demo,clock_panel}.py, tests/test_gui_logic.py,
tests/test_clock_gui.py. See README_CLOCK.md for the timer / alarm feature and the (small, additive) edits it made
to existing files.

## Behaviour notes
- One command at a time; all buttons are disabled while it runs. There is no Stop button: the agent
  cannot be interrupted safely mid-step.
- Closing the window during a run asks for confirmation (a mail run abandoned between Wi-Fi ON and
  OFF can leave the phone's Wi-Fi ON).
- Device-info polling (every 3 s) pauses while a command runs. Wi-Fi polling (every 2 s, read-only
  `settings get global wifi_on`) keeps running so the exposure timer is accurate.
- Ollama status polls http://localhost:11434/api/tags every 5 s, 1 s timeout, proxies disabled.
- The run-history table and success statistics were removed (the window now has a live Timers & alarms panel
  in their place). Nothing writes logs/gui_runs.json any more; an old copy can be deleted. Up/Down command recall
  still uses logs/gui_history.json. Timers and alarms set by the agent are remembered in logs/clock_state.json.

## Known limitations
- Wi-Fi exposure is measured from the PC side by polling every 2 s, so a window shorter than ~2 s can
  be missed and durations are accurate to about +/- 2 s. The timer only runs while the GUI is open.
- Airplane mode is still the Quick Settings macro (a plain toggle without state read-back).
- Reconnect re-tests the uiautomator2 connection; the agent itself reconnects on every command anyway.
- Brain panel is filled from the [BRAIN] log lines of the mirror/mail workflows only. Known-skill
  and reactive-LLM runs do not print them.
- Auto-start only covers a local endpoint and only starts the server; it cannot install Ollama or pull the model.
- Mic button: basic offline voice via Windows' built-in speech recognizer (voice.py, no pip packages). It is enabled
  only on Windows with PowerShell. Needs a default microphone and an installed speech language. Not tested on a real
  PC from here; if it fails the log says why and typing still works.
- logs/gui_history.json and logs/clock_state.json are not in .gitignore (that file is untouched); add them if
  you don't want them committed.
