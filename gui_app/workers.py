"""Background work for the GUI: the single agent runner and the read-only pollers.

Every function here runs OFF the Tk thread and reports back only by putting tuples on a
queue.Queue that the Tk thread drains. Every external call has a timeout:
  * adb calls       -> subprocess timeout (default 4 s)
  * Ollama status   -> urllib timeout 1 s, proxies disabled (localhost only, nothing leaves the PC)

Phone safety: the pollers only use READ-ONLY adb commands (`adb devices`, `getprop`, `wm size`,
`settings get global wifi_on`). The device-info poller is additionally PAUSED while a command runs,
so it can never compete with the agent. The Wi-Fi poller keeps running during a run on purpose --
that is what measures the network-exposure window of the "mail" workflow.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash on Windows


# =============================================================================================
# Result types
# =============================================================================================


@dataclass
class RunResult:
    goal: str
    ok: bool
    duration_s: float
    error: str = ""          # one-line summary for the badge / history
    details: str = ""        # full traceback for the log console


@dataclass
class DeviceInfo:
    connected: bool
    serial: str = ""
    model: str = ""
    android: str = ""
    sdk: str = ""
    resolution: str = ""
    error: str = ""


@dataclass
class OllamaStatus:
    reachable: bool
    model: str
    model_present: bool = False
    models: list[str] = field(default_factory=list)
    error: str = ""


# =============================================================================================
# The agent runner (exactly one command at a time)
# =============================================================================================


class AgentRunner:
    """Runs `agent_fn(goal) -> bool` in a daemon thread. start() refuses a second command while one
    is running (double-click safe). There is deliberately NO stop: the agent can't be interrupted
    safely mid-step, so the GUI never pretends it can."""

    def __init__(self, agent_fn: Callable[[str], bool], events: queue.Queue):
        self.agent_fn = agent_fn
        self.events = events
        self._lock = threading.Lock()
        self._busy = False
        self.current_goal: str = ""
        self.started_at: float = 0.0

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self, goal: str) -> bool:
        goal = goal.strip()
        if not goal:
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self.current_goal = goal
            self.started_at = time.monotonic()
        threading.Thread(target=self._work, args=(goal,), name="renode-agent", daemon=True).start()
        return True

    def _work(self, goal: str) -> None:
        started = self.started_at
        try:
            ok = bool(self.agent_fn(goal))
            reason = getattr(self.agent_fn, "last_error", "") or "agent returned False (see log)"
            result = RunResult(goal, ok, time.monotonic() - started, "" if ok else reason)
        except BaseException as exc:  # noqa: BLE001 -- the GUI must survive anything the agent raises
            result = RunResult(
                goal,
                False,
                time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}"[:300],
                details=traceback.format_exc(),
            )
        finally:
            with self._lock:
                self._busy = False
        self.events.put(("run_done", result))


def load_real_agent() -> Callable[[str], bool]:
    """Import main.py lazily (it pulls in uiautomator2 / easyocr, which can take several seconds).
    Importing main starts nothing -- its CLI only runs under `if __name__ == "__main__"`.

    If the OCR packages (easyocr / torch / cv2 / numpy) are not installed, retry with a stub `easyocr`:
    OCR is only the fallback for screens without an XML tree, so every macro and workflow still works."""
    try:
        import main  # noqa: PLC0415
    except ImportError as exc:
        if exc.name not in ("easyocr", "torch", "cv2", "numpy"):
            raise
        import sys  # noqa: PLC0415
        import types  # noqa: PLC0415

        from utils.logger import logger  # noqa: PLC0415

        logger.warning(f"OCR unavailable ({exc.name} not installed); running without the OCR fallback")
        for name in [m for m in sys.modules if m == "main" or m.startswith("perception")]:
            del sys.modules[name]  # drop half-imported modules before retrying
        stub = types.ModuleType("easyocr")
        stub.Reader = object
        sys.modules["easyocr"] = stub
        import main  # noqa: PLC0415,F811

    return main.run_autonomous_agent


class ToggleAgent:
    """The callable the AgentRunner runs. "@toggle:<kind>" goals go to the real on/off toggle logic
    (direct adb for Wi-Fi / Bluetooth, so they work even if the heavy agent failed to import); every other
    goal is passed straight to the existing run_autonomous_agent()."""

    def __init__(self, shell, emit, get_agent: Callable[[], Callable[[str], bool] | None]):
        self.shell = shell
        self.emit = emit
        self.get_agent = get_agent
        self.torch_assumed: bool | None = None
        self.last_error = ""
        self.last_result = None

    def __call__(self, goal: str) -> bool:
        from gui_app import toggles  # noqa: PLC0415

        self.last_error = ""
        kind = toggles.parse_goal(goal)
        want = None
        if kind is None:
            spoken = toggles.parse_spoken(goal)       # "turn on wifi", "bluetooth off": instant, direct adb
            if spoken:
                kind, want = spoken
        agent = self.get_agent()
        if kind is None:
            if agent is None:
                raise RuntimeError("The agent is not loaded (see the NO AGENT message / log)")
            return agent(goal)
        if kind == "flashlight":
            result = toggles.toggle_flashlight(agent, self.torch_assumed, self.emit, want)
            if result.ok:
                self.torch_assumed = result.now
        else:
            result = toggles.toggle_radio(kind, self.shell, agent, self.emit, want=want)
        self.last_result = result
        if not result.ok:
            self.last_error = result.message
        self.emit("INFO" if result.ok else "ERROR", f"[GUI] {result.message}")
        return result.ok


def make_adb_shell(configured_serial: str, timeout: float = 8.0):
    """shell(["settings", "get", ...]) -> stdout, against the configured / first ready phone."""

    def shell(args: list[str]) -> str:
        serial, problem = pick_serial(list_adb_devices(timeout), configured_serial)
        if not serial:
            raise RuntimeError(problem)
        return _adb(["-s", serial, "shell", *args], timeout)

    return shell


# =============================================================================================
# Read-only probes
# =============================================================================================


def _adb(args: list[str], timeout: float) -> str:
    exe = shutil.which("adb")
    if exe is None:
        raise FileNotFoundError("adb not found on PATH (install Android platform-tools)")
    proc = subprocess.run(
        [exe, *args], capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"adb exited {proc.returncode}")
    return proc.stdout


def list_adb_devices(timeout: float = 4.0) -> list[tuple[str, str]]:
    """[(serial, state)] from `adb devices` (state: device / unauthorized / offline)."""
    out = _adb(["devices"], timeout)
    result = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            result.append((parts[0], parts[1]))
    return result


def pick_serial(devices: list[tuple[str, str]], configured: str) -> tuple[str, str]:
    """(serial, problem). Uses ADB_SERIAL when set, otherwise the first ready device."""
    if configured:
        for serial, state in devices:
            if serial == configured:
                return (serial, "") if state == "device" else ("", f"{serial} is {state}")
        return "", f"ADB_SERIAL {configured} not connected"
    ready = [s for s, st in devices if st == "device"]
    if ready:
        return ready[0], ""
    if devices:
        serial, state = devices[0]
        return "", f"{serial} is {state}" + (" (accept the USB-debugging prompt on the phone)" if state == "unauthorized" else "")
    return "", "no device found by adb"


def parse_wm_size(text: str) -> str:
    """'Physical size: 1080x2400\\nOverride size: 720x1600' -> '720x1600' (override wins)."""
    physical = override = ""
    for line in text.splitlines():
        if "Override size:" in line:
            override = line.split(":", 1)[1].strip()
        elif "Physical size:" in line:
            physical = line.split(":", 1)[1].strip()
    return override or physical


def parse_wifi_on(value: str) -> bool | None:
    """Same mapping as AndroidDispatcher.wifi_enabled(): Settings.Global.wifi_on 1/2 = on, 0/3 = off."""
    value = value.strip()
    if value in ("1", "2"):
        return True
    if value in ("0", "3"):
        return False
    return None


def probe_device(configured_serial: str, timeout: float = 4.0) -> DeviceInfo:
    try:
        serial, problem = pick_serial(list_adb_devices(timeout), configured_serial)
        if not serial:
            return DeviceInfo(False, error=problem)
        props = _adb(
            ["-s", serial, "shell",
             "getprop ro.product.manufacturer; getprop ro.product.model; "
             "getprop ro.build.version.release; getprop ro.build.version.sdk"],
            timeout,
        ).splitlines()
        props = [p.strip() for p in props] + ["", "", "", ""]
        manufacturer, model, release, sdk = props[:4]
        size = parse_wm_size(_adb(["-s", serial, "shell", "wm size"], timeout))
        name = f"{manufacturer} {model}".strip() if manufacturer.lower() not in model.lower() else model
        return DeviceInfo(True, serial=serial, model=name or "unknown", android=release, sdk=sdk, resolution=size)
    except subprocess.TimeoutExpired:
        return DeviceInfo(False, error="adb timed out")
    except Exception as exc:  # noqa: BLE001
        return DeviceInfo(False, error=str(exc)[:200])


def probe_wifi(configured_serial: str, timeout: float = 3.0) -> bool | None:
    """Read-only Wi-Fi state; None when it can't be read (no phone, adb missing, timeout)."""
    try:
        serial, _ = pick_serial(list_adb_devices(timeout), configured_serial)
        if not serial:
            return None
        return parse_wifi_on(_adb(["-s", serial, "shell", "settings", "get", "global", "wifi_on"], timeout))
    except Exception:  # noqa: BLE001
        return None


