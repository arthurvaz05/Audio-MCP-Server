# Process-tap capture + Chrome call detection — design

Date: 2026-11-07 · Repo: `Audio-MCP-Server` · Owner: Arthur

## Intent

The meeting watcher (`watcher.py`, LaunchAgent `com.monkai.meeting-watcher`)
auto-records Teams calls, transcribes, writes an ata and emails it. Today it
captures call audio by switching the macOS output to a Multi-Output device
(`Multi-Output sem Fone` / `Multi-Output Fone`) that includes BlackHole, and
reads the `Gravar Reunião` aggregate (mic + BlackHole).

Two pains:

1. **Output routing is fragile.** Teams must have its speaker set to the
   right Multi-Output by hand; plugging/unplugging wired headphones makes
   Teams jump to `External Headphones` (call not recorded) or stay on
   `Multi-Output Fone` with nothing plugged (user hears nothing).
2. **Only the Teams desktop app is detected.** Google Meet and Teams-in-browser
   run in Chrome and are never auto-recorded.

Goal: record any meeting in the Teams app or in Chrome, regardless of which
speaker/headphones the user picks, starting and stopping on its own.

## Goals

- Capture call audio directly from the Teams and Chrome processes (Core Audio
  process tap, macOS 14.4+; this Mac runs macOS 26.5) plus the user's mic.
  No output switching, no Multi-Output needed on the happy path.
- Auto-start and auto-stop for calls in Chrome (any WebRTC call: Google Meet,
  Teams web, others), same as Teams desktop today.
- Keep the current BlackHole/Multi-Output path as **fallback** when the tap
  path is unavailable (helper missing/not compiled, permission denied, tap
  creation error). Fallback must be logged with the reason.
- Downstream pipeline unchanged: WAV in `~/Recordings`, transcription, no-speech
  discard, ata, email, GRKMemory ingest.

## Non-goals

- Restricting capture to the Meet tab only (decided: any Chrome call is fine,
  so no Chrome Automation/AppleScript permission).
- Zoom desktop, Slack huddles, other apps.
- Speaker diarization / separate tracks per source.
- Windows/Linux.

## Anti-goals (failure even if it "works")

- The user stops hearing the call, or their output device is changed, on the
  tap path.
- A broken tap silently produces mic-only recordings (call participants lost)
  with no log/notification.
- A temporary audio device left behind in Audio MIDI Setup after a crash.

## Decided by Arthur

- Tap path primary, BlackHole path as fallback.
- Chrome is Arthur's browser; any call inside Chrome counts (Meet + Teams web).
- Chrome calls start and stop automatically.

## Design (Claude's calls, open to review)

### Capture: Swift helper `tap/` → `tap-capture`

- A small Swift CLI kept in the repo (`tap/main.swift`), built with
  `swiftc` (Command Line Tools, Swift 6.2 present) into `tap/tap-capture`
  (binary git-ignored; `make`-less: one `swiftc` line in README and a build
  step the recorder runs if the binary is missing or older than the source).
- Behavior: find the Core Audio process objects of the target apps (bundle
  IDs `com.microsoft.teams2` and `com.google.Chrome`, including helper
  processes), create a `CATapDescription` (stereo mixdown, not muted) and an
  aggregate device that is **public** (not private) named `Gravar Reunião
  (Tap)` containing the tap + the default input device (mic). Print `READY`
  on stdout, then block until SIGTERM/SIGINT or stdin EOF, and destroy the
  aggregate + tap on exit.
- Being public is what lets the existing Python `sounddevice` stream open it
  by name, so `_record()` stays as is.
- Processes that start after the tap is created (e.g. Chrome spawning a new
  audio helper) are a known risk: the helper should re-scan process objects
  periodically and update the tap's process list
  (`kAudioTapPropertyDescription`) — builder to confirm feasibility; if not
  feasible, tap by bundle ID if the API allows, else accept and document.

### Recorder changes (`recorder.py`)

- `MeetingRecorder.start()`: try tap path first: spawn `tap-capture`, wait up
  to N s for `READY`, then `find_recording_device()` prefers
  `Gravar Reunião (Tap)`. No output switch on this path.
- On any failure → log reason with `logger.exception`/`logger.error`, kill
  helper, then current path (pick_multi_output + switch + `Gravar Reunião`).
- `_run()` finally: terminate helper (if any) before releasing the lock;
  restore output only when the fallback switched it.
- Startup hygiene: if a stale `Gravar Reunião (Tap)` exists (previous crash),
  the helper removes it before creating a new one.

### Call detection

- Generalize `count_teams_media_sockets()` to a per-app probe reused for
  Chrome: `pgrep -f "Google Chrome.app"` → `lsof -iUDP` → existing
  `count_media_sockets_from_lsof` parser.
