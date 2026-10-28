"""Standalone UI inspector for Re-Node: prints every clickable element on the phone's CURRENT screen
(package, resource-id, text, content-desc, bounds) so you can verify package names / element IDs
in ~30 seconds. Needs only `uiautomator2` -- it does not import any other Re-Node module.

Usage (phone connected over USB, USB debugging on):
    python inspect_ui.py                    # clickable elements on the current screen
    python inspect_ui.py --all              # include non-clickable nodes (EditTexts, TextViews, containers)
    python inspect_ui.py --package com.google.android.gm   # only nodes from one app
    python inspect_ui.py --packages         # installed Notes / Camera / Gmail / Settings package names
    python inspect_ui.py --qs               # swipe open Quick Settings first, then dump it
    python inspect_ui.py --delay 5          # wait 5 s first so you can navigate to the screen to inspect
    python inspect_ui.py --serial ABC123    # pick a device when several are connected (or set ADB_SERIAL)

Tip: run it while sitting on (1) the camera screen, (2) the Notes list, (3) an open note, (4) the Gmail
composer. Whatever it prints for those four screens is exactly what the selector chains in
workflows.py need to match.
"""

import argparse
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import uiautomator2 as u2

HINT_RE = re.compile(r"note|memo|keep|camera|gm$|gmail|settings|clock|alarm", re.IGNORECASE)


def parse_bounds(bounds: str):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    return tuple(int(v) for v in m.groups()) if m else None


def shell_output(device, command: str) -> str:
    result = device.shell(command)
    return result.output if hasattr(result, "output") else (result[0] if isinstance(result, tuple) else str(result))


def print_packages(device) -> None:
    out = shell_output(device, "pm list packages")
    packages = sorted(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("package:"))
    print("\nInstalled packages that look like Notes / Camera / Gmail / Settings / Clock:")
    for pkg in packages:
        if HINT_RE.search(pkg) and "overlay" not in pkg and "provider" not in pkg:
            print(f"  {pkg}")
    print("\nPut the right ones in .env as NOTES_PACKAGE= / CAMERA_PACKAGE= if auto-detection ever picks wrongly.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="include non-clickable nodes")
    ap.add_argument("--package", help="only show nodes from this package")
    ap.add_argument("--packages", action="store_true", help="list installed notes/camera/gmail/settings packages and exit")
    ap.add_argument("--qs", action="store_true", help="open Quick Settings before dumping")
    ap.add_argument("--delay", type=float, default=0, help="seconds to wait before dumping")
    ap.add_argument("--serial", default=os.getenv("ADB_SERIAL", ""), help="device serial (default: the only connected device)")
    args = ap.parse_args()

    device = u2.connect(args.serial) if args.serial else u2.connect()
    info = device.info
    print(f"Device: {info.get('productName')}  {info.get('displayWidth')}x{info.get('displayHeight')}  Android SDK {info.get('sdkInt')}")

    if args.packages:
        print_packages(device)
        return 0

    if args.delay:
        print(f"Waiting {args.delay:g}s - navigate the phone to the screen you want to inspect...")
        time.sleep(args.delay)
    if args.qs:
        device.open_quick_settings()
        time.sleep(1.5)

    current = device.app_current()
    print(f"Foreground app: {current.get('package')}  /  {current.get('activity')}\n")

    root = ET.fromstring(device.dump_hierarchy())
    rows = []
    for node in root.iter("node"):
        clickable = node.get("clickable") == "true"
        if not (clickable or args.all):
            continue
        pkg = node.get("package", "")
        if args.package and pkg != args.package:
            continue
        rid, text, desc = node.get("resource-id", ""), node.get("text", ""), node.get("content-desc", "")
        if not args.all and not (rid or text.strip() or desc.strip()):
            continue
        b = parse_bounds(node.get("bounds", ""))
        rows.append((b[1] if b else 0, b[0] if b else 0, pkg, rid, text, desc, node.get("class", ""), clickable, node.get("bounds", "")))

    rows.sort()
    for _, _, pkg, rid, text, desc, cls, clickable, bounds in rows:
        flag = "CLICK" if clickable else "     "
        print(f"[{flag}] pkg={pkg}\n        resource-id={rid!r}\n        text={text!r}\n        content-desc={desc!r}\n        class={cls}  bounds={bounds}\n")
    print(f"{len(rows)} element(s) shown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
