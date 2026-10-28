"""Flagship workflows: "mirror" and "mail".

Hybrid design -- the local LLM (brain.reason_about_command) classifies the command and narrates
the plan, while every phone interaction is a deterministic uiautomator2 / ADB step with
multi-selector fallback chains (resource-id -> content-desc -> text) so it behaves the same on
Realme/ColorOS, Xiaomi/MIUI, Samsung and stock Android.

Zero-trust network rule: Wi-Fi is switched ON only inside the "mail" workflow, right before Send,
and OFF immediately after. A `finally` block force-disables it even if a step crashes mid-way.

main.py injects its existing helpers (_capture_ui_state / _find_element -- the validated
quick-settings logic) via WorkflowContext, so there is no duplicated or diverging Control Center code.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

import config
from actuator.actuator import AndroidDispatcher
from brain.brain import reason_about_command
from perception.perception import dump_nodes, extract_note_text, find_by_chain, find_first_list_item, resolve_clickable
from skills import _normalize
from utils.logger import logger

_PLACEHOLDER_RECIPIENT = "your.recipient@example.com"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SYSTEMUI = "com.android.systemui"


class WorkflowError(Exception):
    """A workflow step failed in a way that makes continuing pointless (message is shown to the user)."""


@dataclass
class WorkflowContext:
    device: object                      # uiautomator2 device
    dispatcher: AndroidDispatcher
    find_element: Callable[[list[dict], str], dict | None]   # main._find_element (existing)
    capture_ui_state: Callable[[object], list[dict]]         # main._capture_ui_state (existing)


# =============================================================================================
# Command matching (deterministic gate)
# =============================================================================================

_MIRROR_FILLER = {"the", "a", "my", "open", "show", "start", "launch", "please", "me", "mode", "app", "use", "phone", "selfie"}
_MAIL_FILLER = {
    "the", "a", "my", "send", "note", "notes", "first", "please", "via", "using", "gmail", "this",
    "it", "to", "myself", "latest", "top", "that", "an", "mail", "email",
}


def match_workflow(goal: str) -> str | None:
    """Return "MIRROR" / "MAIL" if `goal` is one of the two flagship commands, else None.

    Deliberately strict: every word must be the trigger word or harmless filler, so "mirror legacy"
    still reaches the old macro and sentences like "read my mail" don't hijack the mail workflow.
    """
    words = _normalize(goal).split()
    if not words:
        return None
    if "mirror" in words and set(words) <= _MIRROR_FILLER | {"mirror"}:
        return "MIRROR"
    if ("mail" in words or "email" in words) and set(words) <= _MAIL_FILLER:
        return "MAIL"
    return None


# =============================================================================================
# Shared helpers
# =============================================================================================


def _step(n: int, total: int, message: str) -> None:
    logger.info(f"[STEP {n}/{total}] {message}")


def _sleep(seconds: float | None = None) -> None:
    time.sleep(config.WORKFLOW_STEP_PAUSE_S if seconds is None else seconds)


def _current_package(ctx: WorkflowContext) -> str:
    try:
        return ctx.device.app_current().get("package") or ""
    except Exception:  # transient while system UI animates
        return ""


def log_brain_reasoning(command: str, expected_intent: str) -> dict:
    """Step 1 of both workflows: query the local Ollama model and print its reasoning to the terminal."""
    logger.info(f"[BRAIN] Input received: {command!r}")
    logger.info(f"[BRAIN] Querying local model {config.OLLAMA_MODEL!r} at {config.OLLAMA_ENDPOINT} (offline, no cloud)...")
    started = time.time()
    result = reason_about_command(command, expected_intent=expected_intent)
    elapsed = time.time() - started

    logger.info(f"[BRAIN] Intent detected: {result['intent']} (confidence {result['confidence']:.2f}, source={result['source']}, {elapsed:.1f}s)")
    if result["intent"] != expected_intent:
        logger.warning(
            f"[BRAIN] Model said {result['intent']} but the deterministic command gate matched {expected_intent}; "
            f"proceeding with {expected_intent}."
        )
    logger.info(f"[BRAIN] Reasoning: {result['reasoning']}")
    logger.info("[BRAIN] Execution plan:")
    for i, step in enumerate(result["plan"], 1):
        logger.info(f"[BRAIN]   {i}. {step}")
    return result


_PERMISSION_PKG_RE = re.compile(r"permissioncontroller|packageinstaller", re.IGNORECASE)
_PERMISSION_BUTTON_ORDER = ["while using the app", "only this time", "allow", "ok"]
_ONBOARDING_BUTTON_RE = r"^(got it|skip|not now|no thanks|maybe later|dismiss|agree|i agree|accept|accept all|take me to gmail|continue)$"


def _dismiss_blockers(ctx: WorkflowContext, nodes: list[dict], pkg: str | None) -> bool:
    """Tap away permission prompts and first-run screens that would otherwise stall a workflow.
    Returns True if something was tapped."""
    perm_buttons = [
        n for n in nodes
        if _PERMISSION_PKG_RE.search(n["package"]) and n["clickable"] and n["text"].strip().lower() in _PERMISSION_BUTTON_ORDER
    ]
    if perm_buttons:
        perm_buttons.sort(key=lambda n: _PERMISSION_BUTTON_ORDER.index(n["text"].strip().lower()))
        target = perm_buttons[0]
        logger.info(f"[UI] Dismissing permission prompt via {target['text']!r}")
        ctx.dispatcher.tap(target["x"], target["y"])
        return True

    if pkg:
        node, _ = find_by_chain(nodes, [{"text": _ONBOARDING_BUTTON_RE, "clickable": True}, {"desc": _ONBOARDING_BUTTON_RE, "clickable": True}], pkg=pkg)
        if node:
            logger.info(f"[UI] Dismissing first-run screen via {(node['text'] or node['content_desc'])!r}")
            ctx.dispatcher.tap(node["x"], node["y"])
            return True
    return False


def _wait_for(
    ctx: WorkflowContext,
    chain: list[dict],
    timeout: float | None = None,
    pkg: str | None = None,
    dismiss: bool = True,
) -> tuple[dict | None, list[dict]]:
    """Poll the UI hierarchy until a node matching `chain` appears (or timeout). Returns (node, last_nodes)."""
    deadline = time.time() + (config.UI_WAIT_TIMEOUT_S if timeout is None else timeout)
    nodes: list[dict] = []
    dismissals = 0
    while True:
        try:
            nodes = dump_nodes(ctx.device)
        except Exception as exc:  # uiautomator can hiccup mid-transition
            logger.debug(f"dump_nodes retry: {exc}")
            nodes = []
        node, _ = find_by_chain(nodes, chain, pkg=pkg)
        if node:
            return node, nodes
        if dismiss and nodes and dismissals < 5 and _dismiss_blockers(ctx, nodes, pkg):
            dismissals += 1
            time.sleep(1.0)
            continue
        if time.time() >= deadline:
            return None, nodes
        time.sleep(0.7)


def _tap(ctx: WorkflowContext, nodes: list[dict], node: dict) -> None:
    target = resolve_clickable(nodes, node)
    ctx.dispatcher.tap(target["x"], target["y"])


def _node_label(node: dict) -> str:
    return node["text"].strip() or node["content_desc"].strip() or node["resource_id"] or node["class"]


def _resolve_package(ctx: WorkflowContext, forced: str, candidates: list[str], what: str) -> str:
    installed = ctx.dispatcher.installed_packages()
    if forced:
        if forced not in installed:
            raise WorkflowError(f"{what} package {forced!r} (from config/.env) is not installed on this phone")
        return forced
    for candidate in candidates:
        if candidate in installed:
            return candidate
    raise WorkflowError(
        f"No known {what} app is installed. Run `python inspect_ui.py --packages`, then set "
        f"{what.upper()}_PACKAGE in .env to the right package name."
    )


def _launch(ctx: WorkflowContext, package: str, fresh: bool = True) -> None:
    """Start an app (cold start when `fresh`, so it opens on its home screen) and wait for it to be foreground."""
    try:
        ctx.device.app_start(package, stop=fresh, use_monkey=True)
    except Exception as exc:
        logger.warning(f"app_start({package}) raised {exc!r}; retrying via adb monkey")
        ctx.dispatcher.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
    deadline = time.time() + config.UI_WAIT_TIMEOUT_S
    while time.time() < deadline:
        if _current_package(ctx) == package:
            return
        time.sleep(0.6)
    logger.warning(f"{package} did not report as foreground within {config.UI_WAIT_TIMEOUT_S:.0f}s; continuing anyway")


# =============================================================================================
# Wi-Fi via the Control Panel (reuses main.py's validated quick-settings detection)
# =============================================================================================

_WIFI_TILE_PATTERN = r"wi-?fi|wlan"
_WIFI_TILE_CHAIN = [
    {"desc": _WIFI_TILE_PATTERN, "pkg": _SYSTEMUI},
    {"text": r"^(wi-?fi|wlan)$", "pkg": _SYSTEMUI},
    {"rid": r"wifi|wlan", "pkg": _SYSTEMUI},
    {"desc": _WIFI_TILE_PATTERN},
    {"text": r"^(wi-?fi|wlan)$"},
]


def _find_wifi_tile(ctx: WorkflowContext) -> tuple[int, int] | None:
    """Locate the Wi-Fi quick-settings tile. First the existing validated path (clickable elements via
    main._find_element), then the full-hierarchy chain with clickable-ancestor resolution."""
    try:
        match = ctx.find_element(ctx.capture_ui_state(ctx.device), _WIFI_TILE_PATTERN)
        if match:
            return match["coordinates"]["x"], match["coordinates"]["y"]
    except Exception as exc:
        logger.debug(f"existing tile finder failed, using full-hierarchy chain: {exc}")
    try:
        nodes = dump_nodes(ctx.device)
    except Exception:
        return None
    node, _ = find_by_chain(nodes, _WIFI_TILE_CHAIN)
    if node is None:
        return None
    target = resolve_clickable(nodes, node)
    return target["x"], target["y"]


def _open_control_panel(ctx: WorkflowContext) -> tuple[int, int]:
    """Open Quick Settings visibly (examiners watch it on scrcpy) and return the Wi-Fi tile centre.
    Tier 1: the swipe your project already uses; tier 2: d.open_quick_settings(); tier 3: statusbar service."""
    width, height = ctx.device.window_size()
    attempts = [
        ("swipe from top", lambda: ctx.dispatcher.swipe_down_from_top(width, height)),
        ("d.open_quick_settings()", lambda: ctx.device.open_quick_settings()),
        ("statusbar expand-settings", lambda: ctx.dispatcher.shell("cmd", "statusbar", "expand-settings")),
    ]
    for name, action in attempts:
        try:
            action()
        except Exception as exc:
            logger.debug(f"control panel via {name} failed: {exc}")
            continue
        time.sleep(1.5)
        tile = _find_wifi_tile(ctx)
        if tile:
            logger.info(f"[UI] Control Panel open via {name}; Wi-Fi tile at {tile}")
            return tile
    raise WorkflowError("Could not open the Control Panel / find the Wi-Fi tile (run `python inspect_ui.py --qs` to inspect it)")


def _close_control_panel(ctx: WorkflowContext) -> None:
    width, height = ctx.device.window_size()
    ctx.dispatcher.swipe_up_to_close(width, height)
    time.sleep(1.0)
    if _find_wifi_tile(ctx) is not None:  # panel still showing
        ctx.device.press("back")
        time.sleep(1.0)


def _wait_wifi_state(ctx: WorkflowContext, want: bool, timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ctx.dispatcher.wifi_enabled() == want:
            return True
        time.sleep(0.5)
    return ctx.dispatcher.wifi_enabled() == want


def _set_wifi_via_control_panel(ctx: WorkflowContext, enable: bool) -> None:
    label = "ON" if enable else "OFF"
    tile = _open_control_panel(ctx)
    time.sleep(1.0)  # let the examiners see the panel

    if ctx.dispatcher.wifi_enabled() == enable:
        logger.info(f"[WIFI] Already {label}; no tile tap needed")
    else:
        logger.info(f"[WIFI] Tapping Wi-Fi tile to turn {label}")
        ctx.dispatcher.tap(*tile)
        if not _wait_wifi_state(ctx, enable):
            logger.warning(f"[WIFI] Tile tap did not set Wi-Fi {label}; reinforcing with `svc wifi`")
            ctx.dispatcher.set_wifi(enable)
            if not _wait_wifi_state(ctx, enable):
                _close_control_panel(ctx)
                raise WorkflowError(f"Wi-Fi did not switch {label} (tile tap and svc fail-safe both failed)")
    logger.info(f"[WIFI] Wi-Fi is {label}")
    time.sleep(1.0)
    _close_control_panel(ctx)


# =============================================================================================
# Workflow A: "mirror"
# =============================================================================================

_CAMERA_ACTION = "android.media.action.STILL_IMAGE_CAMERA"
_FRONT_EXTRAS = [
    ("--ei", "android.intent.extras.CAMERA_FACING", "1"),
    ("--ez", "android.intent.extra.USE_FRONT_CAMERA", "true"),
    ("--ei", "android.intent.extras.LENS_FACING_FRONT", "1"),
]

_FLIP_VERB = r"(switch|flip|change|toggle|use|go|rotate|turn)"
_FLIP_WORD = r"(camera|cam|lens|front|rear|back|selfie|facing)"
_FLIP_CHAIN = [
    # 1. resource-id (most specific; every OEM names these differently, so match loosely)
    {"rid": r"(switch|flip|toggle|change|rotate|reverse).*(camera|cam|lens|facing)|(camera|cam|lens).*(switch|flip|toggle|change|reverse)|front_?back|camera_?switch|btn_?switch|switch_?btn|lens_?facing|selfie", "clickable": True},
    # 2. content-desc (what accessibility labels say: "Switch to front camera", "Flip camera", "Front camera")
    {"desc": rf"{_FLIP_VERB}.*{_FLIP_WORD}|{_FLIP_WORD}.*{_FLIP_VERB}|^(front|selfie) (camera|mode)$|^(rear|back) camera$", "clickable": True},
    # 3. visible text
    {"text": rf"{_FLIP_VERB}.*{_FLIP_WORD}|{_FLIP_WORD}.*{_FLIP_VERB}|^(front|selfie)$", "clickable": True},
    # 4. same patterns without the clickable filter (label on a non-clickable child; resolve_clickable() finds the parent)
    {"desc": rf"{_FLIP_VERB}.*{_FLIP_WORD}|{_FLIP_WORD}.*{_FLIP_VERB}"},
    {"text": rf"{_FLIP_VERB}.*{_FLIP_WORD}|{_FLIP_WORD}.*{_FLIP_VERB}"},
]


def _facing_from_label(label: str) -> str | None:
    """Infer the CURRENT lens from the flip button's accessibility label, which names the lens it will
    switch TO. "Switch to front camera" => currently rear. Returns None when the label is generic."""
    text = label.lower()
    wants_front = re.search(r"\b(to|use)\b.*\b(front|selfie)\b|\b(front|selfie)\b.*\b(switch|flip|change)\b", text)
    wants_rear = re.search(r"\b(to|use)\b.*\b(rear|back)\b|\b(rear|back)\b.*\b(switch|flip|change)\b", text)
    if wants_front and not wants_rear:
        return "rear"
    if wants_rear and not wants_front:
        return "front"
    return None


def _detect_facing(ctx: WorkflowContext, flip_node: dict | None, poll_s: float = 6.0) -> tuple[str | None, str]:
    """Which lens is live? (facing, evidence). Primary: `dumpsys media.camera` active camera ID
    (ground truth, polled while the camera HAL opens). Secondary: the flip button's label."""
    deadline = time.time() + poll_s
    while True:
        camera_id = ctx.dispatcher.active_camera_id()
        if camera_id is not None:
            facing = "front" if camera_id in config.FRONT_CAMERA_IDS else "rear"
            return facing, f"camera service reports active camera ID {camera_id}"
        if time.time() >= deadline:
            break
        time.sleep(0.7)

    if flip_node is not None:
        label = f"{flip_node['content_desc']} {flip_node['text']}".strip()
        facing = _facing_from_label(label)
        if facing:
            return facing, f"flip button label {label!r}"
    return None, "no camera-service data and the flip button label is generic"


