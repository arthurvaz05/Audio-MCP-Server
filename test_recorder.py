"""Locks the measured Teams call-detection behavior (~20 sockets in call, 0 idle).
Run: .venv/bin/python3 test_recorder.py"""
from recorder import count_media_sockets_from_lsof

# Real lsof -nP -iUDP output captured OUT of a call (2026-07-07):
# only QUIC(:443) with peer + a wildcard listener remain.
IDLE = """\
COMMAND     PID       USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
Microsoft 66171  arthurvaz   88u  IPv6 0x1234      0t0    UDP [2804:14d::1]:57716->[2603:1056::2]:443
Microsoft 66171  arthurvaz   89u  IPv6 0x1235      0t0    UDP [2804:14d::1]:57791->[2001:4860::8844]:443
Microsoft 66177  arthurvaz   90u  IPv4 0x1236      0t0    UDP *:50071
"""

# Real shape captured IN a call: media sockets bound to concrete local IPs
# on high ephemeral ports (subset of the ~20 measured).
IN_CALL = IDLE + """\
Microsoft 66177  arthurvaz   91u  IPv4 0x2001      0t0    UDP 192.168.0.240:50006
Microsoft 66177  arthurvaz   92u  IPv4 0x2002      0t0    UDP 192.168.0.240:50031
Microsoft 66177  arthurvaz   93u  IPv6 0x2003      0t0    UDP [2804:14d::d5e8]:50007
Microsoft 66177  arthurvaz   94u  IPv6 0x2004      0t0    UDP [2804:14d::bc37]:50012
"""

assert count_media_sockets_from_lsof("") == 0, "empty output"
assert count_media_sockets_from_lsof(IDLE) == 0, "idle Teams must count 0"
assert count_media_sockets_from_lsof(IN_CALL) == 4, "in-call media sockets"
# port suffix must not false-match (:5443 is not :443)
TRICKY = IDLE + "Microsoft 66177 arthurvaz 95u IPv4 0x3001 0t0 UDP 192.168.0.240:5443\n"
assert count_media_sockets_from_lsof(TRICKY) == 1, ":5443 is media, not QUIC"
print("all detection tests passed")

# Output routing: headphones must keep sound in the headphones while recording.
from recorder import pick_multi_output
WITH_FONE = ["MacBook Pro Speakers", "BlackHole 2ch", "Multi-Output sem Fone",
             "Multi-Output Fone", "External Headphones"]
NO_FONE = [d for d in WITH_FONE if d != "Multi-Output Fone"]
assert pick_multi_output("MacBook Pro Speakers", WITH_FONE) == "Multi-Output sem Fone"
assert pick_multi_output("Multi-Output sem Fone", WITH_FONE) == "Multi-Output sem Fone"
assert pick_multi_output("Multi-Output Fone", WITH_FONE) == "Multi-Output sem Fone"  # stale from crash: safe default
assert pick_multi_output("External Headphones", WITH_FONE) == "Multi-Output Fone"
assert pick_multi_output("External Headphones", NO_FONE) == "Multi-Output sem Fone", "not created yet: old behavior"
assert pick_multi_output("", WITH_FONE) == "Multi-Output sem Fone", "SwitchAudioSource failed"
print("all routing tests passed")

# Call detection: Teams app OR Chrome (Meet / Teams web).
import recorder
# Real Chrome idle capture 2026-11-07: 8 sockets, all mDNS.
CHROME_IDLE = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n" + \
    "Google 1330 arthurvaz 40u IPv4 0x1 0t0 UDP *:5353\n" * 8
assert recorder.count_media_sockets_from_lsof(CHROME_IDLE) == 0, "idle Chrome counts 0"

_real_probe = recorder.count_app_media_sockets
calls = {}
recorder.count_app_media_sockets = lambda pattern: calls.get(pattern, 0)  # boundary: lsof probe
calls = {"Microsoft Teams.app": 0, "Google Chrome.app": 3}
assert recorder.is_in_call(), "Chrome-only call"
calls = {"Microsoft Teams.app": 4, "Google Chrome.app": 0}
assert recorder.is_in_call(), "Teams-only call"
calls = {}
assert not recorder.is_in_call(), "idle"
assert recorder.count_call_media_sockets() == 0
recorder.count_app_media_sockets = _real_probe
print("all call-detection tests passed")

# ---------- Capture: process-tap helper first, BlackHole fallback ----------
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from recorder import parse_tap_line
assert parse_tap_line("READY tap_channels=2 rate=48000") == {"event": "READY", "tap_channels": 2, "rate": 48000}
assert parse_tap_line("DONE seconds=4.0 tap_peak=0.0500") == {"event": "DONE", "seconds": 4.0, "tap_peak": 0.05}
assert parse_tap_line("ERROR no target audio process") is None
assert parse_tap_line("") is None

