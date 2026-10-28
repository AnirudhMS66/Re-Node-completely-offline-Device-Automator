"""Entry point: connect to whatever device is plugged in, accept a natural-language
command, and either run a known/learned skill (fast, reliable macro) or fall back
to the reactive perceive -> plan -> act LLM loop for novel goals.

Usage:
    python main.py                      # interactive: type goals at a prompt
    python main.py "turn on wifi"       # one-shot: run a single goal and exit
"""

import subprocess
import sys
import time

import config
from actuator.actuator import AndroidDispatcher
from brain.brain import query_brain, warm_up_brain
from perception.perception import (
    connect_device,
    extract_visual_coordinates,
    get_current_context,
    get_screen_hierarchy,
)
from skills import KNOWN_SKILLS, get_cached_skill, match_known_skill, save_learned_skill
from utils.logger import logger
from workflows import WorkflowContext, match_workflow, run_workflow
from clock_skill import parse_clock_command
from clock_workflow import run_clock

STATE_VERIFICATION_DELAY_S = 2.5
_SCREENSHOT_PATH = str(config.LOG_DIR / "_last_screen.png")


# --- Perception -------------------------------------------------------------------


def _capture_ui_state(device) -> list[dict]:
    """Perception pass: XML hierarchy first, OCR fallback when nothing clickable is found."""
    xml_state = get_screen_hierarchy(device)
    if xml_state["elements"]:
        return xml_state["elements"]

    logger.warning("XML hierarchy yielded no clickable elements, falling back to OCR")
    device.screenshot(_SCREENSHOT_PATH)
    ocr_state = extract_visual_coordinates(_SCREENSHOT_PATH)
    return ocr_state["elements"]


def _find_element(ui_elements: list[dict], pattern: str) -> dict | None:
    """Find the first element whose text OR content_desc matches `pattern`
    (case-insensitive regex). Many OEM system UI elements (e.g. this phone's
    quick-settings toggles) expose their label only via content_desc, with
    text set to just "On"/"Off", so both fields must be checked."""
    import re

    regex = re.compile(pattern, re.IGNORECASE)
    for element in ui_elements:
        haystack = f"{element.get('text', '')} {element.get('content_desc', '')}"
        if regex.search(haystack):
            return element
    # Icon-only buttons (e.g. a camera's flip button) often expose only a resource id.
    for element in ui_elements:
        if regex.search(element.get("resource_id", "")):
            return element
    return None


# --- Environment check --------------------------------------------------------


def _announce_environment(device) -> None:
    """Log what device/screen we're connected to right after plugging in, so the
    user gets immediate confirmation the environment was understood."""
    info = device.info
    context = get_current_context(device)
    logger.info(
        f"Connected: {info.get('productName', 'unknown device')} "
        f"({info.get('displayWidth')}x{info.get('displayHeight')}, "
        f"Android SDK {info.get('sdkInt')}) — "
        f"currently in {context.get('package') or 'unknown app'}"
    )


# --- Tier 1: fixed/learned skill execution ----------------------------------------


def _run_fixed_steps(device, dispatcher: AndroidDispatcher, steps: list[dict]) -> bool:
    """Execute a hand-written or learned action sequence. Returns True if every
    step executed without error (does not guarantee the goal was achieved —
    only that the macro ran; for hard verification, a future improvement is to
    check UI state after the sequence)."""
    width, height = device.window_size()

    for step in steps:
        kind = step.get("action")
        try:
            if kind == "swipe_down":
                dispatcher.swipe_down_from_top(width, height)
            elif kind == "swipe_up":
                dispatcher.swipe_up_to_close(width, height)
            elif kind == "click":
                if "target_coords" in step:
                    x, y = step["target_coords"]
                else:
                    ui_elements = _capture_ui_state(device)
                    match = _find_element(ui_elements, step["element_match"])
                    if match is None:
                        logger.error(f"Skill step failed: no element matching {step['element_match']!r}")
                        return False
                    x, y = match["coordinates"]["x"], match["coordinates"]["y"]
                dispatcher.tap(x, y)
            elif kind == "keyevent":
                dispatcher.keyevent(step["key"])
            elif kind == "start_intent":
                dispatcher.start_intent(step["intent"])
            elif kind == "wait":
                pass  # the sleep below already runs after every step
            else:
                logger.warning(f"Unknown skill step type: {kind!r}, skipping")

        except (subprocess.CalledProcessError, KeyError) as exc:
            logger.error(f"Skill step {step!r} failed: {exc}")
            return False

        time.sleep(STATE_VERIFICATION_DELAY_S)

    return True


# --- Tier 2: reactive per-step LLM loop -------------------------------------------


