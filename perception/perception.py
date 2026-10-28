"""The Perception Engine: turns a raw Android screen into a clean, LLM-ready JSON structure.

Primary path: parse the uiautomator2 XML view hierarchy for clickable elements.
Fallback path: OCR the screenshot for text/bounding boxes when the XML is unreliable
(e.g. custom WebViews, canvas-rendered UIs, games, or system overlays some OEMs
hide from the accessibility tree — notably the quick-settings panel on some
devices).
"""

import re
import xml.etree.ElementTree as ET

import easyocr
import uiautomator2 as u2

import config

_ocr_reader = None

# Caps how many elements/chars get sent to the LLM. Small local models (gemma:2b,
# phi3) perform noticeably worse and slower with large/noisy JSON blobs, so we
# keep only elements with visible text or a resource id, and cap the count.
_MAX_ELEMENTS_FOR_BRAIN = 40


def connect_device(serial: str = config.ADB_SERIAL) -> u2.Device:
    return u2.connect(serial) if serial else u2.connect()


def get_current_context(device) -> dict:
    """Snapshot of what app/screen the device is currently in.

    Lets the brain ground its decisions in "I am in com.android.settings" rather
    than reasoning from the element list alone. Also doubles as a quick
    environment check after connecting — call this once right after
    connect_device() to confirm the device is reachable and log what's on screen.
    """
    try:
        current = device.app_current()
        width, height = device.window_size()
        return {
            "package": current.get("package"),
            "activity": current.get("activity"),
            "screen_width": width,
            "screen_height": height,
        }
    except Exception as exc:  # device.app_current() can fail on some system overlays
        return {"package": None, "activity": None, "error": str(exc)}


def _parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    """Parse uiautomator2's "[x1,y1][x2,y2]" bounds string into (x1, y1, x2, y2)."""
    try:
        left_str, right_str = bounds.split("][")
        x1, y1 = (int(v) for v in left_str.lstrip("[").split(","))
        x2, y2 = (int(v) for v in right_str.rstrip("]").split(","))
        return x1, y1, x2, y2
    except (ValueError, AttributeError):
        return None