tmp = Path(tempfile.mkdtemp())
recorder.RECORDINGS_DIR = tmp
recorder.LOCK_FILE = tmp / ".recording.lock"

def fake_helper(body: str) -> Path:
    """A real subprocess standing in for tap/tap-capture (boundary: the Swift helper)."""
    path = tmp / f"helper_{abs(hash(body))}.py"
    path.write_text(f"#!{sys.executable}\nimport sys, signal, time, wave\n{body}\n")
    path.chmod(0o755)
    return path

RECORDING_HELPER = fake_helper(
    "w = wave.open(sys.argv[1], 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)\n"
    "w.writeframes(b'\\0\\0' * 4800); w.close()\n"
    "signal.signal(signal.SIGTERM, lambda *a: (print('DONE seconds=0.1 tap_peak=0.2500', flush=True), sys.exit(0)))\n"
    "print('READY tap_channels=2 rate=48000', flush=True)\n"
    "while True: time.sleep(0.05)")
FAILING_HELPER = fake_helper("print('ERROR no target audio process', flush=True); sys.exit(1)")
HANGING_HELPER = fake_helper("time.sleep(60)")

switches = []
recorder.switch_audio_output = lambda name: switches.append(name) or True   # boundary: SwitchAudioSource
recorder.get_current_output = lambda: "External Headphones"
recorder.list_outputs = lambda: ["Multi-Output sem Fone", "Multi-Output Fone", "External Headphones"]
recorder.find_recording_device = lambda: (0, 3)

class FakeStream:  # boundary: sounddevice input stream delivering 3-channel blocks
    def __init__(self, samplerate, channels, device, callback):
        self.cb, self.ch, self.live = callback, channels, True
    def __enter__(self):
        def pump():
            while self.live:
                self.cb(np.full((441, self.ch), 0.1, dtype=np.float32), 441, None, None)
                time.sleep(0.01)
        threading.Thread(target=pump, daemon=True).start()
        return self
    def __exit__(self, *a):
        self.live = False
recorder.sd.InputStream = FakeStream

def no_build_during_call():
    raise AssertionError("helper must never be (re)built inside a recording")
recorder.ensure_tap_helper = no_build_during_call  # build happens at service startup only

def run(helper, max_seconds=0.5):
    switches.clear()
    recorder.TAP_HELPER = helper if helper else tmp / "missing-helper"   # boundary: built binary
    r = recorder.MeetingRecorder(name="t", auto_stop=False, max_seconds=max_seconds)
    assert r.start() is None
    r.join(15)
    return r.status()

# test_tap_path_does_not_switch_output
st = run(RECORDING_HELPER)
assert st["state"] == "done", st
assert st["capture"] == "tap" and st["tap_peak"] == 0.25, st
assert switches == [], f"tap path must not touch the output device: {switches}"
assert Path(st["wav"]).exists()
assert not recorder.LOCK_FILE.exists(), "lock released"

# test_tap_failure_falls_back (helper prints ERROR)
st = run(FAILING_HELPER)
assert st["state"] == "done" and st["capture"] == "blackhole" and st["tap_peak"] is None, st
assert switches == ["Multi-Output Fone", "External Headphones"], f"switch then restore: {switches}"
assert st["seconds"] >= 0.5

# test_tap_timeout_falls_back (helper never prints READY)
recorder.TAP_READY_TIMEOUT = 0.5
st = run(HANGING_HELPER)
assert st["capture"] == "blackhole", st

# test_no_helper_falls_back (build failed)
st = run(None)
assert st["capture"] == "blackhole", st

# test_helper_dies_mid_call_keeps_recording_as_done: an early exit ends the take
# like a call end, so the minutes already captured still get transcribed.
DYING_HELPER = fake_helper(
    "w = wave.open(sys.argv[1], 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)\n"
    "w.writeframes(b'\\0\\0' * 4800); w.close()\n"
    "print('READY tap_channels=2 rate=48000', flush=True); time.sleep(0.3); sys.exit(3)")
st = run(DYING_HELPER, max_seconds=5)
assert st["state"] == "done" and st["capture"] == "tap" and st["tap_peak"] is None, st

# test_unlaunchable_helper_falls_back (present but not executable)
broken = tmp / "not-executable"
broken.write_text("x")
broken.chmod(0o644)
st = run(broken)
assert st["state"] == "done" and st["capture"] == "blackhole", st

# test_malformed_ready_falls_back
st = run(fake_helper("print('READY tap_channels=x rate=48000', flush=True); time.sleep(60)"))
assert st["capture"] == "blackhole", st

# test_tap_without_channels_falls_back (tap did not join the aggregate)
st = run(fake_helper("print('READY tap_channels=0 rate=48000', flush=True); time.sleep(60)"))
assert st["capture"] == "blackhole", st
print("all capture tests passed")