def probe_bluetooth(configured_serial: str, timeout: float = 3.0) -> bool | None:
    """Read-only Bluetooth state (Settings.Global.bluetooth_on); None when unreadable."""
    try:
        serial, _ = pick_serial(list_adb_devices(timeout), configured_serial)
        if not serial:
            return None
        value = _adb(["-s", serial, "shell", "settings", "get", "global", "bluetooth_on"], timeout).strip()
        return {"1": True, "0": False}.get(value)
    except Exception:  # noqa: BLE001
        return None


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe_ollama(endpoint: str, model: str, timeout: float = 1.0) -> OllamaStatus:
    """GET <endpoint>/api/tags with a 1 s timeout. Proxies are disabled so this can only ever talk
    to the local Ollama server."""
    try:
        with _NO_PROXY_OPENER.open(f"{endpoint.rstrip('/')}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        names = [str(m.get("name", "")) for m in data.get("models", []) if isinstance(m, dict)]
        wanted = model if ":" in model else f"{model}:latest"
        present = any(n == model or n == wanted for n in names)
        return OllamaStatus(True, model, present, names)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return OllamaStatus(False, model, error=str(reason)[:120])


# =============================================================================================
# Auto-start the local Ollama server
# =============================================================================================


def is_local_endpoint(endpoint: str) -> bool:
    """Only ever auto-start Ollama for a server that lives on this PC."""
    host = (urllib.parse.urlparse(endpoint).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def find_ollama_exe() -> str | None:
    """`ollama` on PATH, else the default per-user Windows install location."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidate = os.path.join(local, "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


@dataclass
class OllamaStartResult:
    status: str                      # "already_running" | "started" | "not_installed" | "failed" | "remote"
    message: str
    process: subprocess.Popen | None = None   # set only when WE started it (so we may stop it on exit)

    @property
    def ready(self) -> bool:
        return self.status in ("already_running", "started")


def ensure_ollama(endpoint: str, model: str, wait_s: float = 20.0, *, popen=subprocess.Popen,
                  sleep=time.sleep, probe=None) -> OllamaStartResult:
    """Make sure a local Ollama server answers on `endpoint`, starting `ollama serve` if it doesn't.

    Never raises. Runs off the Tk thread (it can block up to `wait_s`). If Ollama is already running
    (tray app or an earlier `ollama serve`) nothing is started and nothing will be stopped later.
    """
    probe = probe or (lambda: probe_ollama(endpoint, model, 1.0))
    if probe().reachable:
        return OllamaStartResult("already_running", "Ollama already running")
    if not is_local_endpoint(endpoint):
        return OllamaStartResult("remote", f"Ollama endpoint {endpoint} is not local; not auto-starting")
    exe = find_ollama_exe()
    if exe is None:
        return OllamaStartResult("not_installed", "Ollama is not installed (get it from ollama.com/download)")
    try:
        proc = popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        return OllamaStartResult("failed", f"Could not start 'ollama serve': {exc}")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if probe().reachable:
            return OllamaStartResult("started", "Started 'ollama serve'", proc)
        if proc.poll() is not None:
            # Exited early: usually "address already in use" because the tray app is just starting up.
            sleep(1.0)
            if probe().reachable:
                return OllamaStartResult("already_running", "Ollama already running")
            return OllamaStartResult("failed", f"'ollama serve' exited with code {proc.returncode}")
        sleep(0.5)
    return OllamaStartResult("failed", f"'ollama serve' did not answer within {wait_s:.0f} s", proc)


def stop_process(proc) -> None:
    """Terminate a process we started (best effort, never raises)."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                proc.kill()
    except Exception:  # noqa: BLE001
        pass


# =============================================================================================
# Poller
# =============================================================================================


class Poller:
    """Calls `probe()` every `interval` seconds on its own daemon thread and puts (name, result) on
    the events queue. `paused()` is checked before each probe. poll_now() wakes it immediately."""

    def __init__(self, name: str, interval: float, probe: Callable[[], object], events: queue.Queue,
                 paused: Callable[[], bool] | None = None):
        self.name = name
        self.interval = interval
        self.probe = probe
        self.events = events
        self.paused = paused or (lambda: False)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"renode-poll-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poll_now(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.paused():
                self.events.put((self.name, "paused"))
            else:
                try:
                    result = self.probe()
                except Exception as exc:  # noqa: BLE001 -- a probe must never kill its thread
                    result = exc
                if self._stop.is_set():
                    break
                self.events.put((self.name, result))
            self._wake.wait(self.interval)
            self._wake.clear()
