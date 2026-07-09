"""Locks the no-speech guard: silent recordings must never reach the ata/email.
Run: .venv/bin/python3 test_watcher.py"""
import tempfile
from pathlib import Path

from watcher import has_speech

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
print("all no-speech guard tests passed")