def run_mirror(ctx: WorkflowContext, command: str = "mirror") -> bool:
    total = 3
    try:
        _step(1, total, "Local brain reasoning")
        log_brain_reasoning(command, "MIRROR")

        _step(2, total, "Opening the camera app (front-lens intent hint)")
        try:
            ctx.dispatcher.start_intent_with_extras(_CAMERA_ACTION, _FRONT_EXTRAS)
        except subprocess.CalledProcessError as exc:
            logger.warning(f"Camera intent failed ({exc}); launching by package instead")
            pkg = _resolve_package(ctx, config.CAMERA_PACKAGE, config.CAMERA_PACKAGE_CANDIDATES, "camera")
            _launch(ctx, pkg, fresh=False)
        _sleep(2.0)

        _step(3, total, "Verifying lens via UI hierarchy / camera service; flipping to the selfie camera if needed")
        flip_node, nodes = _wait_for(ctx, _FLIP_CHAIN, timeout=config.UI_WAIT_TIMEOUT_S)
        # A first-run permission/onboarding dialog can sit on top of the flip button -- clear it first.
        for _ in range(3):
            if not (nodes and _dismiss_blockers(ctx, nodes, None)):
                break
            _sleep(1.2)
            flip_node, nodes = _wait_for(ctx, _FLIP_CHAIN, timeout=config.UI_WAIT_TIMEOUT_S)
        if flip_node:
            logger.info(f"[PERCEPTION] Flip control found: {_node_label(flip_node)!r} ({flip_node['resource_id'] or 'no resource-id'})")
        else:
            logger.warning("[PERCEPTION] No flip control found in the hierarchy")

        facing, evidence = _detect_facing(ctx, flip_node)
        logger.info(f"[PERCEPTION] Active lens: {facing or 'unknown'} ({evidence})")

        if facing == "front":
            logger.info("[DONE] Camera is already on the front lens - mirror ready")
            return True

        if facing is None and config.MIRROR_UNKNOWN_FACING_ACTION == "skip":
            logger.warning("[DONE] Lens could not be verified; leaving the camera untouched (MIRROR_UNKNOWN_FACING_ACTION=skip)")
            return True

        if flip_node is None:
            raise WorkflowError("Camera is on the rear lens but no flip button was found. Run `python inspect_ui.py` on the camera screen.")

        for attempt in (1, 2):
            logger.info(f"[ACT] Tapping flip control {_node_label(flip_node)!r} (attempt {attempt})")
            _tap(ctx, nodes, flip_node)
            _sleep(1.8)
            nodes = dump_nodes(ctx.device)
            flip_node, _ = find_by_chain(nodes, _FLIP_CHAIN)   # label usually changes after a flip
            facing, evidence = _detect_facing(ctx, flip_node, poll_s=3.0)
            if facing != "rear" or flip_node is None:
                break

        if facing == "front":
            logger.info(f"[DONE] Switched to the front lens ({evidence}) - mirror ready")
            return True
        if facing is None:
            logger.warning("[DONE] Flip tapped, but the new lens could not be verified automatically - check the screen")
            return True
        raise WorkflowError("Flip button was tapped but the camera is still on the rear lens")
    except WorkflowError as exc:
        logger.error(f"[FAILED] mirror: {exc}")
        return False
    except Exception as exc:
        logger.exception(f"[FAILED] mirror crashed: {exc}")
        return False


