#!/usr/bin/env python3
import asyncio
import base64
import io
import json
import os
import subprocess
import sounddevice as sd
import soundfile as sf
import numpy as np
import tempfile
import wave
import datetime
from pathlib import Path
from mcp.server.fastmcp import FastMCP

# Directory to save recordings
RECORDINGS_DIR = Path.home() / "Recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)

# Initialize FastMCP server
mcp = FastMCP("audio-interface")

# Constants
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_CHANNELS = 1
DEFAULT_DURATION = 5  # seconds

async def get_audio_devices():
    """Get a list of all available audio devices."""
    devices = sd.query_devices()
    input_devices = [d for d in devices if d['max_input_channels'] > 0]
    output_devices = [d for d in devices if d['max_output_channels'] > 0]
    
    return {
        "input_devices": input_devices,
        "output_devices": output_devices
    }

@mcp.tool()
async def list_audio_devices() -> str:
    """List all available audio input and output devices on the system."""
    devices = await get_audio_devices()
    
    result = "Audio devices available on your system:\n\n"
    
    result += "INPUT DEVICES (MICROPHONES):\n"
    for i, device in enumerate(devices["input_devices"]):
        result += f"{i}: {device['name']} (Channels: {device['max_input_channels']})\n"
    
    result += "\nOUTPUT DEVICES (SPEAKERS):\n"
    for i, device in enumerate(devices["output_devices"]):
        result += f"{i}: {device['name']} (Channels: {device['max_output_channels']})\n"
    
    return result

@mcp.tool()
async def record_audio(duration: float = DEFAULT_DURATION, 
                       sample_rate: int = DEFAULT_SAMPLE_RATE,
                       channels: int = DEFAULT_CHANNELS,
                       device_index: int = None) -> str:
    """Record audio from the microphone.
    
    Args:
        duration: Recording duration in seconds (default: 5)
        sample_rate: Sample rate in Hz (default: 44100)
        channels: Number of audio channels (default: 1)
        device_index: Specific input device index to use (default: system default)
    
    Returns:
        A message confirming the recording was captured
    """
    try:
        # Check if the specified device exists and is an input device
        if device_index is not None:
            devices = await get_audio_devices()
            input_devices = devices["input_devices"]
            if device_index < 0 or device_index >= len(input_devices):
                return f"Error: Invalid device index {device_index}. Use list_audio_devices tool to see available devices."
        
        # Record audio
        recording = sd.rec(
            int(duration * sample_rate),
            samplerate=sample_rate,
            channels=channels,
            device=device_index
        )
        
        # Wait for the recording to complete
        sd.wait()
        
        # Save recording to file
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        save_path = str(RECORDINGS_DIR / f"recording_{timestamp}.wav")

        with wave.open(save_path, 'wb') as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes((recording * 32767).astype(np.int16).tobytes())

        # Encode for in-memory playback
        with open(save_path, 'rb') as f:
            encoded_audio = base64.b64encode(f.read()).decode('utf-8')

        # Store the audio in a global variable for later playback
        global latest_recording
        latest_recording = {
            'audio_data': encoded_audio,
            'sample_rate': sample_rate,
            'channels': channels
        }

        return f"Successfully recorded {duration} seconds of audio. Saved to {save_path}. Use play_audio_file to play it back on a specific device."
            
    except Exception as e:
        return f"Error recording audio: {str(e)}"

# Global variable to store the latest recording
latest_recording = None

@mcp.tool()
async def play_latest_recording() -> str:
    """Play the latest recorded audio through the speakers."""
    global latest_recording
    
    if latest_recording is None:
        return "No recording available. Use record_audio tool first."
    
    try:
        # Decode the audio data
        audio_data = base64.b64decode(latest_recording['audio_data'])
        sample_rate = latest_recording['sample_rate']
        channels = latest_recording['channels']
        
        # Create a temporary file
        fd, temp_path = tempfile.mkstemp(suffix='.wav')
        try:
            # Write the audio data to the temp file
            with open(temp_path, 'wb') as f:
                f.write(audio_data)
            
            # Read the audio file
            data, fs = sf.read(temp_path)
            
            # Play the audio
            sd.play(data, fs)
            sd.wait()  # Wait until the audio is done playing
            
            return "Successfully played the latest recording."
        finally:
            os.close(fd)
            os.unlink(temp_path)
    except Exception as e:
        return f"Error playing audio: {str(e)}"

