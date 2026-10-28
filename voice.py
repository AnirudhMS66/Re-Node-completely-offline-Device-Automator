"""Basic offline voice input for the control center (Windows only, no extra packages).

The GUI's "Mic" button calls only these two functions:

    is_available() -> bool                         # False keeps the Mic button disabled
    listen_once(timeout: float = 6.0) -> str | None
                                                   # one utterance -> recognised text (None = nothing heard)

How: it asks Windows' built-in speech recognizer (System.Speech / SAPI, shipped with Windows, works with no
internet) through PowerShell. Standard library only, nothing to pip install. The recognised text goes into the
command box and runs exactly like a typed command, so "set timer for five minutes" or "alarm at 7 30 am" use the
same parser as typing.

Needs: Windows, a working microphone set as the default recording device, and an installed speech recognizer
(Settings > Time & language > Speech; English is installed on most PCs). It listens for a fixed list of commands
(PHRASES, "set timer for <n> minutes|seconds", "set alarm for <hour> [fifteen|thirty|forty five] a m|p m") rather than
free dictation, and ignores anything it is less than MIN_CONFIDENCE sure about. Speak clearly, short commands.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
import sys
import threading

DEFAULT_TIMEOUT_S = 6.0


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def is_available() -> bool:
    return sys.platform.startswith("win") and _powershell() is not None


_ONES = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen",
         "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["twenty", "thirty", "forty", "fifty"]

# Commands the recognizer is allowed to hear. Restricting it to these (instead of free dictation) is what makes
# short words like "wifi" or "mail" reliable. Add phrases here if you add buttons.
PHRASES = [
    "turn on wifi", "turn off wifi", "wifi", "turn on bluetooth", "turn off bluetooth", "bluetooth",
    "turn on flashlight", "turn off flashlight", "flashlight", "mirror", "mail", "airplane mode", "quick settings",
    "go home", "go back", "stop timer", "show timers", "show alarms", "cancel alarm", "cancel all alarms",
]
MIN_CONFIDENCE = 0.6


def number_words(upto: int = 59) -> list[str]:
    words = list(_ONES[:min(upto, 19)])
    for t_i, tens in enumerate(_TENS, start=2):
        if t_i * 10 > upto:
            break
        words.append(tens)
        words += [f"{tens} {one}" for one in _ONES[:9] if t_i * 10 + _ONES.index(one) + 1 <= upto]
    return words


def _ps_list(items: list[str]) -> str:
    return "@(" + ",".join("'" + i.replace("'", "") + "'" for i in items) + ")"


def build_script(timeout: float) -> str:
    seconds = max(2, int(round(timeout)))
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "Add-Type -AssemblyName System.Speech\n"
        "$S = 'System.Speech.Recognition.'\n"
        "function Ch($items) { $c = New-Object ($S + 'Choices'); $c.Add([string[]]$items); return $c }\n"
        "function Gb($parts) { $g = New-Object ($S + 'GrammarBuilder'); foreach ($p in $parts) { $g.Append($p) }; return $g }\n"
        f"$num = Ch {_ps_list(number_words())}\n"
        "$unit = Ch @('seconds','second','minutes','minute')\n"
        f"$hour = Ch {_ps_list(_ONES[:12])}\n"
        "$mins = Ch @('fifteen','thirty','forty five')\n"
        "$ampm = Ch @('a m','p m')\n"
        "$alarm = New-Object ($S + 'GrammarBuilder'); $alarm.Append('set alarm for'); $alarm.Append($hour)\n"
        "$alarm.Append($mins, 0, 1); $alarm.Append($ampm)\n"
        "$all = New-Object ($S + 'Choices')\n"
        f"$all.Add([string[]]{_ps_list(PHRASES)})\n"
        "$all.Add((Gb @('set timer for', $num, $unit)))\n"
        "$all.Add($alarm)\n"
        "$r = New-Object ($S + 'SpeechRecognitionEngine')\n"
        "$r.SetInputToDefaultAudioDevice()\n"
        "$r.LoadGrammar((New-Object ($S + 'Grammar') (New-Object ($S + 'GrammarBuilder') $all)))\n"
        f"$r.InitialSilenceTimeout = [TimeSpan]::FromSeconds({seconds})\n"
        "[Console]::Out.WriteLine('READY'); [Console]::Out.Flush()\n"
        f"$res = $r.Recognize([TimeSpan]::FromSeconds({seconds}))\n"
        "if ($res -ne $null) { [Console]::Out.WriteLine(('RESULT|{0:N2}|{1}' -f $res.Confidence, $res.Text)) } else { [Console]::Out.WriteLine('NONE') }\n"
    )


def _encode(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def clean(text: str | None) -> str | None:
    """Tidy recognizer output: collapse spaces; '' -> None."""
    text = " ".join((text or "").split())
    text = re.sub(r"\b([ap]) m\b", r"\1m", text)          # "seven a m" -> "seven am" (what the clock parser expects)
    return text or None


def split_result(line: str) -> tuple[float, str] | None:
    """'RESULT|0.87|set timer for five minutes' -> (0.87, text); anything else -> None."""
    parts = (line or "").strip().split("|", 2)
    if len(parts) != 3 or parts[0] != "RESULT":
        return None
    try:
        return float(parts[1].replace(",", ".")), parts[2]
    except ValueError:
        return None


last_detail = ""       # what the last listen_once() heard / why it returned nothing (shown in the GUI log)


def listen_once(timeout: float = DEFAULT_TIMEOUT_S, on_ready=None) -> str | None:
    """Block for up to `timeout` seconds and return what was said (None = nothing understood; see `last_detail`).
    `on_ready()` is called the moment the microphone is actually listening (starting PowerShell takes a few
    seconds, speak after that). Raises RuntimeError with a readable message if voice cannot work on this PC."""
    global last_detail
    last_detail = ""
    exe = _powershell()
    if not sys.platform.startswith("win") or exe is None:
        raise RuntimeError("Voice input needs Windows with PowerShell")
    cmd = [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", _encode(build_script(timeout))]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(timeout + 25, proc.kill)
    watchdog.start()
    result_line = ""
    try:
        for line in proc.stdout:
            line = line.strip()
            if line == "READY":
                if on_ready:
                    on_ready()
            elif line.startswith("RESULT|") or line == "NONE":
                result_line = line
        err = proc.stderr.read()
        code = proc.wait()
    finally:
        watchdog.cancel()
    if code != 0 and not result_line:
        detail = (err or "").strip().splitlines()
        hint = detail[0] if detail else f"exit code {code}"
        raise RuntimeError(f"Windows speech recognition failed: {hint[:200]} "
                           "(check the microphone and Settings > Time & language > Speech)")
    parsed = split_result(result_line)
    if parsed is None:
        last_detail = "nothing recognised (no matching command heard, or too quiet / too late)"
        return None
    confidence, text = parsed
    if confidence < MIN_CONFIDENCE:
        last_detail = f"heard {text!r} but only {confidence:.0%} sure (needs {MIN_CONFIDENCE:.0%}); ignored"
        return None
    last_detail = f"heard {text!r} ({confidence:.0%} sure)"
    return clean(text)


def build_diag_script(seconds: int = 10) -> str:
    return (
        "Add-Type -AssemblyName System.Speech\n"
        "$S = 'System.Speech.Recognition.'\n"
        "$rec = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers() | ForEach-Object { $_.Culture.Name }\n"
        "Write-Output ('RECOGNIZERS: ' + ($rec -join ', '))\n"
        "$r = New-Object ($S + 'SpeechRecognitionEngine')\n"
        "try { $r.SetInputToDefaultAudioDevice() } catch { Write-Output ('NO_MIC: ' + $_.Exception.Message); exit 3 }\n"
        "$r.LoadGrammar((New-Object ($S + 'DictationGrammar')))\n"
        "Register-ObjectEvent $r AudioLevelUpdated -SourceIdentifier lvl | Out-Null\n"
        "Register-ObjectEvent $r SpeechRecognized -SourceIdentifier ok | Out-Null\n"
        "Register-ObjectEvent $r SpeechRecognitionRejected -SourceIdentifier rej | Out-Null\n"
        "Register-ObjectEvent $r SpeechHypothesized -SourceIdentifier hyp | Out-Null\n"
        "$r.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple)\n"
        "[Console]::Out.WriteLine('READY'); [Console]::Out.Flush()\n"
        f"$end = (Get-Date).AddSeconds({int(seconds)}); $max = 0\n"
        "while ((Get-Date) -lt $end) {\n"
        "  foreach ($e in @(Get-Event)) {\n"
        "    $a = $e.SourceEventArgs\n"
        "    switch ($e.SourceIdentifier) {\n"
        "      'lvl' { if ($a.AudioLevel -gt $max) { $max = $a.AudioLevel } }\n"
        "      'ok'  { [Console]::Out.WriteLine(('HEARD: {0}  (confidence {1:N2})' -f $a.Result.Text, $a.Result.Confidence)) }\n"
        "      'rej' { [Console]::Out.WriteLine(('REJECTED: {0}' -f $a.Result.Text)) }\n"
        "      'hyp' { [Console]::Out.WriteLine(('PARTIAL: {0}' -f $a.Result.Text)) }\n"
        "    }\n"
        "    Remove-Event -EventIdentifier $e.EventIdentifier\n"
        "  }\n"
        "  Start-Sleep -Milliseconds 100\n"
        "}\n"
        "[Console]::Out.WriteLine('MAX_AUDIO_LEVEL: ' + $max)\n"
        "$r.RecognizeAsyncCancel()\n"
    )


def diagnose(seconds: int = 10) -> int:
    """`python voice.py --diag`: free dictation for `seconds`, printing every partial / heard / rejected phrase and
    the loudest microphone level seen. Tells apart: no microphone, microphone silent, speech not understood."""
    exe = _powershell()
    if not is_available() or exe is None:
        print("Voice needs Windows + PowerShell.")
        return 1
    print(f"Diagnosing for {seconds} s. Wait for LISTENING, then speak normally: 'turn on wifi', 'set timer for five minutes'...")
    proc = subprocess.Popen([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", _encode(build_diag_script(seconds))],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    max_level, heard = -1, 0
    for raw in proc.stdout:
        line = raw.strip()
        if line == "READY":
            print(">>> LISTENING NOW")
            continue
        print(line)
        if line.startswith("MAX_AUDIO_LEVEL:"):
            try:
                max_level = int(float(line.split(":", 1)[1]))
            except ValueError:
                pass
        if line.startswith(("HEARD:", "PARTIAL:", "REJECTED:")):
            heard += 1
    err = (proc.stderr.read() or "").strip()
    proc.wait()
    if err:
        print("POWERSHELL ERROR:", err.splitlines()[0][:300])
    print("\n--- verdict ---")
    if max_level == 0:
        print("The microphone level stayed at 0: Windows is not receiving sound. Check Settings > System > Sound > Input "
              "(pick the right mic, raise the volume, unmute) and Privacy > Microphone (allow desktop apps).")
    elif max_level > 0 and heard == 0:
        print(f"Sound arrived (peak level {max_level}) but nothing was recognised: the speech language/recognizer is the "
              "problem. Settings > Time & language > Speech: install English and set 'Speech language'.")
    elif heard:
        print("Recognition works. If 'turn on wifi' came out wrong here, say it more slowly; the Mic button only accepts its fixed phrases.")
    else:
        print("No result. Copy everything printed above and send it.")
    return 0


def self_test() -> int:
    """`python voice.py` -> prints what the recognizer does, for diagnosing a PC."""
    print("available:", is_available())
    if not is_available():
        print("Voice needs Windows + PowerShell.")
        return 1
    print("Starting the recognizer (a few seconds)...")
    try:
        text = listen_once(8, on_ready=lambda: print(">>> LISTENING NOW: say e.g. 'turn on wifi' or 'set timer for five minutes'"))
    except Exception as exc:  # noqa: BLE001
        print("ERROR:", exc)
        return 2
    print("result:", repr(text), "|", last_detail)
    return 0


if __name__ == "__main__":
    sys.exit(diagnose() if "--diag" in sys.argv else self_test())