# =============================================================================================
# Workflow B: "mail"
# =============================================================================================

_COMPOSE_CHAIN = [
    {"rid": r":id/compose_button$"},
    {"rid": r":id/compose|:id/fab", "clickable": True},
    {"desc": r"^compose$"},
    {"text": r"^compose$"},
    {"desc": r"compose"},
    {"text": r"compose"},
]
_TO_CHAIN = [
    {"rid": r":id/to$"},
    {"cls": r"EditText|AutoComplete|RecipientEdit", "desc": r"^to\b"},
    {"cls": r"EditText|AutoComplete|RecipientEdit", "text": r"^to$"},
    {"cls": r"EditText|AutoComplete|RecipientEdit", "rid": r":id/(to|recipient)"},
    {"rid": r"peoplekit_autocomplete"},                      # newer Gmail builds: the To chip input
    {"cls": r"EditText|AutoComplete|RecipientEdit", "rid": r"autocomplete|recipient"},
]
# Anything that only exists on the compose screen. Used to recognise "the composer is open" even when the
# To field itself has an id/label we do not know (Gmail changes these between versions).
_COMPOSER_EVIDENCE_CHAIN = [
    {"rid": r":id/(subject|wc_body|compose_body)$"},
    {"rid": r"composearea_tap_trap"},
    {"rid": r":id/send$"},
    {"cls": "EditText", "text": r"^(subject|compose email)$"},
    {"cls": "EditText", "desc": r"^(subject|compose email)$"},
]
_SUBJECT_CHAIN = [
    {"rid": r":id/subject$"},
    {"cls": r"EditText", "text": r"^subject$"},
    {"cls": r"EditText", "desc": r"^subject$"},
    {"cls": r"EditText", "rid": r"subject"},
]
_SEND_CHAIN = [
    {"rid": r":id/send$"},
    {"desc": r"^send$"},
    {"text": r"^send$", "clickable": True},
    {"rid": r"send", "clickable": True, "not_rid": r"feedback"},
]
_DIALOG_CONFIRM_CHAIN = [
    {"rid": r"android:id/button1$"},
    {"text": r"^(send|ok|yes)$", "clickable": True, "cls": r"Button"},
]


