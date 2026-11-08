# Audio MCP Server
[![smithery badge](https://smithery.ai/badge/@GongRzhe/Audio-MCP-Server)](https://smithery.ai/server/@GongRzhe/Audio-MCP-Server)

An MCP (Model Context Protocol) server that provides audio input/output capabilities for AI assistants like Claude. This server enables Claude to interact with your computer's audio system, including recording from microphones and playing audio through speakers.



## Features

- **List Audio Devices**: View all available microphones and speakers on your system
- **Record Audio**: Capture audio from any microphone with customizable duration and quality
- **Playback Recordings**: Play back your most recent recording
- **Audio File Playback**: Play audio files through your speakers
- **Text-to-Speech**: (Placeholder for future implementation)

## Requirements

- Python 3.8 or higher
- Audio input/output devices on your system

## Installation

### Installing via Smithery

To install Audio Interface Server for Claude Desktop automatically via [Smithery](https://smithery.ai/server/@GongRzhe/Audio-MCP-Server):

```bash
npx -y @smithery/cli install @GongRzhe/Audio-MCP-Server --client claude
```

### Manual Installation
1. Clone this repository or download the files to your computer:

```bash
git clone https://github.com/GongRzhe/Audio-MCP-Server.git
cd Audio-MCP-Server
```

2. Create a virtual environment and install dependencies:

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# macOS/Linux
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Or use the included setup script to automate installation:

```bash
python setup_mcp.py
```

## Configuration

### Claude Desktop Configuration

To use this server with Claude Desktop, add the following to your Claude Desktop configuration file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "audio-interface": {
      "command": "/path/to/your/.venv/bin/python",
      "args": [
        "/path/to/your/audio_server.py"
      ],
      "env": {
        "PYTHONPATH": "/path/to/your/audio-mcp-server"
      }
    }
  }
}
```

Replace the paths with the actual paths on your system. The setup script will generate this configuration for you.

## Usage

After setting up the server, restart Claude Desktop. You should see a hammer icon in the input box, indicating that tools are available.

Try asking Claude:

- "What microphones and speakers are available on my system?"
- "Record 5 seconds of audio from my microphone."
- "Play back the audio recording."
- "Play an audio file from my computer."

## Available Tools

### list_audio_devices

Lists all available audio input and output devices on your system.

### record_audio

Records audio from your microphone.

Parameters:
- `duration`: Recording duration in seconds (default: 5)
- `sample_rate`: Sample rate in Hz (default: 44100)
- `channels`: Number of audio channels (default: 1)
- `device_index`: Specific input device index to use (default: system default)

### play_latest_recording

Plays back the most recently recorded audio.

### play_audio

Placeholder for text-to-speech functionality.

Parameters:
- `text`: The text to convert to speech
- `voice`: The voice to use (default: "default")

### play_audio_file

Plays an audio file through your speakers.

Parameters:
- `file_path`: Path to the audio file
- `device_index`: Specific output device index to use (default: system default)

## Troubleshooting

### No devices found

If no audio devices are found, check:
- Your microphone and speakers are properly connected
- Your operating system recognizes the devices
- You have the necessary permissions to access audio devices

### Playback issues

If audio playback isn't working:
- Check your volume settings
- Ensure the correct output device is selected
- Try restarting the Claude Desktop application

### Meeting capture (Teams app + Chrome)

The watcher auto-records calls in the Teams app and in Chrome (Google Meet,
Teams web, any WebRTC call), detected by their UDP media sockets.

**Primary path: process tap** (macOS 14.4+). `tap/tap-capture` (Swift, built
automatically from `tap/main.swift` via `tap/build.sh`) records the default mic
+ the Teams/Chrome audio straight from those processes into the WAV. It never
changes the output device, so Teams/Chrome can play to any speaker or
headphones. It records itself because a tap read by another process is silent.

One-time permission: System Settings → Privacy & Security → Screen & System
Audio Recording → **System Audio Recording Only** → add and enable the app
that runs the recorder. Measured 2026-11-08: for the LaunchAgent (launchd →
Python → tap-capture) the grant that counts is the bare Homebrew binary
`python3.11` (`/opt/homebrew/Cellar/python@3.11/<ver>/Frameworks/Python.framework/Versions/3.11/bin/python3.11`),
not `Python.app`; macOS adds it to the list after the first attempt, then just
switch it on. A `brew upgrade` of Python changes that path: re-grant. Terminal
covers manual runs. Without it the tap records silence: the watcher then warns
"Áudio da call não foi capturado".
The helper is (re)built only when the watcher / MCP server starts, never during
a recording. A rebuilt binary has a new code signature, so if the permission
was granted to `tap-capture` itself, re-grant it after each rebuild.

**Fallback: BlackHole.** If the helper cannot build, start, or report READY in
10 s, the recorder switches the output to a Multi-Output with BlackHole and
records the `Gravar Reunião` aggregate (mic + BlackHole), restoring the output
afterwards. `Multi-Output sem Fone` = speakers + BlackHole; `Multi-Output Fone`
= headphones (primary/clock) + BlackHole 2ch (drift correction on), used when
the current output is not the speakers. On this path Teams' speaker must be
the matching Multi-Output.

Known limitations: the mic is the default input when recording starts;
Chrome capture includes every Chrome tab's audio during the call.

### Server connectivity

If Claude can't connect to the server:
- Verify your configuration paths are correct
- Ensure Python and all dependencies are installed
- Check Claude's logs for error messages

## License

MIT

## Acknowledgments

- Built using the [Model Context Protocol](https://modelcontextprotocol.io/)
- Uses [sounddevice](https://python-sounddevice.readthedocs.io/) and [soundfile](https://pysoundfile.readthedocs.io/) for audio processing

---

*Note: This server provides tools that can access your microphone and speakers. Always review and approve tool actions before they execute.*
