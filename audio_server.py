#!/usr/bin/env python3
"""Audio MCP server: device utilities + meeting recording + transcription.

Recording runs in a background thread (see recorder.py) so tool calls return
immediately — start_meeting_recording / recording_status / stop_meeting_recording.
"""
import asyncio
import datetime
import logging
import os
import wave

import numpy as np
import sounddevice as sd
import soundfile as sf
from mcp.server.fastmcp import FastMCP

import recorder

logging.basicConfig(level=logging.INFO)  # stderr — captured by the MCP client
logger = logging.getLogger(__name__)

RECORDINGS_DIR = recorder.RECORDINGS_DIR

mcp = FastMCP("audio-interface")

DEFAULT_SAMPLE_RATE = 44100

WHISPER_REPO = "mlx-community/whisper-large-v3-turbo"

# The active meeting recording of this server process (recorder.LOCK_FILE
# additionally guards against recordings started by the watcher/CLI).
_current: recorder.MeetingRecorder | None = None


def _input_devices() -> list[tuple[int, dict]]:
    """(global_index, device) pairs for input devices. Global indices are what
    sd.rec/sd.play/sd.InputStream expect."""
    return [(i, d) for i, d in enumerate(sd.query_devices())
            if d['max_input_channels'] > 0]


def _output_devices() -> list[tuple[int, dict]]:
    return [(i, d) for i, d in enumerate(sd.query_devices())
            if d['max_output_channels'] > 0]


@mcp.tool()
async def list_audio_devices() -> str:
    """List all available audio input and output devices on the system."""
    result = "Audio devices (index = device index to pass to other tools):\n\n"
    result += "INPUT DEVICES (MICROPHONES):\n"
    for i, d in _input_devices():
        result += f"{i}: {d['name']} (Channels: {d['max_input_channels']})\n"
    result += "\nOUTPUT DEVICES (SPEAKERS):\n"
    for i, d in _output_devices():
        result += f"{i}: {d['name']} (Channels: {d['max_output_channels']})\n"
    return result


@mcp.tool()
async def record_audio(duration: float = 5,
                       sample_rate: int = DEFAULT_SAMPLE_RATE,
                       channels: int = 1,
                       device_index: int = None) -> str:
    """Record a short clip from the microphone (setup test — for meetings use
    start_meeting_recording).

    Args:
        duration: Recording duration in seconds (default: 5)
        sample_rate: Sample rate in Hz (default: 44100)
        channels: Number of audio channels (default: 1)
        device_index: Device index from list_audio_devices (default: system default)
    """
    try:
        if device_index is not None:
            valid = {i for i, _ in _input_devices()}
            if device_index not in valid:
                return (f"Error: device index {device_index} is not an input device. "
                        "Use list_audio_devices.")

        def _rec():
            data = sd.rec(int(duration * sample_rate), samplerate=sample_rate,
                          channels=channels, device=device_index)
            sd.wait()
            return data

        recording = await asyncio.to_thread(_rec)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        save_path = str(RECORDINGS_DIR / f"recording_{timestamp}.wav")
        with wave.open(save_path, 'wb') as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes((np.clip(recording, -1, 1) * 32767).astype(np.int16).tobytes())

        return (f"Successfully recorded {duration} seconds of audio. Saved to "
                f"{save_path}. Use play_audio_file to play it back.")
    except Exception as e:
        logger.exception("record_audio failed")
        return f"Error recording audio: {e}"


@mcp.tool()
async def play_audio_file(file_path: str, device_index: int = None) -> str:
    """Play an audio file through the speakers.

    Args:
        file_path: Path to the audio file
        device_index: Output device index from list_audio_devices (default: system default)
    """
    try:
        if not os.path.exists(file_path):
            return f"Error: File not found at {file_path}"
        if device_index is not None:
            valid = {i for i, _ in _output_devices()}
            if device_index not in valid:
                return (f"Error: device index {device_index} is not an output device. "
                        "Use list_audio_devices.")

        def _play():
            data, fs = sf.read(file_path)
            sd.play(data, fs, device=device_index)
            sd.wait()

        await asyncio.to_thread(_play)
        return f"Successfully played audio file: {file_path}"
    except Exception as e:
        logger.exception("play_audio_file failed")
        return f"Error playing audio file: {e}"