def _resolve_to(nodes: list[dict], pkg: str) -> dict | None:
    """The recipient field: the known chain, else the top-most EditText that is not Subject / Body / Cc / Bcc."""
    node, _ = find_by_chain(nodes, _TO_CHAIN, pkg=pkg)
    if node:
        return node
    edits = [
        n for n in nodes
        if n["package"] == pkg and "EditText" in n["class"]
        and not re.search(r"subject|body|cc|bcc|from", n["resource_id"], re.IGNORECASE)
        and not re.match(r"^(subject|compose email|cc|bcc|from)\b", (n["text"] or n["content_desc"]).strip(), re.IGNORECASE)
    ]
    return min(edits, key=lambda n: (n["bounds"][1], n["bounds"][0])) if edits else None


def _wait_composer(ctx: WorkflowContext, pkg: str, timeout: float) -> tuple[dict | None, list[dict]]:
    """Wait for the Gmail composer. Returns (node to treat as the To field, nodes); node is None if it never opened.
    If the composer is clearly open but the To field cannot be identified, the evidence node is returned and a
    UI dump is written to logs/last_failure_ui.txt so the selector can be fixed."""
    node, nodes = _wait_for(ctx, _TO_CHAIN + _COMPOSER_EVIDENCE_CHAIN, timeout=timeout, pkg=pkg)
    if node is None:
        return None, nodes
    to_node = _resolve_to(nodes, pkg)
    if to_node is None:
        logger.warning("[UI] Composer is open but the To field was not recognised; dumping the screen for diagnosis")
        _dump_debug(ctx, "Gmail composer open, To field not found")
        return node, nodes
    return to_node, nodes


