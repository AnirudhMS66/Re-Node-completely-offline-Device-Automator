"""The Skill Library: fast, reliable macros for common goals, plus a cache of
goals the reactive brain has already solved once.

Two tiers, both keyed by matching the user's goal text:

1. KNOWN_SKILLS — hand-written, hardcoded action sequences for common system
   actions (Wi-Fi, Bluetooth, etc.) that don't need the LLM at all. These are
   fast and near-100% reliable because they're not guessed per-step.

2. Learned skill cache (skills_cache.json on disk) — goals the reactive
   per-step brain loop has previously completed successfully. Once a goal
   succeeds, its action sequence is saved here, so the next time the same (or
   a very similar) goal is requested, it replays instantly instead of calling
   the LLM again.

Matching is intentionally simple (keyword containment) rather than ML-based,
so it stays debuggable. Swap `_match_skill` for an LLM/embedding classifier
later without changing the dispatch logic in main.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import config
from utils.logger import logger

_CACHE_PATH = config.BASE_DIR / "skills_cache.json"

# --- Tier 1: hand-written macros -------------------------------------------------
# Each step is a dict the dispatcher in main.py already knows how to execute:
#   {"action": "swipe_down" | "swipe_up" | "click" | "wait" | "keyevent" | "start_intent"}
# "click" steps use "element_match" (substring, case-insensitive) instead of
# fixed coordinates, since toggle positions shift across Android/OEM versions.
# The dispatcher resolves element_match against the live UI elements each time
# it runs that step, so these stay robust across phones/launchers.

KNOWN_SKILLS: dict[str, list[dict]] = {
    "toggle_wifi": [
        {"action": "swipe_down"},
        {"action": "wait"},
        {"action": "click", "element_match": "wi-fi|wifi"},
        {"action": "wait"},
        {"action": "swipe_up"},
    ],
    "toggle_bluetooth": [
        {"action": "swipe_down"},
        {"action": "wait"},
        {"action": "click", "element_match": "bluetooth"},
        {"action": "wait"},
        {"action": "swipe_up"},
    ],
    "toggle_flashlight": [
        {"action": "swipe_down"},
        {"action": "wait"},
        {"action": "click", "element_match": "flashlight|torch"},
        {"action": "wait"},
        {"action": "swipe_up"},
    ],
    "toggle_airplane_mode": [
        {"action": "swipe_down"},
        {"action": "wait"},
        {"action": "click", "element_match": "airplane"},
        {"action": "wait"},
        {"action": "swipe_up"},
    ],
    "open_selfie_camera": [
        {"action": "start_intent", "intent": "android.media.action.STILL_IMAGE_CAMERA"},
        {"action": "wait"},
        {"action": "click", "element_match": r"switch.*camera|camera.*switch|flip|selfie|front camera"},
    ],
    "open_quick_settings": [
        {"action": "swipe_down"},
    ],
    "go_home": [
        {"action": "keyevent", "key": "HOME"},
    ],
    "go_back": [
        {"action": "keyevent", "key": "BACK"},
    ],
}

# Keyword groups used to match a free-text goal to a KNOWN_SKILLS entry.
# All keywords in a group must appear (in any order) for that skill to match.
_SKILL_KEYWORDS: dict[str, list[str]] = {
    "toggle_wifi": ["wifi"],
    "toggle_bluetooth": ["bluetooth"],
    "toggle_flashlight": ["flashlight"],
    "toggle_airplane_mode": ["airplane"],
    "open_selfie_camera": ["mirror"],
    "open_quick_settings": ["quick", "settings"],
    "go_home": ["home", "screen"],
    "go_back": ["go", "back"],
}

_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]+")


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation, but drop hyphens/spaces entirely first so
    variants like "Wi-Fi", "wi fi", and "wifi" all collapse to the same keyword."""
    collapsed = text.lower().replace("-", "").replace("wi fi", "wifi")
    return _NORMALIZE_RE.sub(" ", collapsed)


def match_known_skill(goal: str) -> str | None:
    """Return the KNOWN_SKILLS key matching this goal, or None."""
    normalized = _normalize(goal)
    words = set(normalized.split())

    for skill_name, keywords in _SKILL_KEYWORDS.items():
        if all(kw in normalized or kw in words for kw in keywords):
            return skill_name
    return None


# --- Tier 2: learned skill cache -------------------------------------------------


def _load_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Could not read skill cache, starting fresh: {exc}")
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Could not write skill cache: {exc}")


def _cache_key(goal: str) -> str:
    return _normalize(goal).strip()


def get_cached_skill(goal: str) -> list[dict] | None:
    """Return a previously-learned action sequence for this exact goal text, if any."""
    cache = _load_cache()
    entry = cache.get(_cache_key(goal))
    return entry["steps"] if entry else None


def save_learned_skill(goal: str, action_sequence: list[dict]) -> None:
    """Persist a successful reactive-loop run so the same goal replays instantly next time."""
    cache = _load_cache()
    cache[_cache_key(goal)] = {"goal": goal, "steps": action_sequence}
    _save_cache(cache)
    logger.info(f"Learned new skill for goal: {goal!r} ({len(action_sequence)} steps)")
