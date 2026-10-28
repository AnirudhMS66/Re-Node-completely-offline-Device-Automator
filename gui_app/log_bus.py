"""Log plumbing for the GUI: a loguru sink that pushes formatted lines into a thread-safe queue, plus
pure parsers that turn the agent's existing log prefixes into dashboard state.

Threading rule: the sink runs on whatever thread logged (usually the agent worker thread). It only
does queue.put(). The Tk main thread drains the queue with root.after(...). No Tk widget is ever
touched from a worker thread.
"""

from __future__ import annotations

import queue
import re
from dataclasses import dataclass, field
from datetime import datetime

LOG_FORMAT = "{time:HH:mm:ss}|{level}|{message}"

_LINE_RE = re.compile(r"^(?P<time>\d{2}:\d{2}:\d{2})\|(?P<level>[A-Z]+)\|(?P<message>.*)$")
_STEP_RE = re.compile(r"\[STEP\s+(\d+)\s*/\s*(\d+)\]\s*(.*)")
_REACTIVE_STEP_RE = re.compile(r"---\s*Step\s+(\d+)\s*/\s*(\d+)\s*---")
_NOTE_LINE_RE = re.compile(r"^\[NOTE\]\s+\|\s?(.*)$")
_INTENT_RE = re.compile(
    r"\[BRAIN\]\s+Intent detected:\s*(?P<intent>\S+)\s*\(confidence\s+(?P<conf>[\d.]+),\s*source=(?P<source>[^,]+),\s*(?P<elapsed>[\d.]+)s\)"
)
_PLAN_ITEM_RE = re.compile(r"^\[BRAIN\]\s+(\d+)\.\s+(.*)$")


# =============================================================================================
# Line parsing + colour classification
# =============================================================================================


@dataclass
class LogLine:
    time: str
    level: str
    message: str
    tag: str
    continuation: bool = False

    def render(self) -> str:
        if self.continuation:
            return f"         {self.message}"
        return f"{self.time} {self.level:<8} {self.message}"


def classify(level: str, message: str) -> str:
    """Colour tag for a log line. Order matters: failures beat everything, a [SAFETY] warning stays
    orange, other warnings are amber, then the informational prefixes."""
    level = level.upper()
    if level in ("ERROR", "CRITICAL") or "[FAILED]" in message or "[ERROR]" in message:
        return "error"
    if "[SAFETY]" in message:
        return "safety"
    if level == "WARNING":
        return "warning"
    if "[DONE]" in message:
        return "done"
    if "[NOTE]" in message:
        return "note"
    if "[WIFI]" in message:
        return "wifi"
    if "[BRAIN]" in message:
        return "brain"
    if "[STEP" in message or _REACTIVE_STEP_RE.search(message):
        return "step"
    if level in ("DEBUG", "TRACE") or "[DEBUG]" in message:
        return "debug"
    if "[GUI]" in message:
        return "gui"
    return "info"


def parse_raw(raw: str) -> list[LogLine]:
    """Split one formatted sink message ("HH:MM:SS|LEVEL|text", possibly followed by a traceback on
    extra lines) into LogLines. Lines that don't match the format are kept as plain INFO text."""
    lines = raw.rstrip("\r\n").splitlines() or [""]
    match = _LINE_RE.match(lines[0])
    if match:
        time_s, level, message = match.group("time"), match.group("level"), match.group("message")
    else:
        time_s, level, message = datetime.now().strftime("%H:%M:%S"), "INFO", lines[0]
    head = LogLine(time_s, level, message, classify(level, message))
    out = [head]
    for extra in lines[1:]:
        if extra.strip():
            out.append(LogLine(time_s, level, extra, "error" if head.tag == "error" else head.tag, continuation=True))
    return out


def parse_step(message: str) -> tuple[int, int, str] | None:
    """[STEP 3/11] text  -> (3, 11, "text"). Also understands the reactive loop's "--- Step 2/10 ---"."""
    m = _STEP_RE.search(message)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(3).strip()
    m = _REACTIVE_STEP_RE.search(message)
    if m:
        return int(m.group(1)), int(m.group(2)), "LLM reasoning step"
    return None


# =============================================================================================
# Dashboard trackers (fed one message at a time, on the Tk thread)
# =============================================================================================