def _dump_debug(ctx: WorkflowContext, reason: str) -> None:
    try:
        lines = [f"Re-Node workflow debug dump: {reason}"]
        for n in dump_nodes(ctx.device):
            lines.append(f"{'CLICK' if n['clickable'] else '     '} pkg={n['package']} rid={n['resource_id']!r} text={n['text']!r} "
                         f"desc={n['content_desc']!r} cls={n['class']} bounds={n['bounds']}")
        path = config.LOG_DIR / "last_failure_ui.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(f"[UI] Screen dump saved to {path}")
    except Exception as exc:  # noqa: BLE001 -- diagnostics only
        logger.debug(f"_dump_debug failed: {exc}")


def _find_body(nodes: list[dict], pkg: str) -> dict | None:
    """Gmail's message-body field, via a chain: known ids -> 'Compose email' hint -> largest non-header
    EditText -> the tap-trap that focuses the body."""
    node, _ = find_by_chain(nodes, [{"rid": r":id/(wc_body|body|compose_body)$"}, {"cls": "EditText", "text": r"^compose email$"}, {"cls": "EditText", "desc": r"compose email"}], pkg=pkg)
    if node:
        return node
    edits = [
        n for n in nodes
        if n["package"] == pkg and "EditText" in n["class"]
        and not re.search(r":id/(to|cc|bcc|from|subject)", n["resource_id"], re.IGNORECASE)
        and not re.fullmatch(r"(to|cc|bcc|from|subject)", n["text"].strip(), re.IGNORECASE)
        and not re.match(r"^(to|cc|bcc|from|subject)\b", n["content_desc"].strip(), re.IGNORECASE)
    ]
    if edits:
        return max(edits, key=lambda n: n["area"])
    node, _ = find_by_chain(nodes, [{"rid": r"composearea_tap_trap"}, {"rid": r"tap_trap"}], pkg=pkg)
    return node


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _text_on_screen(ctx: WorkflowContext, snippet: str, pkg: str | None = None) -> bool:
    target = _squash(snippet)
    if not target:
        return True
    try:
        nodes = dump_nodes(ctx.device)
    except Exception:
        return False
    return any(target in _squash(f"{n['text']} {n['content_desc']}") for n in nodes if not pkg or n["package"] == pkg)


