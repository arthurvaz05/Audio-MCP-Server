#!/usr/bin/env python3
"""Meeting recorder core: Teams call detection + streaming WAV recording.

Shared by the MCP server (audio_server.py) and the background watcher
(watcher.py). A PID lock file guarantees at most one recording per machine,
whichever entry point started it.
"""
import datetime
import json
import logging
import os
import queue
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path.home() / "Recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)
LOCK_FILE = RECORDINGS_DIR / ".recording.lock"

SAMPLE_RATE = 44100
CALL_CHECK_SECONDS = 10
STOP_AFTER_MISSES = 3  # ~30s without media before concluding the call ended


# ---------- Teams call detection ----------

def count_media_sockets_from_lsof(lsof_output: str) -> int:
    """Pure parser: count UDP sockets bound to a concrete local IP.

    Excludes wildcard listeners ('*:port'), QUIC (:443) and mDNS (:5353),
    which an idle Teams keeps open. Measured: ~20 in call, 0 idle.
    """
    media = 0
    for line in lsof_output.splitlines()[1:]:  # skip header
        parts = line.split()
        if not parts:
            continue
        addr = parts[-1]  # NAME field: 'local' or 'local->peer'
        if addr.startswith("*:") or addr.endswith((":5353", ":443")):
            continue
        media += 1
    return media


def count_teams_media_sockets() -> int:
    """Count active RTP/media UDP sockets held by the Teams process family.

    Teams 2 opens media sockets in child helper processes (e.g. 'Microsoft
    Teams ModuleHost'), NOT the top-level 'MSTeams' process, so we match the
    whole Teams.app family by PID instead of `lsof -c MSTeams`.

    Known limitation: on relay-only corporate networks (UDP blocked, media
    over TCP/TLS 443) this returns 0 during a call — callers must not treat
    a persistent 0 as proof there is no meeting.
    """
    try:
        pids = subprocess.run(
            ["pgrep", "-f", "Microsoft Teams.app"],
            capture_output=True, text=True, timeout=5
        ).stdout.split()
        if not pids:
            return 0
        result = subprocess.run(
            ["lsof", "-nP", "-iUDP", "-a", "-p", ",".join(pids)],
            capture_output=True, text=True, timeout=5
        )
        return count_media_sockets_from_lsof(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.exception("Teams media socket probe failed")
        return 0


def is_teams_in_call() -> bool:
    return count_teams_media_sockets() > 0


# ---------- Audio device helpers ----------

def switch_audio_output(device_name: str) -> bool:
    """Switch macOS audio output device using SwitchAudioSource."""
    try:
        subprocess.run(
            ["SwitchAudioSource", "-s", device_name, "-t", "output"],
            check=True, capture_output=True, text=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.exception("SwitchAudioSource failed for %r", device_name)
        return False


def get_current_output() -> str:
    """Get current macOS audio output device name."""
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-c", "-t", "output"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.exception("SwitchAudioSource query failed")
        return ""


def find_recording_device() -> tuple[int | None, int]:
    """Find the recording device: (GLOBAL device index, channel count).

    Prefers the 'Gravação/Gravar Reunião' aggregate (mic + system audio),
    falls back to BlackHole. Indices are into sd.query_devices() — the global
    list sd.InputStream/sd.rec expect (a filtered-list index points at the
    wrong device).
    """
    fallback = None
    for i, d in enumerate(sd.query_devices()):
        if d['max_input_channels'] <= 0:
            continue
        name = d['name']
        if 'Gravação Reunião' in name or 'Gravar Reunião' in name:
            return i, d['max_input_channels']
        if 'BlackHole' in name and fallback is None:
            fallback = (i, d['max_input_channels'])
    return fallback if fallback else (None, 0)


# ---------- Single-recording lock ----------

def acquire_lock(wav_path: Path) -> bool:
    """Atomically acquire the machine-wide recording lock. False if busy."""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            info = json.loads(LOCK_FILE.read_text())
            os.kill(info["pid"], 0)  # raises OSError if the holder died
            return False  # a live recording is in progress
        except (OSError, ValueError, KeyError):
            logger.warning("removing stale recording lock %s", LOCK_FILE)
            LOCK_FILE.unlink(missing_ok=True)
            return acquire_lock(wav_path)
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "wav": str(wav_path)}, f)
    return True


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


# ---------- Recorder ----------