def get_screen_hierarchy(device) -> dict:
    """Dump the current screen's XML hierarchy and extract clickable elements.

    Returns:
        {"source": "xml", "elements": [{"text", "resource_id", "class", "coordinates": {"x", "y"}}]}
    """
    xml_dump = device.dump_hierarchy()
    root = ET.fromstring(xml_dump)

    elements = []
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue

        bounds = _parse_bounds(node.get("bounds", ""))
        if bounds is None:
            continue
        x1, y1, x2, y2 = bounds

        elements.append(
            {
                "text": node.get("text", "").strip(),
                "content_desc": node.get("content-desc", "").strip(),
                "resource_id": node.get("resource-id", "").strip(),
                "class": node.get("class", "").strip(),
                "coordinates": {"x": (x1 + x2) // 2, "y": (y1 + y2) // 2},
            }
        )

    return {"source": "xml", "elements": _trim_for_brain(elements)}


def extract_visual_coordinates(image_path: str) -> dict:
    """OCR a screenshot for text and bounding-box centers (fallback for non-native UI).

    Returns:
        {"source": "ocr", "elements": [{"text", "confidence", "coordinates": {"x", "y"}}]}
    """
    results = _get_ocr_reader().readtext(image_path)

    elements = []
    for bbox, text, confidence in results:
        xs = [point[0] for point in bbox]
        ys = [point[1] for point in bbox]
        elements.append(
            {
                "text": text.strip(),
                "confidence": round(float(confidence), 4),
                "coordinates": {"x": int(sum(xs) / len(xs)), "y": int(sum(ys) / len(ys))},
            }
        )

    return {"source": "ocr", "elements": _trim_for_brain(elements)}


def _trim_for_brain(elements: list[dict]) -> list[dict]:
    """Drop elements with no text/desc/id (nothing for the LLM to reason about) and cap the list."""
    useful = [e for e in elements if e.get("text") or e.get("content_desc") or e.get("resource_id")]
    return useful[:_MAX_ELEMENTS_FOR_BRAIN]


def _get_ocr_reader() -> easyocr.Reader:
    global _ocr_reader
    if _ocr_reader is None:
        _ocr_reader = easyocr.Reader(config.OCR_LANGUAGES, gpu=config.OCR_GPU)
    return _ocr_reader

# --- Additions for the "mirror" / "mail" workflows (everything above is untouched) -------
#
# The functions above only expose *clickable* nodes, trimmed for the LLM. The workflows need
# the full tree (EditTexts, plain TextViews holding note text, container structure), so this
# section adds an untrimmed dump plus a multi-selector fallback chain that works across
# MIUI / ColorOS / One UI / stock Android.


def dump_nodes(device) -> list[dict]:
    """Full, untrimmed hierarchy as a flat list of dicts (document order), each carrying its
    parent's list index so callers can walk up to a clickable ancestor."""
    root = ET.fromstring(device.dump_hierarchy())
    nodes: list[dict] = []

    def walk(xml_node, parent_idx: int | None, depth: int) -> None:
        idx = parent_idx
        if xml_node.tag == "node":
            bounds = _parse_bounds(xml_node.get("bounds", ""))
            if bounds is not None:
                x1, y1, x2, y2 = bounds
                idx = len(nodes)
                nodes.append(
                    {
                        "index": idx,
                        "parent": parent_idx,
                        "depth": depth,
                        "text": xml_node.get("text", ""),
                        "content_desc": xml_node.get("content-desc", ""),
                        "resource_id": xml_node.get("resource-id", ""),
                        "class": xml_node.get("class", ""),
                        "package": xml_node.get("package", ""),
                        "clickable": xml_node.get("clickable") == "true",
                        "checked": xml_node.get("checked") == "true",
                        "enabled": xml_node.get("enabled") != "false",
                        "focused": xml_node.get("focused") == "true",
                        "bounds": bounds,
                        "x": (x1 + x2) // 2,
                        "y": (y1 + y2) // 2,
                        "area": max(0, x2 - x1) * max(0, y2 - y1),
                    }
                )
        for child in xml_node:
            walk(child, idx, depth + 1)

    walk(root, None, 0)
    return nodes


def match_node(node: dict, selector: dict) -> bool:
    """AND-match one selector against a node. Selector keys (all optional, case-insensitive regex
    via re.search): rid, text, desc, label (text OR desc), cls, pkg, not_rid; plus clickable (bool)."""
    for key, field in (
        ("rid", "resource_id"),
        ("text", "text"),
        ("desc", "content_desc"),
        ("cls", "class"),
        ("pkg", "package"),
    ):
        pattern = selector.get(key)
        if pattern and not re.search(pattern, node[field].strip(), re.IGNORECASE):
            return False
    label = selector.get("label")
    if label and not re.search(label, f"{node['text']} {node['content_desc']}".strip(), re.IGNORECASE):
        return False
    not_rid = selector.get("not_rid")
    if not_rid and re.search(not_rid, node["resource_id"], re.IGNORECASE):
        return False
    if "clickable" in selector and node["clickable"] != selector["clickable"]:
        return False
    return True


def find_by_chain(nodes: list[dict], chain: list[dict], pkg: str | None = None) -> tuple[dict | None, dict | None]:
    """Try each selector in `chain` in order; return (first matching node, the selector that hit).
    Earlier selectors are more specific (resource-id) and later ones more generic (text/desc), so
    the same chain survives OEM differences. `pkg` restricts matching to one app's nodes."""
    pool = [n for n in nodes if not pkg or n["package"] == pkg]
    for selector in chain:
        for node in pool:
            if match_node(node, selector):
                return node, selector
    return None, None


def resolve_clickable(nodes: list[dict], node: dict) -> dict:
    """The node itself if clickable, else its nearest clickable ancestor, else the smallest clickable
    node whose bounds contain its centre, else the node. (OEM quick-settings tiles often put the label
    on a non-clickable child of the real tile.)"""
    if node["clickable"]:
        return node
    parent = node["parent"]
    while parent is not None:
        candidate = nodes[parent]
        if candidate["clickable"]:
            return candidate
        parent = candidate["parent"]
    containing = [
        n for n in nodes
        if n["clickable"] and n["bounds"][0] <= node["x"] <= n["bounds"][2] and n["bounds"][1] <= node["y"] <= n["bounds"][3]
    ]
    return min(containing, key=lambda n: n["area"]) if containing else node


_NOTE_TEXT_SKIP_RID = re.compile(r"toolbar|status|battery|clock|date|time|count|tab|menu|title_bar|action_bar", re.IGNORECASE)


def extract_note_text(nodes: list[dict], pkg: str | None = None) -> str:
    """Full text of the note currently open, read straight from the hierarchy (no touch gestures).
    Preference: non-empty EditText nodes (editable note body, holds the complete string even when it
    scrolls off-screen); fallback: non-clickable TextViews in reading order."""
    pool = [n for n in nodes if (not pkg or n["package"] == pkg) and n["package"] != "com.android.systemui"]

    edits = [n for n in pool if "EditText" in n["class"] and n["text"].strip()]
    if edits:
        edits.sort(key=lambda n: (n["bounds"][1], n["bounds"][0]))
        return "\n".join(n["text"].replace("\u200b", "").rstrip() for n in edits).strip()

    views = [
        n for n in pool
        if n["class"].endswith("TextView") and not n["clickable"] and n["text"].strip()
        and not _NOTE_TEXT_SKIP_RID.search(n["resource_id"])
    ]
    views.sort(key=lambda n: (n["bounds"][1], n["bounds"][0]))
    return "\n".join(n["text"].replace("\u200b", "").rstrip() for n in views).strip()


_LIST_CONTAINER_CLS = re.compile(r"RecyclerView|ListView|GridView|ScrollView|ViewPager", re.IGNORECASE)
_NOT_A_NOTE_RE = re.compile(
    r"\b(add|new|create|compose|fab|search|menu|more|settings?|back|navigate|sort|select|folder|category|all notes|sync|cloud|login|sign)\b",
    re.IGNORECASE,
)


def find_first_list_item(nodes: list[dict], pkg: str | None = None) -> dict | None:
    """The first (top-left-most) item of the main scrolling list/grid of an app's home screen --
    i.e. the first note. Strategy 1: children of the largest RecyclerView/ListView/GridView that
    contain text. Strategy 2 (custom UIs): the top-most clickable text-bearing node that isn't
    obviously a button (add/search/menu/...)."""
    pool = [n for n in nodes if (not pkg or n["package"] == pkg) and n["package"] != "com.android.systemui"]

    def has_text_below(root_idx: int) -> bool:
        stack = [root_idx]
        while stack:
            cur = stack.pop()
            if nodes[cur]["text"].strip() or nodes[cur]["content_desc"].strip():
                return True
            stack.extend(n["index"] for n in nodes if n["parent"] == cur)
        return False

    containers = sorted(
        (n for n in pool if _LIST_CONTAINER_CLS.search(n["class"]) and n["area"] > 0),
        key=lambda n: n["area"],
        reverse=True,
    )
    for container in containers:
        items = [
            n for n in pool
            if n["parent"] == container["index"] and n["area"] > 0 and has_text_below(n["index"])
        ]
        items.sort(key=lambda n: (n["bounds"][1], n["bounds"][0]))
        if items:
            return resolve_clickable(nodes, items[0])

    candidates = [
        n for n in pool
        if n["clickable"] and (n["text"].strip() or has_text_below(n["index"]))
        and not _NOT_A_NOTE_RE.search(f"{n['text']} {n['content_desc']} {n['resource_id']}")
    ]
    candidates.sort(key=lambda n: (n["bounds"][1], n["bounds"][0]))
    return candidates[0] if candidates else None