- Measured 2026-11-07 (Chrome idle, no call): 8 UDP sockets, all mDNS :5353,
  parser counts **0**. In-call count **not measured yet** — must be measured
  in a real Meet call and pinned as a fixture in `test_recorder.py` (same as
  the Teams fixtures).
- `is_in_call()` = Teams media > 0 OR Chrome media > 0. Watcher start rule
  (2 consecutive positives) and stop rule (`STOP_AFTER_MISSES`) unchanged,
  applied to the combined count.
- Known: Chrome blips (pre-join, camera test, any WebRTC page) may start a
  recording; the existing no-speech guard discards those.
- Recording name: `reuniao_<ts>` unchanged (the ata step names it from the
  calendar).

## Testing

- `test_recorder.py`: Chrome idle + in-call lsof fixtures; combined detection;
  start() falls back when the helper is missing/fails (mock the subprocess
  boundary only).
- Manual acceptance (after merge, on the first real meeting — see item 3 below): one real Teams desktop call and
  one real Meet call in Chrome, with wired headphones and Teams/Chrome speaker
  left on `External Headphones`; WAV must contain both the user's voice and the
  other participants; after the call, `Gravar Reunião (Tap)` must not remain
  in Audio MIDI Setup. Also one forced-fallback run (helper renamed).

## Permissions / ops

- First tap use prompts "System Audio Recording" (TCC) for the process that
  owns the tap. Under the LaunchAgent the prompt may not show; the builder
  must verify how TCC attributes it (helper binary vs python) and document the
  one-time grant step in README.
- Rollout: merge to `main`, `git pull` in `~/Audio-MCP-Server`, build helper,
  `launchctl kickstart -k gui/$(id -u)/com.monkai.meeting-watcher`.
- Note: the main checkout has uncommitted local edits in `recorder.py`/
  `watcher.py` (`CALL_CHECK_SECONDS = 5`, watcher tweaks) not on `origin/main`;
  they must survive the pull (patch-and-reapply as done for PR #4).

## Resolved after builder check (Claude's calls unless noted)

1. **Dead/silent tap.** No mid-recording switch to BlackHole (too complex
   for the gain). `_record()` tracks the peak of the tap channels separately
   from the mic channel; `status()` exposes `tap_peak`. If the whole recording
   ran on the tap path and `tap_peak` stayed ~0 while the transcript has
   speech, the watcher notifies "áudio da call não foi capturado — confira a
   permissão Gravação de Áudio do Sistema" and logs an error. A silent call
   side for the whole meeting is not a normal case, so this signal is honest.
2. **Chrome in-call count.** lsof/UDP stays the first choice. If the real
   Meet measurement shows 0 or overlaps idle traffic, the builder may switch
   the Chrome signal to the Core Audio process-object
   `kAudioProcessPropertyIsRunningInput` (Chrome using the mic), queried by
   the helper (`tap-capture --probe`). Either signal is acceptable; the
   no-speech guard covers false starts.
3. **Real-call validation (decided by Arthur, 2026-11-07):** build and merge
   validated with simulated data (lsof fixtures, mocked helper boundary,
   a local tap smoke against a process playing a test tone); the real Teams +
   Meet acceptance happens on Arthur's first real meeting after rollout.
   Accepted risk: that first meeting may lose call audio. Mitigation: the
   silent-tap notification (item 1) and the BlackHole fallback stay in place;
   the Chrome in-call socket fixture is captured from that first meeting.
4. **Tap timing.** The tap is created lazily, only after a call is detected
   (in `_run()`, after the waiting loop), so the target processes already
   have audio objects. Helper still re-scans every few seconds for new
   Chrome/Teams audio processes; if updating a live tap is not feasible,
   documented limitation.
5. **Mic.** Default input at tap creation; changing input mid-call is a
   known, accepted limitation (logged in README).
6. **Main checkout local edits** (measured): `recorder.py`
   `CALL_CHECK_SECONDS = 5` (from 10, comment ~15s); `watcher.py` only the ata
   prompt/allowedTools (HTML attachment). They do not touch detection; reapply
   by patch after pull.
7. **MCP path.** Rename to `is_in_call()` (Teams OR Chrome) and switch every
   caller (watcher, `_run()`, `audio_server.py` docs/error strings), so MCP
   and watcher behave the same.

## Left to the builder

- Exact Core Audio API sequence, sample rate handling (keep 44.1 kHz on the
  Python side if the aggregate allows, else read the device rate).
- Helper build/staleness check mechanics.
- Mixdown details of tap channels + mic in `_record()` (current mono mean is
  acceptable).