class MeetingRecorder:
    """Records a meeting in a background thread, streaming mono WAV to disk.

    Streaming (vs accumulating in RAM) keeps memory flat (~MBs instead of
    ~11 GB for 2h/3ch) and preserves the partial file if anything crashes.
    Mono downmix: the target is transcription — Whisper downmixes anyway,
    and the file is 3x smaller than the 3ch aggregate.
    """

    def __init__(self, name: str = "meeting", auto_stop: bool = True,
                 max_seconds: float = 7200, wait_minutes: float = 5):
        self.name = name
        self.auto_stop = auto_stop
        self.max_seconds = max_seconds
        self.wait_minutes = wait_minutes
        self.state = "idle"  # idle|waiting_call|recording|done|error
        self.error: str | None = None
        self.seconds_recorded = 0.0
        self.wav_path: Path | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._original_output = ""

    def start(self) -> str | None:
        """Start recording in the background. Returns error message or None."""
        device_index, channels = find_recording_device()
        if device_index is None:
            return ("No recording device found. Configure 'Gravação Reunião' "
                    "in Audio MIDI Setup or install BlackHole: brew install blackhole-2ch")
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in self.name)
        self.wav_path = RECORDINGS_DIR / f"{safe_name}_{timestamp}.wav"

        if not acquire_lock(self.wav_path):
            return f"Another recording is already in progress (see {LOCK_FILE})."

        self._original_output = get_current_output()
        if not switch_audio_output("Multi-Output Device"):
            release_lock()
            return ("Could not switch to Multi-Output Device. "
                    "Make sure it is configured in Audio MIDI Setup.")

        self._device_index = device_index
        self._channels = channels
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return None

    def _run(self) -> None:
        try:
            if self.auto_stop and not is_teams_in_call():
                self.state = "waiting_call"
                deadline = time.monotonic() + self.wait_minutes * 60
                while not is_teams_in_call():
                    if self._stop.is_set():
                        self.state = "done"
                        return
                    if time.monotonic() > deadline:
                        self.state = "error"
                        self.error = (f"No Teams call detected in {self.wait_minutes:.0f} min. "
                                      "On networks where Teams media runs over TCP/443 "
                                      "(relay-only) detection cannot work — use auto_stop=False.")
                        return
                    self._stop.wait(10)
            self._record()
            self.state = "done"
        except Exception as e:
            logger.exception("recording failed")
            self.state = "error"
            self.error = f"{type(e).__name__}: {e} (partial audio kept at {self.wav_path})"
        finally:
            if self._original_output and not switch_audio_output(self._original_output):
                logger.error("could not restore audio output to %r — fix it in "
                             "System Settings > Sound", self._original_output)
            release_lock()

    def _record(self) -> None:
        buf: queue.Queue = queue.Queue()

        def callback(indata, frames, time_info, status):
            if status:
                logger.warning("stream status: %s", status)
            buf.put(indata.copy())

        misses = 0
        next_check = time.monotonic() + CALL_CHECK_SECONDS
        with wave.open(str(self.wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(SAMPLE_RATE)
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=self._channels,
                                device=self._device_index, callback=callback):
                self.state = "recording"
                while not self._stop.is_set() and self.seconds_recorded < self.max_seconds:
                    try:
                        block = buf.get(timeout=1)
                    except queue.Empty:
                        continue
                    mono = block.mean(axis=1) if block.ndim > 1 else block
                    wf.writeframes(
                        (np.clip(mono, -1, 1) * 32767).astype(np.int16).tobytes())
                    self.seconds_recorded += len(block) / SAMPLE_RATE

                    if self.auto_stop and time.monotonic() >= next_check:
                        next_check = time.monotonic() + CALL_CHECK_SECONDS
                        n = count_teams_media_sockets()
                        logger.info("teams media sockets: %d", n)
                        # Hysteresis: ICE renegotiation dips the count mid-call,
                        # so only stop after several consecutive empty reads.
                        if n > 0:
                            misses = 0
                        else:
                            misses += 1
                            if misses >= STOP_AFTER_MISSES:
                                logger.info("call ended — stopping recording")
                                break

    def stop(self) -> None:
        """Request stop and wait for the file to be finalized."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def status(self) -> dict:
        return {
            "state": self.state,
            "seconds": round(self.seconds_recorded, 1),
            "wav": str(self.wav_path) if self.wav_path else None,
            "error": self.error,
        }