@dataclass
class BrainState:
    command: str = ""
    model: str = ""
    intent: str = ""
    confidence: float | None = None
    source: str = ""
    elapsed_s: float | None = None
    reasoning: str = ""
    plan: list[str] = field(default_factory=list)
    disagreement: str = ""

    @property
    def is_fallback(self) -> bool:
        return self.source == "deterministic-fallback"

    @property
    def source_tag(self) -> str:
        if not self.source:
            return ""
        return "FALLBACK (Ollama unreachable)" if self.is_fallback else self.source


class BrainTracker:
    """Rebuilds the latest brain decision from the [BRAIN] lines that workflows.log_brain_reasoning()
    already prints. A new "Input received" line starts a fresh card."""

    def __init__(self):
        self.state = BrainState()

    def feed(self, message: str) -> bool:
        if "[BRAIN]" not in message:
            return False
        s = self.state
        if "Input received:" in message:
            self.state = BrainState(command=message.split("Input received:", 1)[1].strip().strip("'\""))
            return True
        if "Querying local model" in message:
            m = re.search(r"Querying local model\s+'?([^'\s]+)'?", message)
            if m:
                s.model = m.group(1)
            return True
        m = _INTENT_RE.search(message)
        if m:
            s.intent = m.group("intent")
            s.confidence = float(m.group("conf"))
            s.source = m.group("source").strip()
            s.elapsed_s = float(m.group("elapsed"))
            return True
        if "Reasoning:" in message:
            s.reasoning = message.split("Reasoning:", 1)[1].strip()
            return True
        if "Execution plan:" in message:
            s.plan = []
            return True
        if "Model said" in message:
            s.disagreement = message.split("[BRAIN]", 1)[1].strip()
            return True
        m = _PLAN_ITEM_RE.match(message.strip())
        if m:
            s.plan.append(m.group(2).strip())
            return True
        return False


class NoteTracker:
    """Collects the "[NOTE]   | ..." lines the mail workflow prints for the note it copied."""

    def __init__(self):
        self.lines: list[str] = []
        self._collecting = False

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def feed(self, message: str) -> bool:
        if "[NOTE]" not in message:
            return False
        m = _NOTE_LINE_RE.match(message.strip("\r\n"))
        if m:
            if not self._collecting:  # first content line of a new note
                self.lines = []
                self._collecting = True
            self.lines.append(m.group(1))
            return True
        # Header line ("[NOTE] Copied note content ...") starts a new note.
        self.lines = []
        self._collecting = True
        return True

    def end_block(self) -> None:
        """Called on any non-[NOTE] line so the next [NOTE] block replaces this one."""
        self._collecting = False


@dataclass
class StepState:
    current: int = 0
    total: int = 0
    text: str = ""

    @property
    def fraction(self) -> float:
        return self.current / self.total if self.total else 0.0


# =============================================================================================
# The bus
# =============================================================================================


class LogBus:
    """Thread-safe log queue with an optional loguru sink."""

    def __init__(self, maxsize: int = 0):
        self.queue: queue.Queue[str] = queue.Queue(maxsize=maxsize)
        self._sink_id: int | None = None
        self._logger = None

    # producer side (any thread) -----------------------------------------------------------

    def put_raw(self, raw: str) -> None:
        try:
            self.queue.put_nowait(str(raw))
        except queue.Full:
            pass  # never block or raise inside a logging call

    def emit(self, level: str, message: str) -> None:
        """Log a line without loguru (used by the GUI itself and by --demo)."""
        self.put_raw(f"{datetime.now():%H:%M:%S}|{level.upper()}|{message}")

    def attach_loguru(self, logger) -> bool:
        """Add a DEBUG-level sink to the project's loguru logger. Returns False if it can't."""
        if self._sink_id is not None:
            return True
        try:
            self._sink_id = logger.add(self.put_raw, level="DEBUG", format=LOG_FORMAT, colorize=False)
            self._logger = logger
            return True
        except Exception:
            self._sink_id = None
            return False

    def detach(self) -> None:
        if self._sink_id is not None and self._logger is not None:
            try:
                self._logger.remove(self._sink_id)
            except ValueError:
                pass
        self._sink_id = None
        self._logger = None

    # consumer side (Tk thread) ------------------------------------------------------------

    def drain(self, max_items: int = 500) -> list[LogLine]:
        """Pull up to `max_items` raw messages (bounded so a log flood can't freeze one Tk tick)."""
        out: list[LogLine] = []
        for _ in range(max_items):
            try:
                raw = self.queue.get_nowait()
            except queue.Empty:
                break
            out.extend(parse_raw(raw))
        return out