@mcp.tool()
async def play_audio(text: str, voice: str = "default") -> str:
    """
    Play audio from text using text-to-speech.
    
    Args:
        text: The text to convert to speech
        voice: The voice to use (default: "default")
    
    Returns:
        A message indicating if the audio was played successfully
    """
    try:
        # Note: This is a simplified implementation that would need to be expanded
        # with an actual TTS service like gTTS, pyttsx3, or an external API
        
        # For now, we'll return a message indicating that TTS is not implemented
        return (
            "Text-to-speech functionality requires additional setup. "
            "You would need to install a TTS library like gTTS or pyttsx3, "
            f"which would convert the text '{text}' to audio using voice '{voice}'. "
            "This would then be played through your speakers."
        )
    except Exception as e:
        return f"Error playing audio: {str(e)}"

@mcp.tool()
async def play_audio_file(file_path: str, device_index: int = None) -> str:
    """
    Play an audio file through the speakers.
    
    Args:
        file_path: Path to the audio file
        device_index: Specific output device index to use (default: system default)
    
    Returns:
        A message indicating if the audio was played successfully
    """
    try:
        # Check if the file exists
        if not os.path.exists(file_path):
            return f"Error: File not found at {file_path}"
        
        # Check if the specified device exists and is an output device
        if device_index is not None:
            devices = await get_audio_devices()
            output_devices = devices["output_devices"]
            if device_index < 0 or device_index >= len(output_devices):
                return f"Error: Invalid device index {device_index}. Use list_audio_devices tool to see available devices."
        
        # Read the audio file
        data, fs = sf.read(file_path)
        
        # Play the audio
        sd.play(data, fs, device=device_index)
        sd.wait()  # Wait until the audio is done playing
        
        return f"Successfully played audio file: {file_path}"
    except Exception as e:
        return f"Error playing audio file: {str(e)}"

