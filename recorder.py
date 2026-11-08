#!/usr/bin/env python3
"""Meeting recorder core: Teams/Chrome call detection + streaming WAV recording.

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


# ---------- Call detection (Teams app + Chrome) ----------

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


# Apps whose calls we record: Teams desktop and Chrome (Meet, Teams web, any WebRTC call).
CALL_APPS = ("Microsoft Teams.app", "Google Chrome.app")


def count_app_media_sockets(app_path_pattern: str) -> int:
    """Count active RTP/media UDP sockets held by one app's process family.

    Apps open media sockets in helper processes (Teams 'ModuleHost', Chrome
    network service), NOT the top-level process, so we match the whole .app
    family by PID (`pgrep -f <App>.app`) instead of `lsof -c <name>`.

    Known limitation: on relay-only corporate networks (UDP blocked, media
    over TCP/TLS 443) this returns 0 during a call — callers must not treat
    a persistent 0 as proof there is no meeting.
    """
    try:
        pids = subprocess.run(
            ["pgrep", "-f", app_path_pattern],
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
        logger.exception("media socket probe failed for %s", app_path_pattern)
        return 0


def count_call_media_sockets() -> int:
    """Media sockets across all CALL_APPS (Teams + Chrome)."""
    return sum(count_app_media_sockets(app) for app in CALL_APPS)


def is_in_call() -> bool:
    return count_call_media_sockets() > 0


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


def list_outputs() -> list[str]:
    """All macOS output device names."""
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-a", "-t", "output"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.exception("SwitchAudioSource list failed")
        return []


DEFAULT_MULTI_OUTPUT = "Multi-Output sem Fone"  # speakers + BlackHole
HEADPHONE_MULTI_OUTPUT = "Multi-Output Fone"   # headphones + BlackHole


def pick_multi_output(current: str, outputs: list[str]) -> str:
    """Multi-Output to record through, keeping sound where the user hears it.

    Speakers (or an existing Multi-Output) → the default speakers+BlackHole one.
    Anything else (wired headphones, USB headset) → the headphones one, if the
    user created it in Audio MIDI Setup; otherwise the default.
    """
    # ponytail: a single headphone Multi-Output; one per device if several headphones are used
    on_speakers = not current or "Speakers" in current or current.startswith("Multi-Output")
    if not on_speakers and HEADPHONE_MULTI_OUTPUT in outputs:
        return HEADPHONE_MULTI_OUTPUT
    return DEFAULT_MULTI_OUTPUT


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


# ---------- Process-tap helper (tap/tap-capture) ----------
# Primary capture: the Swift helper records mic + Teams/Chrome audio itself
# (a tap read by another process is silent). BlackHole is the fallback.

TAP_DIR = Path(__file__).parent / "tap"
TAP_HELPER = TAP_DIR / "tap-capture"
TAP_SOURCE = TAP_DIR / "main.swift"
TAP_READY_TIMEOUT = 8  # seconds for the helper to print READY
TAP_STOP_TIMEOUT = 15   # seconds for the helper to finalize the WAV after SIGTERM


def ensure_tap_helper() -> Path | None:
    """Build the helper if missing or older than its source.

    Call once at service startup (watcher / MCP server), never during a
    recording: swiftc takes minutes, and a rebuilt binary has a new code
    signature, so a System Audio permission granted to it must be re-granted.
    """
    if TAP_HELPER.exists() and (not TAP_SOURCE.exists()
                                or TAP_HELPER.stat().st_mtime >= TAP_SOURCE.stat().st_mtime):
        return TAP_HELPER
    try:
        subprocess.run(["sh", str(TAP_DIR / "build.sh")], check=True,
                       capture_output=True, text=True, timeout=300)
        return TAP_HELPER
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        logger.exception("could not build the process-tap helper")
        return None


def parse_tap_line(line: str) -> dict | None:
    """Parse a helper stdout line: 'READY k=v ...' / 'DONE k=v ...' → dict, else None."""
    event, _, rest = line.strip().partition(" ")
    if event not in ("READY", "DONE"):
        return None
    out: dict = {"event": event}
    for pair in rest.split():
        key, _, value = pair.partition("=")
        out[key] = float(value) if "." in value else int(value)
    return out


def _read_line(proc: subprocess.Popen, timeout: float) -> str:
    """One stdout line from proc, or '' on timeout/EOF."""
    result: list[str] = []
    reader = threading.Thread(target=lambda: result.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    return result[0] if result else ""


def start_tap(helper: Path | None, wav_path: Path) -> subprocess.Popen | None:
    """Start the helper recording to wav_path; None (reason logged) if it is not READY."""
    if helper is None or not helper.exists():
        logger.error("process tap helper not built — falling back to BlackHole")
        return None
    try:
        proc = subprocess.Popen([str(helper), str(wav_path)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        logger.exception("process tap helper could not be launched — falling back to BlackHole")
        return None
    line = _read_line(proc, TAP_READY_TIMEOUT)
    try:
        info = parse_tap_line(line) or {}
    except ValueError:
        logger.exception("malformed helper line %r", line)
        info = {}
    # tap_channels == 0: the tap did not join the aggregate → mic-only; BlackHole does better.
    if info.get("event") == "READY" and info.get("tap_channels", 0) > 0:
        logger.info("process tap recording: %s", line.strip())
        return proc
    proc.kill()
    proc.wait()
    logger.error("process tap unavailable (%s) — falling back to BlackHole",
                 line.strip() or f"no READY in {TAP_READY_TIMEOUT}s")
    return None


def stop_tap(proc: subprocess.Popen) -> dict:
    """SIGTERM the helper and return its DONE info ({} if it did not finish cleanly)."""
    if proc.poll() is None:
        proc.terminate()
    line = _read_line(proc, TAP_STOP_TIMEOUT)  # dead helper: EOF returns at once
    try:
        info = parse_tap_line(line) or {}
    except ValueError:
        logger.exception("malformed helper line %r", line)
        info = {}
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        logger.error("process tap helper did not exit — killing (WAV may be truncated)")
        proc.kill()
        proc.wait()
    return info


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
        self.capture: str | None = None  # "tap" | "blackhole", decided once the call is up
        self.tap_peak: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._original_output = ""
        self._misses = 0
        self._next_check = 0.0

    def start(self) -> str | None:
        """Start recording in the background. Returns error message or None.

        The capture path (process tap or BlackHole) is chosen in _run(), after
        the call is detected: the tap needs the call apps' audio objects alive.
        """
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in self.name)
        self.wav_path = RECORDINGS_DIR / f"{safe_name}_{timestamp}.wav"

        if not acquire_lock(self.wav_path):
            return f"Another recording is already in progress (see {LOCK_FILE})."

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return None

    def _run(self) -> None:
        try:
            if self.auto_stop and not is_in_call():
                self.state = "waiting_call"
                deadline = time.monotonic() + self.wait_minutes * 60
                while not is_in_call():
                    if self._stop.is_set():
                        self.state = "done"
                        return
                    if time.monotonic() > deadline:
                        self.state = "error"
                        self.error = (f"No Teams or Chrome call detected in {self.wait_minutes:.0f} min. "
                                      "On networks where Teams media runs over TCP/443 "
                                      "(relay-only) detection cannot work — use auto_stop=False.")
                        return
                    self._stop.wait(10)
            self._next_check = time.monotonic() + CALL_CHECK_SECONDS
            # Built at service startup. Core Audio intermittently gives a fresh tap no
            # IO (measured 2026-11-08); a second helper process usually gets it.
            proc = start_tap(TAP_HELPER, self.wav_path) or start_tap(TAP_HELPER, self.wav_path)
            if proc:
                self.capture = "tap"
                self._record_tap(proc)
            else:
                self.capture = "blackhole"
                self._record_blackhole()
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

    def _call_ended(self) -> bool:
        """Poll call sockets every CALL_CHECK_SECONDS; True after STOP_AFTER_MISSES empty reads."""
        if not self.auto_stop or time.monotonic() < self._next_check:
            return False
        self._next_check = time.monotonic() + CALL_CHECK_SECONDS
        n = count_call_media_sockets()
        logger.info("call media sockets: %d", n)
        # Hysteresis: ICE renegotiation dips the count mid-call,
        # so only stop after several consecutive empty reads.
        self._misses = 0 if n > 0 else self._misses + 1
        if self._misses >= STOP_AFTER_MISSES:
            logger.info("call ended — stopping recording")
            return True
        return False

    def _record_tap(self, proc: subprocess.Popen) -> None:
        """The helper writes the WAV; we only decide when to stop it."""
        self.state = "recording"
        started = time.monotonic()
        try:
            while not self._stop.is_set() and self.seconds_recorded < self.max_seconds:
                if proc.poll() is not None:
                    # Keep what was captured: end the take like a call end. tap_peak stays
                    # None (no DONE line), so the watcher warns about the call audio.
                    logger.error("process tap helper exited early (code %s) — keeping %.0fs recorded",
                                 proc.returncode, self.seconds_recorded)
                    break
                if self._call_ended():
                    break
                self._stop.wait(0.2)
                self.seconds_recorded = time.monotonic() - started
        finally:
            info = stop_tap(proc)
            self.seconds_recorded = info.get("seconds", self.seconds_recorded)
            self.tap_peak = info.get("tap_peak")

    def _record_blackhole(self) -> None:
        """Fallback: route output through a Multi-Output with BlackHole and record it."""
        device_index, channels = find_recording_device()
        if device_index is None:
            raise RuntimeError("No recording device found. Configure 'Gravar Reunião' "
                               "in Audio MIDI Setup or install BlackHole: brew install blackhole-2ch")
        current = get_current_output()
        multi_output = pick_multi_output(current, list_outputs())
        if not switch_audio_output(multi_output):
            raise RuntimeError(f"Could not switch to {multi_output}. "
                               "Make sure it is configured in Audio MIDI Setup.")
        self._original_output = current  # restored in _run()'s finally
        self._device_index = device_index
        self._channels = channels
        self._record()

    def _record(self) -> None:
        buf: queue.Queue = queue.Queue()

        def callback(indata, frames, time_info, status):
            if status:
                logger.warning("stream status: %s", status)
            buf.put(indata.copy())

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
                    if self._call_ended():
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
            "capture": self.capture,
            "tap_peak": self.tap_peak,
        }
