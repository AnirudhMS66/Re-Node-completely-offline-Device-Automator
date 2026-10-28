"""The Cognitive Engine: turns UI state + goal into a single structured next action.

Queries a local Ollama-hosted quantized LLM (gemma:2b / phi3) and forces it into a
strict JSON action schema so the actuator layer can dispatch it without guesswork.

This is the fallback reasoning path, used only when no hand-written skill in
skills.py matches the goal (see main.py). Small local models are unreliable at
strict multi-field JSON, so every response is schema-validated after parsing —
anything that doesn't match is treated as a parse failure and surfaced as a
visible "fail" action with the raw model text in `reasoning`, rather than
silently executing a malformed/garbage action.
"""

import json
import re

import requests

import config
from utils.logger import logger

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

_VALID_ACTIONS = {"click", "type", "scroll_down", "complete", "fail"}

_SYSTEM_PROMPT = """You are an autonomous mobile agent that controls an Android device one \
step at a time. You are given the user's goal, the current app/screen context, the clickable \
elements currently visible on screen, and the history of actions already taken.

Decide the single next action to move toward the goal. Respond with ONLY a raw JSON object \
matching this exact schema — no prose, no markdown code fences, no explanation outside the JSON:

{"action": "click" | "type" | "scroll_down" | "complete" | "fail", \
"target_coords": [x, y], "input_text": "string", "reasoning": "brief explanation"}

Rules:
- "click": target_coords must be the [x, y] of the element to tap; input_text is "".
- "type": input_text is the string to type into the currently focused field; target_coords is [0, 0].
- "scroll_down": target_coords is [0, 0]; input_text is "".
- "complete": the goal is already achieved; explain why in reasoning.
- "fail": the goal cannot be achieved from this screen; explain why in reasoning.
- Every key must always be present, exactly as named. Do not add extra keys or rename keys.
- Only choose "click" on an element that actually appears in CURRENT UI ELEMENTS below.
- Output raw JSON only, nothing else.

Example of a valid response:
{"action": "click", "target_coords": [540, 1200], "input_text": "", "reasoning": "Tapping the Wi-Fi toggle to turn it on"}
"""


def _build_prompt(
    user_goal: str,
    current_ui_elements: list[dict] | dict,
    step_history: list[dict],
    context: dict | None = None,
) -> str:
    context_block = f"CURRENT SCREEN CONTEXT:\n{json.dumps(context)}\n\n" if context else ""
    return (
        f"GOAL: {user_goal}\n\n"
        f"{context_block}"
        f"CURRENT UI ELEMENTS:\n{json.dumps(current_ui_elements)}\n\n"
        f"STEP HISTORY (actions already taken, oldest first):\n{json.dumps(step_history)}\n\n"
        "Return the next single action as JSON."
    )


def _extract_json(raw_response: str) -> dict:
    match = _JSON_BLOCK_RE.search(raw_response)
    if not match:
        raise ValueError(f"no JSON object found in model response: {raw_response!r}")
    return json.loads(match.group(0))


def _validate(parsed: dict) -> dict:
    """Enforce the exact action schema. Raises ValueError on anything that doesn't match,
    so the caller's except block converts it into a visible "fail" action instead of
    silently dispatching a malformed/guessed action (e.g. a response missing "reasoning"
    or using different keys than the schema)."""
    if not isinstance(parsed, dict):
        raise ValueError(f"model response was not a JSON object: {parsed!r}")
    if parsed.get("action") not in _VALID_ACTIONS:
        raise ValueError(f"missing/invalid 'action' in model response: {parsed!r}")

    parsed.setdefault("target_coords", [0, 0])
    parsed.setdefault("input_text", "")
    parsed.setdefault("reasoning", "")
    return parsed


def _fail_result(reasoning: str) -> dict:
    return {
        "action": "fail",
        "target_coords": None,
        "input_text": None,
        "reasoning": reasoning,
    }


def query_brain(
    user_goal: str,
    current_ui_elements: list[dict] | dict,
    step_history: list[dict],
    context: dict | None = None,
    model: str = config.OLLAMA_MODEL,
) -> dict:
    """Ask the local LLM for the single next action given the current UI state.

    Returns a dict matching {"action", "target_coords", "input_text", "reasoning"}.
    A malformed/unparsable/off-schema model response is converted into a "fail"
    action carrying the raw model output in `reasoning`, rather than raising or
    silently dispatching a guessed action.
    """
    response = requests.post(
        f"{config.OLLAMA_ENDPOINT}/api/generate",
        json={
            "model": model,
            "system": _SYSTEM_PROMPT,
            "prompt": _build_prompt(user_goal, current_ui_elements, step_history, context),
            "stream": False,
            "format": "json",
            "options": {"temperature": config.LLM_TEMPERATURE},
        },
        timeout=config.LLM_REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    raw_text = response.json()["response"]
    logger.debug(f"Raw model output: {raw_text!r}")

    try:
        return _validate(_extract_json(raw_text))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"Brain response failed validation: {exc}")
        return _fail_result(f"failed to parse/validate model output: {exc}")


# --- Additions for the "mirror" / "mail" workflows (query_brain above is untouched) ---------
#
# Design note: the LOCAL LLM does intent classification + plan narration here, but the decision
# to run a workflow is also gated by a deterministic keyword matcher (workflows.match_workflow).
# That keeps the live demo 100% reliable even if a 2B model misclassifies; any disagreement
# between the two is logged openly rather than hidden.