def _dispatch_action(device, dispatcher: AndroidDispatcher, action: dict) -> None:
    """Execute a single brain decision via the actuator."""
    kind = action.get("action")

    if kind == "click":
        x, y = action["target_coords"]
        dispatcher.tap(x, y)
    elif kind == "type":
        dispatcher.type_text(action.get("input_text", ""))
    elif kind == "scroll_down":
        width, height = device.window_size()
        cx = width // 2
        dispatcher.swipe(cx, int(height * 0.8), cx, int(height * 0.2))
    else:
        logger.warning(f"No physical action to dispatch for action type: {kind!r}")


def _run_reactive_loop(device, dispatcher: AndroidDispatcher, goal: str, max_steps: int) -> bool:
    """The original perceive -> plan -> act LLM loop, used when no known/learned
    skill matches the goal. On success, the executed action sequence is saved
    to the skill cache so the same goal runs as a fast macro next time."""
    step_history: list[dict] = []

    for step in range(1, max_steps + 1):
        logger.info(f"--- Step {step}/{max_steps} ---")

        ui_elements = _capture_ui_state(device)
        context = get_current_context(device)
        action = query_brain(goal, ui_elements, step_history, context=context)
        step_history.append(action)

        logger.info(f"Action: {action.get('action')} | Reasoning: {action.get('reasoning')}")

        if action.get("action") == "complete":
            logger.info("Goal reported complete by brain.")
            learned_steps = [
                {"action": a["action"], "target_coords": a.get("target_coords"), "input_text": a.get("input_text")}
                for a in step_history
                if a.get("action") in ("click", "type", "scroll_down")
            ]
            if learned_steps:
                save_learned_skill(goal, learned_steps)
            return True

        if action.get("action") == "fail":
            logger.error(f"Brain reported failure: {action.get('reasoning')}")
            return False

        try:
            _dispatch_action(device, dispatcher, action)
        except subprocess.CalledProcessError as exc:
            logger.error(f"Actuator command failed: {exc}")

        time.sleep(STATE_VERIFICATION_DELAY_S)

    logger.warning(f"Max steps ({max_steps}) reached without completion.")
    return False


# --- Top-level dispatch ------------------------------------------------------------


def run_autonomous_agent(goal: str, max_steps: int = 10) -> bool:
    """Run a single goal to completion, routing through (in order):
      1. A hand-written KNOWN_SKILLS macro, if the goal matches one.
      2. A previously learned skill from the cache, if this exact goal succeeded before.
      3. The reactive per-step LLM loop, as a fallback for novel goals — and on
         success, the sequence it found is cached for next time.

    Returns True if the goal was achieved, False otherwise.
    """
    device = connect_device()
    dispatcher = AndroidDispatcher()
    _announce_environment(device)

    # Flagship workflows ("mirror", "mail") are matched first and only on exact trigger words,
    # so every other goal falls straight through to the original skill/LLM routing below.
    workflow_name = match_workflow(goal)
    if workflow_name:
        logger.info(f"Matched flagship workflow: {workflow_name!r}")
        ctx = WorkflowContext(
            device=device,
            dispatcher=dispatcher,
            find_element=_find_element,
            capture_ui_state=_capture_ui_state,
        )
        return run_workflow(workflow_name, ctx, goal)

    # Timer / alarm commands ("set timer for 5 minutes", "alarm at 7 am"). The parser only claims a command it
    # fully understands, so everything else falls through to the original routing below.
    clock_intent = parse_clock_command(goal)
    if clock_intent:
        logger.info(f"Matched clock command: {clock_intent.kind}")
        ctx = WorkflowContext(
            device=device,
            dispatcher=dispatcher,
            find_element=_find_element,
            capture_ui_state=_capture_ui_state,
        )
        return run_clock(ctx, clock_intent, goal)

    skill_name = match_known_skill(goal)
    if skill_name:
        logger.info(f"Matched known skill: {skill_name!r} — running fixed macro")
        return _run_fixed_steps(device, dispatcher, KNOWN_SKILLS[skill_name])

    cached_steps = get_cached_skill(goal)
    if cached_steps:
        logger.info(f"Found learned skill for goal {goal!r} — replaying cached sequence")
        return _run_fixed_steps(device, dispatcher, cached_steps)

    logger.info("No known/learned skill matched — falling back to reactive LLM loop")
    return _run_reactive_loop(device, dispatcher, goal, max_steps)


def _interactive_loop() -> None:
    print("Re-Node ready. Type a command (e.g. 'turn on wifi', 'mirror', 'mail'), or 'quit' to exit.")
    if config.BRAIN_WARMUP:
        print("Warming up the local model (one-time)...")
        warm_up_brain()
    while True:
        try:
            goal = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not goal:
            continue
        if goal.lower() in ("quit", "exit"):
            break
        run_autonomous_agent(goal=goal)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_autonomous_agent(goal=" ".join(sys.argv[1:]))
    else:
        _interactive_loop()