def _switch_audio_output(device_name: str) -> bool:
    """Switch macOS audio output device using SwitchAudioSource."""
    try:
        subprocess.run(
            ["SwitchAudioSource", "-s", device_name, "-t", "output"],
            check=True, capture_output=True, text=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def _get_current_output() -> str:
    """Get current macOS audio output device name."""
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-c", "-t", "output"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""

def _find_blackhole_input_index() -> int | None:
    """Find the BlackHole 2ch input device index."""
    devices = sd.query_devices()
    input_devices = [d for d in devices if d['max_input_channels'] > 0]
    for i, d in enumerate(input_devices):
        if 'BlackHole' in d['name']:
            return i
    return None

def _is_teams_in_call() -> bool:
    """Detect if Microsoft Teams is currently in a call by checking UDP connections.
    When in a call, Teams opens additional UDP connections for RTP media streams
    beyond its usual signaling port.
    """
    try:
        result = subprocess.run(
            ["lsof", "-i", "UDP", "-a", "-c", "MSTeams"],
            capture_output=True, text=True, timeout=5
        )
        # Count UDP connections - during a call there are more than 1
        udp_lines = [l for l in result.stdout.strip().split('\n')
                      if l and not l.startswith('COMMAND')]
        return len(udp_lines) > 1
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

CHUNK_SECONDS = 10  # Record in 10-second chunks

@mcp.tool()
async def record_meeting(duration: float = 7200, name: str = "meeting", auto_stop: bool = True) -> str:
    """Record a meeting. Automatically switches audio output to capture system audio via BlackHole.

    When auto_stop is True (default), recording automatically stops when you leave the Teams meeting.
    When auto_stop is False, it records for the specified duration.

    Args:
        duration: Maximum recording duration in seconds (default: 7200 = 2 hours)
        name: Name for the recording file (default: "meeting")
        auto_stop: If True, automatically stop when Teams call ends (default: True)

    Returns:
        A message with the path to the saved recording
    """
    original_output = ""
    try:
        # Find BlackHole input device
        bh_index = _find_blackhole_input_index()
        if bh_index is None:
            return "Error: BlackHole 2ch not found. Install it with: brew install blackhole-2ch"

        # Save current output device and switch to Multi-Output Device
        original_output = _get_current_output()
        switched = _switch_audio_output("Multi-Output Device")
        if not switched:
            return "Error: Could not switch to Multi-Output Device. Make sure it is configured in Audio MIDI Setup."

        if auto_stop:
            # Wait for Teams call to start (if not already in one)
            if not _is_teams_in_call():
                # Give a short grace period for the call to start
                for _ in range(30):  # Wait up to 5 minutes
                    await asyncio.sleep(10)
                    if _is_teams_in_call():
                        break
                else:
                    if original_output:
                        _switch_audio_output(original_output)
                    return "Timed out waiting for Teams call to start. No call detected in 5 minutes."

            # Record in chunks, checking call status between each
            all_chunks = []
            total_recorded = 0.0

            while total_recorded < duration:
                chunk = sd.rec(
                    int(CHUNK_SECONDS * DEFAULT_SAMPLE_RATE),
                    samplerate=DEFAULT_SAMPLE_RATE,
                    channels=2,
                    device=bh_index
                )
                sd.wait()
                all_chunks.append(chunk)
                total_recorded += CHUNK_SECONDS

                # Check if still in call
                if not _is_teams_in_call():
                    # Record one more chunk to capture final moments
                    chunk = sd.rec(
                        int(CHUNK_SECONDS * DEFAULT_SAMPLE_RATE),
                        samplerate=DEFAULT_SAMPLE_RATE,
                        channels=2,
                        device=bh_index
                    )
                    sd.wait()
                    all_chunks.append(chunk)
                    total_recorded += CHUNK_SECONDS
                    break

            # Concatenate all chunks
            recording = np.concatenate(all_chunks, axis=0)

        else:
            # Fixed duration recording
            recording = sd.rec(
                int(duration * DEFAULT_SAMPLE_RATE),
                samplerate=DEFAULT_SAMPLE_RATE,
                channels=2,
                device=bh_index
            )
            sd.wait()
            total_recorded = duration

        # Save recording
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name)
        save_path = str(RECORDINGS_DIR / f"{safe_name}_{timestamp}.wav")

        with wave.open(save_path, 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(DEFAULT_SAMPLE_RATE)
            wf.writeframes((recording * 32767).astype(np.int16).tobytes())

        minutes = total_recorded / 60
        return f"Recording complete! {minutes:.1f} minutes saved to {save_path}. Open with: open \"{save_path}\""

    except Exception as e:
        return f"Error recording meeting: {str(e)}"
    finally:
        # Always restore original audio output
        if original_output:
            _switch_audio_output(original_output)

@mcp.tool()
async def transcribe_audio(file_path: str, language: str = "pt") -> str:
    """Transcribe an audio file to text using Whisper (runs locally).

    Args:
        file_path: Path to the audio file (WAV, MP3, etc.)
        language: Language code (default: "pt" for Portuguese). Use "en" for English.

    Returns:
        The full transcription text with timestamps.
    """
    try:
        if not os.path.exists(file_path):
            return f"Error: File not found at {file_path}"

        from faster_whisper import WhisperModel

        # Use base model for speed/quality balance. Downloads on first use.
        model = WhisperModel("base", device="cpu", compute_type="int8")

        segments, info = model.transcribe(file_path, language=language)

        lines = []
        for segment in segments:
            start_min = int(segment.start // 60)
            start_sec = int(segment.start % 60)
            lines.append(f"[{start_min:02d}:{start_sec:02d}] {segment.text.strip()}")

        transcript = "\n".join(lines)

        # Save transcript to file alongside the audio
        transcript_path = file_path.rsplit('.', 1)[0] + '_transcript.txt'
        with open(transcript_path, 'w', encoding='utf-8') as f:
            f.write(transcript)

        return f"Transcription complete ({info.language}, {info.duration:.0f}s). Saved to {transcript_path}\n\n{transcript}"

    except Exception as e:
        return f"Error transcribing audio: {str(e)}"

if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport='stdio')