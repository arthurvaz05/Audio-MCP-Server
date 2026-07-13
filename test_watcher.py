"""Locks the no-speech guard: silent recordings must never reach the ata/email.
Also locks the long-recording nudge (forgotten-open Teams window, 2026-07-13).
Run: .venv/bin/python3 test_watcher.py"""
import tempfile
from pathlib import Path

import watcher
from watcher import has_speech, wait_for_recording

# Real transcript of a false-positive recording (2026-07-08 20:22, 2 min):
# Teams opened media sockets while gathering ICE candidates, nobody spoke.
SILENT = """\
[00:00] Abertura
[00:30] Abertura
[01:00] Abertura
[02:00] Abertura
[02:02] Fim
[02:04]
[02:04] O que é isso?
[02:06]
[02:08] Fim
"""

# Real mic test (2026-07-07 12:34) — one voice, a few words, no meeting.
MIC_TEST = """\
[00:00] Alô, teste, prancha amarela, bike verde, futebol.
[00:30] Alô, teste, prancha amarela, bike verde, futebol.
"""

# Shape of a real meeting: many distinct words (measured 252-1368 unique).
REAL = "\n".join(f"[00:{i:02d}] palavra{i} distinta{i} conversa{i}" for i in range(20))


def check(text: str, expected: bool, label: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8",
                                     delete=False) as f:
        f.write(text)
    try:
        assert has_speech(f.name) is expected, label
    finally:
        Path(f.name).unlink()


check("", False, "empty transcript")
check(SILENT, False, "whisper filler on silence is not speech")
check(MIC_TEST, False, "mic test is not a meeting")
check(REAL, True, "real meeting has speech")


class FakeRec:
    """Recorder stub: each join() advances the clock; done at total_seconds."""

    def __init__(self, total_seconds, step=600):
        self.seconds = 0
        self.total = total_seconds
        self.step = step

    def status(self):
        return {"state": "done" if self.seconds >= self.total else "recording",
                "seconds": min(self.seconds, self.total)}

    def join(self, timeout=None):
        self.seconds += self.step


def run_wait(total_seconds):
    notifications = []
    original = watcher.notify
    watcher.notify = lambda *a, **k: notifications.append(a)
    try:
        wait_for_recording(FakeRec(total_seconds))
    finally:
        watcher.notify = original
    return notifications


short = run_wait(30 * 60)
assert short == [], "short recording must not nudge"

long_run = run_wait(watcher.LONG_RECORDING_ALERT_SECONDS + 3600)
assert len(long_run) == 1, f"long recording must nudge exactly once, got {long_run}"
assert "saia da call" in long_run[0][0], "nudge must tell the user to leave the call"

print("all watcher tests passed")
