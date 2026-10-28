"""Test bootstrap: put the project root on sys.path and stub heavy/hardware-only third-party
modules (uiautomator2, easyocr) when they're not installed, so tests run on any machine."""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for name in ("uiautomator2", "easyocr"):
    try:
        __import__(name)
    except ImportError:
        module = types.ModuleType(name)
        module.connect = lambda *a, **k: None
        module.Reader = object
        module.Device = object
        sys.modules[name] = module