def _inject_text(ctx: WorkflowContext, nodes: list[dict], field: dict, text: str, verify_snippet: str, pkg: str) -> None:
    """Focus `field` and put `text` into it. Order: accessibility set_text (exact, multi-line safe) ->
    FastInputIME send_keys -> clipboard paste (KEYCODE_PASTE). Each attempt is verified on screen."""
    attempts = [
        ("set_text", lambda: ctx.device(focused=True).set_text(text)),
        ("send_keys", lambda: ctx.device.send_keys(text, clear=True)),
        ("clipboard paste", lambda: (_set_clipboard(ctx, text), ctx.dispatcher.keyevent(279))),
    ]
    for name, action in attempts:
        _tap(ctx, nodes, field)
        time.sleep(0.8)
        try:
            action()
        except Exception as exc:
            logger.debug(f"{name} failed: {exc}")
            continue
        time.sleep(0.8)
        if _text_on_screen(ctx, verify_snippet, pkg):
            logger.info(f"[ACT] Text injected via {name}")
            return
        logger.warning(f"[ACT] {name} did not stick; trying next method")
    raise WorkflowError("Could not enter text into the Gmail field with any method")


def _set_clipboard(ctx: WorkflowContext, text: str) -> None:
    try:
        ctx.device.set_clipboard(text)
    except Exception:
        ctx.device.clipboard = text