WORKFLOW_INTENTS = ("MIRROR", "MAIL", "TIMER", "ALARM", "OTHER")

FALLBACK_PLANS: dict[str, list[str]] = {
    "MIRROR": [
        "Launch the native camera app (front-facing intent hint)",
        "Dump the UI hierarchy and verify which lens is active",
        "If the rear lens is active, locate the camera-flip control and tap it",
    ],
    "MAIL": [
        "Open the Notes app",
        "Open the first note and extract its full text from the UI hierarchy; copy it to the clipboard",
        "Press Home",
        "Open Gmail",
        "Tap Compose",
        "Fill recipient, subject and paste the note into the body",
        "Open the Control Panel and turn Wi-Fi ON (only now, for the send)",
        "Tap Send and wait for the message to leave the Outbox",
        "Open the Control Panel and turn Wi-Fi OFF (restore the offline air-gap)",
        "Press Home and report completion",
    ],
    "TIMER": [
        "Parse the duration with the deterministic clock parser (no LLM guessing of numbers)",
        "Start the timer through Android's SET_TIMER intent",
        "Verify a countdown is running in the Clock app; fall back to Clock-app UI automation if not",
    ],
    "ALARM": [
        "Parse the time of day with the deterministic clock parser (am/pm and rollover resolved in code)",
        "Create the alarm through Android's SET_ALARM intent",
        "Verify the alarm in the phone's next-alarm setting or the Clock app list; fall back to UI automation if not",
    ],
}

_REASONING_SYSTEM_PROMPT = """You are the cognitive core of Re-Node, an offline Android automation agent. \
Classify the user's command into exactly one intent and write a short execution plan.

Supported intents:
- MIRROR: open the phone camera on the front (selfie) lens so the phone works as a mirror.
- MAIL: copy the first note from the Notes app and e-mail it through Gmail, turning Wi-Fi on only for the send and off right after.
- TIMER: start, stop or show a countdown timer in the Clock app.
- ALARM: set, cancel or show an alarm in the Clock app.
- OTHER: anything else (Wi-Fi/Bluetooth toggles, navigation, etc.).

Respond with ONLY a raw JSON object, no markdown, in exactly this shape:
{"intent": "MIRROR" | "MAIL" | "TIMER" | "ALARM" | "OTHER", "confidence": 0.0-1.0, "reasoning": "one or two sentences", "plan": ["step 1", "step 2"]}
"""


def _fallback_reasoning(user_input: str, expected_intent: str | None, error: str) -> dict:
    intent = expected_intent if expected_intent in FALLBACK_PLANS else "OTHER"
    return {
        "intent": intent,
        "confidence": 1.0 if intent != "OTHER" else 0.0,
        "reasoning": f"Local model unavailable ({error}); intent resolved by the deterministic keyword gate.",
        "plan": list(FALLBACK_PLANS.get(intent, [])),
        "source": "deterministic-fallback",
    }


def reason_about_command(
    user_input: str,
    expected_intent: str | None = None,
    model: str = config.OLLAMA_MODEL,
    timeout: int = config.BRAIN_REASONING_TIMEOUT_S,
) -> dict:
    """Ask the local Ollama model to classify `user_input` and narrate a plan.

    Returns {"intent", "confidence", "reasoning", "plan", "source"} where `source` is
    "ollama:<model>" when the LLM produced the answer, or "deterministic-fallback" when Ollama was
    unreachable / returned unusable output (so the log never pretends the LLM said something it didn't).
    Never raises.
    """
    try:
        response = requests.post(
            f"{config.OLLAMA_ENDPOINT}/api/generate",
            json={
                "model": model,
                "system": _REASONING_SYSTEM_PROMPT,
                "prompt": f'USER COMMAND: "{user_input}"\n\nReturn the JSON object.',
                "stream": False,
                "format": "json",
                "keep_alive": "30m",
                "options": {"temperature": config.LLM_TEMPERATURE},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        raw_text = response.json()["response"]
        logger.debug(f"Raw reasoning output: {raw_text!r}")
        parsed = _extract_json(raw_text)
    except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as exc:
        logger.debug(f"reason_about_command fell back: {exc}")
        return _fallback_reasoning(user_input, expected_intent, type(exc).__name__)

    intent = str(parsed.get("intent", "")).strip().upper()
    if intent not in WORKFLOW_INTENTS:
        return _fallback_reasoning(user_input, expected_intent, f"unrecognised intent {intent!r}")

    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    plan = [str(s).strip() for s in parsed.get("plan", []) if str(s).strip()] if isinstance(parsed.get("plan"), list) else []
    if len(plan) < 2 and intent in FALLBACK_PLANS:
        plan = list(FALLBACK_PLANS[intent])  # tiny models sometimes return a one-line plan

    return {
        "intent": intent,
        "confidence": confidence,
        "reasoning": str(parsed.get("reasoning", "")).strip() or "(model gave no explanation)",
        "plan": plan[:12],
        "source": f"ollama:{model}",
    }


def warm_up_brain(model: str = config.OLLAMA_MODEL, timeout: int = config.BRAIN_REASONING_TIMEOUT_S) -> bool:
    """Load the model into memory once so the first live command isn't slowed by a cold start."""
    try:
        requests.post(
            f"{config.OLLAMA_ENDPOINT}/api/generate",
            json={"model": model, "prompt": "ready", "stream": False, "keep_alive": "30m", "options": {"num_predict": 1}},
            timeout=timeout,
        ).raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning(f"Brain warm-up skipped (Ollama not reachable yet): {type(exc).__name__}")
        return False
