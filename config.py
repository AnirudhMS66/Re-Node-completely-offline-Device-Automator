"""Central configuration for Re-Node. Override any value via a `.env` file."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# --- Device connection (ADB / uiautomator2) ---
ADB_SERIAL = os.getenv("ADB_SERIAL", "")  # e.g. "emulator-5554" or a device serial from `adb devices`
ADB_HOST = os.getenv("ADB_HOST", "127.0.0.1")
ADB_PORT = int(os.getenv("ADB_PORT", "5037"))

# --- Brain layer (Ollama-hosted quantized LLM) ---
OLLAMA_ENDPOINT = os.getenv("OLLAMA_ENDPOINT", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma:2b")  # or "phi3"
LLM_REQUEST_TIMEOUT_S = int(os.getenv("LLM_REQUEST_TIMEOUT_S", "60"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))

# --- Perception layer ---
OCR_LANGUAGES = ["en"]
OCR_GPU = os.getenv("OCR_GPU", "false").lower() == "true"
SCREEN_POLL_INTERVAL_S = float(os.getenv("SCREEN_POLL_INTERVAL_S", "1.0"))

# --- Actuator layer ---
TAP_DELAY_S = float(os.getenv("TAP_DELAY_S", "0.5"))
SWIPE_DURATION_MS = int(os.getenv("SWIPE_DURATION_MS", "300"))

# --- Flagship workflows: "mirror" and "mail" (additive; nothing above is changed) ---
# Gmail recipient used when the note itself contains no e-mail address.
# SET THIS (in .env or here) before the viva -- the placeholder below is rejected at runtime.
MAIL_DEFAULT_RECIPIENT = os.getenv("MAIL_DEFAULT_RECIPIENT", "your.recipient@example.com")
MAIL_DEFAULT_SUBJECT = os.getenv("MAIL_DEFAULT_SUBJECT", "Re-Node: note sent by offline edge agent")
# True -> subject = first line of the note (max 60 chars); False -> always MAIL_DEFAULT_SUBJECT.
MAIL_SUBJECT_FROM_NOTE = os.getenv("MAIL_SUBJECT_FROM_NOTE", "true").lower() == "true"

GMAIL_PACKAGE = os.getenv("GMAIL_PACKAGE", "com.google.android.gm")

# Leave NOTES_PACKAGE / CAMERA_PACKAGE empty to auto-detect from the candidate lists
# (first one that is installed wins). Run `python inspect_ui.py --packages` to see yours.
NOTES_PACKAGE = os.getenv("NOTES_PACKAGE", "")
NOTES_PACKAGE_CANDIDATES = [
    "com.oplus.note",                       # Realme / OnePlus / Oppo (ColorOS 12+)
    "com.coloros.note",                     # Realme / Oppo (older ColorOS)
    "com.miui.notes",                       # Xiaomi / Redmi / Poco (MIUI / HyperOS)
    "com.samsung.android.app.notes",        # Samsung One UI (Samsung Notes)
    "com.google.android.keep",              # Google Keep
    "com.android.notes",                    # Vivo / generic AOSP-style
    "com.bbk.notes",                        # Vivo (older)
    "com.vivo.notes",
    "com.huawei.notepad",                   # Huawei / Honor
    "com.example.android.notepad",          # Huawei (legacy) / AOSP sample
    "com.motorola.notes",
    "com.nothing.notes",
]

CAMERA_PACKAGE = os.getenv("CAMERA_PACKAGE", "")
CAMERA_PACKAGE_CANDIDATES = [
    "com.oplus.camera",                     # Realme / OnePlus / Oppo (ColorOS 12+)
    "com.oppo.camera",
    "com.android.camera",                   # Xiaomi (MIUI/HyperOS) / AOSP
    "com.android.camera2",
    "com.sec.android.app.camera",           # Samsung
    "com.google.android.GoogleCamera",      # Pixel / stock
    "com.huawei.camera",
    "com.motorola.camera3",
    "com.vivo.camera",
]

# Camera IDs that are the front (selfie) lens. Almost every phone uses "1".
FRONT_CAMERA_IDS = {c.strip() for c in os.getenv("FRONT_CAMERA_IDS", "1").split(",") if c.strip()}
# What to do if the lens facing can't be determined at all (no dumpsys data, ambiguous button label):
# "tap" = assume rear and tap the flip button once; "skip" = leave the camera as it is.
MIRROR_UNKNOWN_FACING_ACTION = os.getenv("MIRROR_UNKNOWN_FACING_ACTION", "tap").lower()

# Timing (seconds)
UI_WAIT_TIMEOUT_S = float(os.getenv("UI_WAIT_TIMEOUT_S", "12"))
WORKFLOW_STEP_PAUSE_S = float(os.getenv("WORKFLOW_STEP_PAUSE_S", "1.0"))
WIFI_CONNECT_WAIT_S = float(os.getenv("WIFI_CONNECT_WAIT_S", "5"))
SEND_WAIT_S = float(os.getenv("SEND_WAIT_S", "5"))

# Brain "cognitive log" call for the workflows (separate from the per-step action loop above).
BRAIN_REASONING_TIMEOUT_S = int(os.getenv("BRAIN_REASONING_TIMEOUT_S", "30"))
BRAIN_WARMUP = os.getenv("BRAIN_WARMUP", "true").lower() == "true"

# --- Timer / alarm control (additive; nothing above is changed) ---
# Leave CLOCK_PACKAGE empty to auto-detect from the candidate list (first installed wins).
# Run `python inspect_ui.py --packages` to see the clock apps installed on your phone.
CLOCK_PACKAGE = os.getenv("CLOCK_PACKAGE", "")
CLOCK_PACKAGE_CANDIDATES = [
    "com.coloros.alarmclock",               # Realme / Oppo (ColorOS)
    "com.oplus.alarmclock",                 # Realme / OnePlus / Oppo (ColorOS 12+)
    "com.sec.android.app.clockpackage",     # Samsung One UI
    "com.android.deskclock",                # AOSP / many OEMs
    "com.google.android.deskclock",         # Google Clock (Pixel / stock)
]