@mcp.tool()
async def start_meeting_recording(name: str = "meeting", auto_stop: bool = True,
                                  wait_minutes: float = 5,
                                  max_hours: float = 2) -> str:
    """Start recording a meeting in the background and return immediately.
    Switches audio output to capture system audio via the recording aggregate
    device. Check progress with recording_status; stop early with
    stop_meeting_recording.

    Args:
        name: Name for the recording file (default: "meeting")
        auto_stop: Stop automatically when the Teams call ends (default: True).
            Use False for non-Teams meetings (Meet/Zoom) or relay-only corporate
            networks where Teams media runs over TCP/443 and cannot be detected.
        wait_minutes: With auto_stop, how long to wait for a call to start (default: 5)
        max_hours: Hard recording limit in hours (default: 2)
    """
    global _current
    try:
        if _current and _current.state in ("waiting_call", "recording"):
            return f"Already recording: {_current.status()}"
        rec = recorder.MeetingRecorder(name=name, auto_stop=auto_stop,
                                       max_seconds=max_hours * 3600,
                                       wait_minutes=wait_minutes)
        error = rec.start()
        if error:
            return f"Error: {error}"
        _current = rec
        mode = ("auto-stop when the Teams call ends"
                if auto_stop else f"fixed {max_hours:g}h limit or manual stop")
        return (f"Recording started in background ({mode}). File: {rec.wav_path}. "
                "Use recording_status to check on it.")
    except Exception as e:
        logger.exception("start_meeting_recording failed")
        return f"Error starting recording: {e}"


@mcp.tool()
async def recording_status() -> str:
    """Status of the current meeting recording (state, minutes recorded, file)."""
    if _current is None:
        if recorder.LOCK_FILE.exists():
            return (f"No recording started by this session, but a lock exists at "
                    f"{recorder.LOCK_FILE} — the background watcher may be recording.")
        return "No recording in progress."
    st = _current.status()
    minutes = st["seconds"] / 60
    msg = f"State: {st['state']} | {minutes:.1f} min recorded | file: {st['wav']}"
    if st["error"]:
        msg += f" | error: {st['error']}"
    if st["state"] == "done":
        msg += " — ready to transcribe with transcribe_audio."
    return msg


@mcp.tool()
async def stop_meeting_recording() -> str:
    """Stop the current meeting recording and finalize the WAV file."""
    global _current
    if _current is None:
        return "No recording was started by this session."
    _current.stop()
    st = _current.status()
    _current = None
    return (f"Recording stopped: {st['seconds'] / 60:.1f} min saved to {st['wav']}. "
            "Transcribe it with transcribe_audio.")


def _transcribe_sync(file_path: str, language: str) -> tuple[str, float]:
    """Blocking transcription — run via asyncio.to_thread. mlx_whisper caches
    the model per repo internally, so repeat calls skip the load."""
    import mlx_whisper
    result = mlx_whisper.transcribe(
        file_path, path_or_hf_repo=WHISPER_REPO, language=language)
    lines = []
    for segment in result["segments"]:
        start_min = int(segment["start"] // 60)
        start_sec = int(segment["start"] % 60)
        lines.append(f"[{start_min:02d}:{start_sec:02d}] {segment['text'].strip()}")
    duration = result["segments"][-1]["end"] if result["segments"] else 0.0
    return "\n".join(lines), duration


@mcp.tool()
async def transcribe_audio(file_path: str, language: str = "pt",
                           keep_audio: bool = False) -> str:
    """Transcribe an audio file to text using Whisper large-v3-turbo (runs
    locally on the Apple Silicon GPU via MLX; first use downloads ~1.6 GB).

    Args:
        file_path: Path to the audio file (WAV, MP3, etc.)
        language: Language code (default: "pt" for Portuguese). Use "en" for English.
        keep_audio: Keep the audio file after successful transcription
            (default: False — the transcript is the artifact; meeting audio is
            sensitive and is deleted once transcribed).
    """
    try:
        if not os.path.exists(file_path):
            return f"Error: File not found at {file_path}"

        transcript, duration = await asyncio.to_thread(
            _transcribe_sync, file_path, language)

        transcript_path = file_path.rsplit('.', 1)[0] + '_transcript.txt'
        with open(transcript_path, 'w', encoding='utf-8') as f:
            f.write(transcript)

        deleted = ""
        if not keep_audio:
            os.unlink(file_path)
            deleted = " Audio file deleted (pass keep_audio=True to keep it)."

        return (f"Transcription complete ({language}, {duration:.0f}s). "
                f"Saved to {transcript_path}.{deleted}\n\n{transcript}")
    except Exception as e:
        logger.exception("transcribe_audio failed")
        return f"Error transcribing audio: {e}"


if __name__ == "__main__":
    mcp.run(transport='stdio')