def _first_line(text: str, limit: int = 60) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def run_mail(ctx: WorkflowContext, command: str = "mail") -> bool:
    total = 11
    state = {"wifi_needs_off": False}
    gmail = config.GMAIL_PACKAGE
    try:
        _step(1, total, "Local brain reasoning")
        log_brain_reasoning(command, "MAIL")
        if config.MAIL_DEFAULT_RECIPIENT == _PLACEHOLDER_RECIPIENT:
            logger.warning("[CONFIG] MAIL_DEFAULT_RECIPIENT is still the placeholder; the note must contain an address or the run will stop at step 7")

        # ---- 2. Notes -------------------------------------------------------------------
        _step(2, total, "Opening the Notes app")
        notes_pkg = _resolve_package(ctx, config.NOTES_PACKAGE, config.NOTES_PACKAGE_CANDIDATES, "notes")
        logger.info(f"[PERCEPTION] Notes app: {notes_pkg}")
        _launch(ctx, notes_pkg, fresh=True)
        _sleep(1.5)

        # ---- 3. Copy first note ----------------------------------------------------------
        _step(3, total, "Opening the first note and extracting its full text from the UI hierarchy")
        first = None
        deadline = time.time() + config.UI_WAIT_TIMEOUT_S
        while first is None and time.time() < deadline:
            nodes = dump_nodes(ctx.device)
            first = find_first_list_item(nodes, pkg=notes_pkg)
            if first is None:
                if not _dismiss_blockers(ctx, nodes, notes_pkg):
                    time.sleep(0.8)
                else:
                    time.sleep(1.0)
        if first is None:
            raise WorkflowError("Could not find any note in the Notes list (is it empty? run `python inspect_ui.py` on the list screen)")
        logger.info(f"[PERCEPTION] First note item at ({first['x']}, {first['y']})")
        ctx.dispatcher.tap(first["x"], first["y"])

        note_text = ""
        for _ in range(4):
            _sleep(1.2)
            note_text = extract_note_text(dump_nodes(ctx.device), pkg=notes_pkg)
            if note_text:
                break
        if not note_text:
            raise WorkflowError("The first note opened but no text could be read from it")
        _set_clipboard(ctx, note_text)
        logger.info("[NOTE] Copied note content (stored in Python variable + device clipboard):")
        for line in note_text.splitlines():
            logger.info(f"[NOTE]   | {line}")

        # ---- 4. Home ---------------------------------------------------------------------
        _step(4, total, "Pressing Home")
        ctx.device.press("home")
        _sleep(1.2)

        # ---- 5. Gmail --------------------------------------------------------------------
        _step(5, total, "Opening Gmail")
        if gmail not in ctx.dispatcher.installed_packages():
            raise WorkflowError(f"Gmail ({gmail}) is not installed on this phone")
        _launch(ctx, gmail, fresh=True)
        _sleep(1.5)

        # ---- 6. Compose ------------------------------------------------------------------
        _step(6, total, "Tapping Compose")
        to_node = None
        nodes = []
        for attempt in range(1, 4):
            compose, nodes = _wait_for(ctx, _COMPOSE_CHAIN, pkg=gmail)
            if compose is None:
                continue
            logger.info(f"[PERCEPTION] Compose button: {_node_label(compose)!r}")
            _tap(ctx, nodes, compose)
            to_node, nodes = _wait_composer(ctx, gmail, 6)
            if to_node:
                break
            logger.warning(f"[UI] Compose screen not detected after tap (attempt {attempt})")
        if to_node is None:
            logger.warning("[UI] Falling back to a mailto: intent to open the Gmail composer")
            ctx.dispatcher.shell("am", "start", "-a", "android.intent.action.SENDTO", "-d", "mailto:", "-p", gmail)
            to_node, nodes = _wait_composer(ctx, gmail, config.UI_WAIT_TIMEOUT_S)
        if to_node is None:
            _dump_debug(ctx, "Gmail composer did not open")
            raise WorkflowError("Gmail composer did not open (screen saved to logs/last_failure_ui.txt; or run `python inspect_ui.py --package com.google.android.gm`)")

        # ---- 7. Fill recipient / subject / body --------------------------------------------
        _step(7, total, "Filling recipient, subject and body")
        if _resolve_to(nodes, gmail) is None:
            _dump_debug(ctx, "composer open but To field not recognised")
            raise WorkflowError("Gmail composer is open but its To field was not recognised (screen saved to logs/last_failure_ui.txt)")
        found = _EMAIL_RE.search(note_text)
        recipient = found.group(0) if found else config.MAIL_DEFAULT_RECIPIENT
        if recipient == _PLACEHOLDER_RECIPIENT:
            raise WorkflowError("No recipient: set MAIL_DEFAULT_RECIPIENT in .env (or put an e-mail address inside the note)")
        logger.info(f"[ACT] Recipient: {recipient} ({'parsed from note' if found else 'config default'})")

        _tap(ctx, nodes, to_node)
        time.sleep(0.8)
        ctx.dispatcher.type_text(recipient)       # real keystrokes -> Gmail turns it into a recipient chip
        time.sleep(0.4)
        ctx.dispatcher.type_text(",")
        time.sleep(1.0)
        if not _text_on_screen(ctx, recipient, gmail):
            logger.warning("[ACT] Keystroke entry of recipient not visible; using set_text")
            nodes = dump_nodes(ctx.device)
            to_node = _resolve_to(nodes, gmail)
            if to_node is None:
                _dump_debug(ctx, "To field lost")
                raise WorkflowError("Lost the To field while entering the recipient")
            _inject_text(ctx, nodes, to_node, recipient + ",", recipient, gmail)

        nodes = dump_nodes(ctx.device)
        subject_node, _ = find_by_chain(nodes, _SUBJECT_CHAIN, pkg=gmail)
        subject = _first_line(note_text) if config.MAIL_SUBJECT_FROM_NOTE else ""
        subject = subject or config.MAIL_DEFAULT_SUBJECT
        if subject_node:
            _inject_text(ctx, nodes, subject_node, subject, subject, gmail)
        else:
            logger.warning("[UI] Subject field not found; Gmail may ask to send without a subject")

        nodes = dump_nodes(ctx.device)
        body = _find_body(nodes, gmail)
        if body is None:
            raise WorkflowError("Could not locate the Gmail body field")
        snippet = _squash(note_text)[:30]
        _inject_text(ctx, nodes, body, note_text, snippet, gmail)
        logger.info("[ACT] Note pasted into the message body")

        # ---- 8. Wi-Fi ON -----------------------------------------------------------------
        _step(8, total, "Opening the Control Panel and turning Wi-Fi ON")
        state["wifi_needs_off"] = True
        _set_wifi_via_control_panel(ctx, True)
        logger.info(f"[WIFI] Waiting {config.WIFI_CONNECT_WAIT_S:.0f}s for the connection to establish")
        time.sleep(config.WIFI_CONNECT_WAIT_S)
        if _current_package(ctx) != gmail:
            logger.warning("[UI] Gmail is not in the foreground after closing the panel; bringing it back")
            _launch(ctx, gmail, fresh=False)

        # ---- 9. Send ---------------------------------------------------------------------
        _step(9, total, "Sending the mail")
        send, nodes = _wait_for(ctx, _SEND_CHAIN, pkg=gmail, dismiss=False)
        if send is None:
            raise WorkflowError("Send button not found in Gmail")
        _tap(ctx, nodes, send)
        _sleep(1.2)
        confirm, nodes = _wait_for(ctx, _DIALOG_CONFIRM_CHAIN, timeout=2, dismiss=False)
        if confirm:  # e.g. "Send without subject / body?"
            logger.info(f"[UI] Confirming dialog via {_node_label(confirm)!r}")
            _tap(ctx, nodes, confirm)
        logger.info(f"[ACT] Send tapped; waiting {config.SEND_WAIT_S:.0f}s for the message to leave the Outbox")
        time.sleep(config.SEND_WAIT_S)
        still_composing, _ = _wait_for(ctx, _TO_CHAIN + _COMPOSER_EVIDENCE_CHAIN[:2], timeout=1, pkg=gmail, dismiss=False)
        if still_composing:
            logger.warning("[UI] The composer still seems to be open - the send may not have gone through")

        # ---- 10. Wi-Fi OFF ---------------------------------------------------------------
        _step(10, total, "Opening the Control Panel and turning Wi-Fi OFF (restoring the air-gap)")
        _set_wifi_via_control_panel(ctx, False)
        state["wifi_needs_off"] = False

        # ---- 11. Home --------------------------------------------------------------------
        _step(11, total, "Returning Home")
        ctx.device.press("home")
        logger.info("[DONE] mail workflow complete - note sent, Wi-Fi back OFF")
        return True

    except WorkflowError as exc:
        logger.error(f"[FAILED] mail: {exc}")
        return False
    except Exception as exc:
        logger.exception(f"[FAILED] mail crashed: {exc}")
        return False
    finally:
        if state["wifi_needs_off"]:
            logger.warning("[SAFETY] Workflow ended with Wi-Fi possibly ON - forcing it OFF to keep the air-gap")
            try:
                ctx.dispatcher.set_wifi(False)
            except Exception as exc:
                logger.error(f"[SAFETY] Could not force Wi-Fi OFF: {exc}")


# =============================================================================================
# Entry point used by main.py
# =============================================================================================

WORKFLOWS: dict[str, Callable[[WorkflowContext, str], bool]] = {
    "MIRROR": run_mirror,
    "MAIL": run_mail,
}


def run_workflow(name: str, ctx: WorkflowContext, command: str) -> bool:
    return WORKFLOWS[name](ctx, command)
