"""The Execution Engine: dispatches low-level ADB shell commands via subprocess.

Bypasses uiautomator2 entirely so the agent can act even on devices/UIs where the
accessibility service is flaky, using raw `adb shell input ...` / `adb shell input keyevent ...`.
"""

import re
import subprocess

import config

# Common Android keyevent names -> keycodes (https://developer.android.com/reference/android/view/KeyEvent)
KEYCODES = {
    "HOME": 3,
    "BACK": 4,
    "ENTER": 66,
    "DEL": 67,
    "TAB": 61,
    "MENU": 82,
    "APP_SWITCH": 187,
}

# Characters that are meaningful to the device-side shell and must be escaped
# so `input text` receives them as literal characters (space included).
_SHELL_SPECIAL_CHARS = re.compile(r'([ &;|<>()$`"\'\\*?\[\]{}~!#^])')

# Quick-settings swipe gestures as fractions of screen height/width, so they
# scale correctly across different phones instead of using hardcoded pixels.
_SWIPE_DOWN_START_Y_FRAC = 0.01
_SWIPE_DOWN_END_Y_FRAC = 0.6
_SWIPE_UP_START_Y_FRAC = 0.95
_SWIPE_UP_END_Y_FRAC = 0.3


class AndroidDispatcher:
    def __init__(self, serial: str = config.ADB_SERIAL):
        self.serial = serial
        self._base_cmd = ["adb"] + (["-s", serial] if serial else [])

    def _run(self, *shell_args: str) -> subprocess.CompletedProcess:
        cmd = self._base_cmd + ["shell", *shell_args]
        return subprocess.run(cmd, check=True, capture_output=True, text=True)

    @staticmethod
    def _escape_text(text: str) -> str:
        """Escape spaces and shell metacharacters so the device shell treats them literally."""
        return _SHELL_SPECIAL_CHARS.sub(r"\\\1", text)

    def tap(self, x: int, y: int) -> None:
        self._run("input", "tap", str(x), str(y))

    def type_text(self, text: str) -> None:
        self._run("input", "text", self._escape_text(text))

    def swipe(self, sx: int, sy: int, ex: int, ey: int, duration_ms: int = config.SWIPE_DURATION_MS) -> None:
        self._run("input", "swipe", str(sx), str(sy), str(ex), str(ey), str(duration_ms))

    def swipe_down_from_top(self, screen_width: int, screen_height: int) -> None:
        """Open the notification/quick-settings panel."""
        cx = screen_width // 2
        start_y = int(screen_height * _SWIPE_DOWN_START_Y_FRAC)
        end_y = int(screen_height * _SWIPE_DOWN_END_Y_FRAC)
        self.swipe(cx, start_y, cx, end_y)

    def swipe_up_to_close(self, screen_width: int, screen_height: int) -> None:
        """Close the notification/quick-settings panel."""
        cx = screen_width // 2
        start_y = int(screen_height * _SWIPE_UP_START_Y_FRAC)
        end_y = int(screen_height * _SWIPE_UP_END_Y_FRAC)
        self.swipe(cx, start_y, cx, end_y)

    def start_intent(self, intent_action: str) -> None:
        self._run("am", "start", "-a", intent_action)

    def keyevent(self, keycode: str | int) -> None:
        code = KEYCODES[keycode.upper()] if isinstance(keycode, str) else keycode
        self._run("input", "keyevent", str(code))

    # --- Additions for the "mirror" / "mail" workflows (existing methods above are untouched) ---

    def shell(self, *shell_args: str) -> str:
        """Run an arbitrary `adb shell ...` command and return its stdout."""
        return self._run(*shell_args).stdout

    def start_intent_with_extras(self, intent_action: str, extras: list[tuple[str, str, str]]) -> None:
        """`am start -a <action>` plus typed extras, e.g. ("--ei", "android.intent.extras.CAMERA_FACING", "1")."""
        args = ["am", "start", "-a", intent_action]
        for flag, key, value in extras:
            args += [flag, key, str(value)]
        self._run(*args)

    def installed_packages(self) -> set[str]:
        out = self.shell("pm", "list", "packages")
        return {line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("package:")}

    def wifi_enabled(self) -> bool | None:
        """Ground-truth Wi-Fi state from Settings.Global.wifi_on (1/2 = on, 0/3 = off). None if unreadable."""
        try:
            value = self.shell("settings", "get", "global", "wifi_on").strip()
        except subprocess.CalledProcessError:
            return None
        if value in ("1", "2"):
            return True
        if value in ("0", "3"):
            return False
        return None

    def set_wifi(self, enable: bool) -> None:
        """Fail-safe Wi-Fi switch: `svc wifi enable|disable`, falling back to `cmd wifi set-wifi-enabled`."""
        word = "enable" if enable else "disable"
        try:
            self._run("svc", "wifi", word)
        except subprocess.CalledProcessError:
            self._run("cmd", "wifi", "set-wifi-enabled", "enabled" if enable else "disabled")

    def active_camera_id(self) -> str | None:
        """Camera ID currently opened by an app, parsed from `dumpsys media.camera`. None if no app
        holds a camera or the dump format isn't recognised."""
        try:
            dump = self.shell("dumpsys", "media.camera")
        except subprocess.CalledProcessError:
            return None
        marker = dump.find("Active Camera Clients")
        if marker == -1:
            return None
        match = re.search(r"Camera ID:\s*(\d+)", dump[marker : marker + 800])
        return match.group(1) if match else None
