# Process-tap capture + Chrome call detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record Teams-app and Chrome (Meet / Teams web) calls from the app processes themselves, so recording works with any speaker/headphones, with the BlackHole path as fallback.

**Architecture:** A Swift CLI (`tap/tap-capture`) creates a public aggregate device `Gravar Reunião (Tap)` = Core Audio process tap of Teams+Chrome + default mic, and keeps it alive until killed. `recorder.py` starts it lazily once a call is detected, records from it with the existing `_record()` loop, and falls back to the current Multi-Output/BlackHole path on any helper failure. Call detection becomes `is_in_call()` = Teams OR Chrome media sockets.

**Tech Stack:** Python 3 (`sounddevice`, `numpy`, stdlib), Swift 6.2 via `swiftc` (CoreAudio `CATapDescription`, `AudioHardwareCreateProcessTap`, `AudioHardwareCreateAggregateDevice`), macOS 26.5.

**Spec:** `docs/superpowers/specs/2026-11-07-process-tap-capture-design.md`

## Global Constraints

- Tests are plain assert scripts run with `/Users/arthurvaz/Audio-MCP-Server/.venv/bin/python3 <file>` (no pytest in this repo); keep that style.
- Mock only the boundary (subprocess / helper process / sounddevice), never the code under test.
- Never `except Exception: pass`; log with `logger.exception` and keep going only where the spec says fallback.
- Aggregate device name exactly `Gravar Reunião (Tap)`; fallback devices exactly `Multi-Output sem Fone`, `Multi-Output Fone`, `Gravar Reunião`.
- Target bundle IDs exactly `com.microsoft.teams2`, `com.google.Chrome`.
- The tap must not mute the apps (user keeps hearing the call) and must never change the system output device.
- Helper binary `tap/tap-capture` is git-ignored; source `tap/main.swift` is committed.
- Worktree is isolated: Bash must be simple single commands; no `git -C` to the main checkout.

## Review Focus

1. Helper crashes or is SIGKILLed mid-call → `Gravar Reunião (Tap)` must not linger: next helper start removes a stale aggregate with that name (test: Task 2 smoke step "stale device").
2. Tap created but permission missing → silence on tap channels; user must get the silent-tap notification, not a mic-only ata with no warning (test: Task 4 `test_silent_tap_notifies`).
3. Helper never prints `READY` (hang) → recorder must time out (`TAP_READY_TIMEOUT = 10` s), kill it and fall back (test: Task 3 `test_tap_timeout_falls_back`).
4. Call detected on Chrome while Teams is idle, and the reverse → both must start/stop recording (test: Task 1 `IN_CALL` combined assertions).
5. Fallback path still restores the user's original output after the call; tap path never touches it (test: Task 3 `test_tap_path_does_not_switch_output`).

---

### Task 1: Chrome call detection + `is_in_call()`

**Files:**
- Modify: `recorder.py:33-82` (detection section), `recorder.py:239-300` (`_run`, `_record` callers)
- Modify: `watcher.py:4` (docstring), `watcher.py:181`
- Modify: `audio_server.py:135-170` (docstring/mode strings: "Teams or Chrome call")
- Test: `test_recorder.py`

**Interfaces:**
- Produces: `count_app_media_sockets(app_path_pattern: str) -> int`; `count_call_media_sockets() -> int` (Teams + Chrome); `is_in_call() -> bool`. `count_teams_media_sockets` and `is_teams_in_call` are deleted (no aliases).
- Constants: `CALL_APPS = ("Microsoft Teams.app", "Google Chrome.app")`.

- [ ] **Step 1: Write the failing tests** (append to `test_recorder.py`)

```python
import recorder
# Real Chrome idle capture 2026-11-07: 8 sockets, all mDNS.
CHROME_IDLE = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n" + \
    "Google 1330 arthurvaz 40u IPv4 0x1 0t0 UDP *:5353\n" * 8
assert recorder.count_media_sockets_from_lsof(CHROME_IDLE) == 0

calls = {}
def fake_probe(pattern):  # boundary: per-app lsof probe
    return calls.get(pattern, 0)
recorder.count_app_media_sockets = fake_probe
calls = {"Microsoft Teams.app": 0, "Google Chrome.app": 3}
assert recorder.is_in_call(), "Chrome-only call"
calls = {"Microsoft Teams.app": 4, "Google Chrome.app": 0}
assert recorder.is_in_call(), "Teams-only call"
calls = {}
assert not recorder.is_in_call(), "idle"
assert recorder.count_call_media_sockets() == 0
print("all call-detection tests passed")
```
(Restore `recorder.count_app_media_sockets` afterwards if later tests need the real one.)

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python3 test_recorder.py` → `AttributeError: ... is_in_call`.
- [ ] **Step 3: Implement** in `recorder.py`: generalize the Teams probe body into `count_app_media_sockets(pattern)` (`pgrep -f pattern` → `lsof -nP -iUDP -a -p` → parser); `count_call_media_sockets()` sums over `CALL_APPS` looking the function up at call time (module attribute) so the test monkeypatch works; `is_in_call()`. Replace every caller (`_run` wait loop and error text "No Teams or Chrome call detected…", `_record` stop probe, `watcher.py:181`). Update the module docstrings and `audio_server.py` strings.
- [ ] **Step 4: Run** `test_recorder.py` and `test_watcher.py` → all "passed" lines. `grep -n "teams_in_call\|count_teams" *.py` → no output.
- [ ] **Step 5: Commit** `feat: detect calls in Chrome as well as Teams`.

### Task 2: Swift helper `tap-capture`

**Files:**
- Create: `tap/main.swift`, `tap/build.sh` (one line: `swiftc -O -o "$(dirname "$0")/tap-capture" "$(dirname "$0")/main.swift"`)
- Modify: `.gitignore` (add `tap/tap-capture`)

**Interfaces:**
- Produces CLI: `tap/tap-capture` (no args). Stdout protocol, one line each, flushed:
  - `READY tap_channels=<first>:<count> total=<n>` when the aggregate is usable (channel indices are 0-based into the aggregate's input channels).
  - `ERROR <message>` then exit code 1 on any failure (no target process, tap/aggregate creation error).
- Lifetime: exits cleanly (destroying aggregate + tap) on SIGTERM, SIGINT or stdin EOF.

- [ ] **Step 1: Implement `main.swift`**: translate bundle IDs to Core Audio process objects (`kAudioHardwarePropertyProcessObjectList` + `kAudioProcessPropertyBundleID`, prefix match so Chrome/Teams helper processes count); if none → `ERROR no target audio process`. Remove any existing device named `Gravar Reunião (Tap)` first. `CATapDescription(stereoMixdownOfProcesses:)`, `muteBehavior = .unmuted`; `AudioHardwareCreateProcessTap`; aggregate dict with name `Gravar Reunião (Tap)`, `kAudioAggregateDeviceIsPrivateKey: false`, sub-device = default input device UID, tap list = tap UUID with drift compensation on. Every 5 s re-scan processes and, if the set grew, update the tap description (`kAudioTapPropertyDescription`); if that set fails, log to stderr and continue (spec item 4, documented limitation). Compute tap channel range from the aggregate's input stream configuration and print `READY`.
- [ ] **Step 2: Build** — `bash tap/build.sh` → exit 0, `tap/tap-capture` exists.
- [ ] **Step 3: Smoke (simulated call)** — with Chrome playing audio in a tab (e.g. any local test tone page) run `tap/tap-capture` in background, then `.venv/bin/python3 -c` snippet that opens `Gravar Reunião (Tap)` with `sounddevice` for 3 s and prints per-channel peak. Expected: `READY …`, tap channels peak > 0.01 while system output is untouched (`SwitchAudioSource -c -t output` unchanged). First run triggers the "System Audio Recording" prompt: Arthur must click Allow (record in README, Task 4).
- [ ] **Step 4: Stale device** — `kill -9` the helper, run it again → still `READY`, and `SwitchAudioSource -a -t input` lists exactly one `Gravar Reunião (Tap)`; after a normal SIGTERM it lists none.
- [ ] **Step 5: Commit** `feat: add Core Audio process-tap helper`.

### Task 3: Recorder uses the tap, BlackHole as fallback

**Files:**
- Modify: `recorder.py` (`MeetingRecorder.start`, `_run`, `_record`, `status`; new helpers)
- Test: `test_recorder.py`

**Interfaces:**
- Consumes: `is_in_call()` (Task 1); helper CLI protocol (Task 2).
- Produces:
  - `TAP_HELPER = Path(__file__).parent / "tap" / "tap-capture"`, `TAP_DEVICE = "Gravar Reunião (Tap)"`, `TAP_READY_TIMEOUT = 10`.
  - `ensure_tap_helper() -> Path | None` — runs `tap/build.sh` if the binary is missing or older than `main.swift`; `None` on build failure (logged).
  - `parse_ready(line: str) -> tuple[int, int] | None` — `"READY tap_channels=2:2 total=3"` → `(2, 2)`; anything else → `None`.
  - `start_tap() -> tuple[subprocess.Popen, tuple[int, int]] | None` — spawn, wait ≤ `TAP_READY_TIMEOUT` for READY, else kill + `None` (reason logged).
  - `MeetingRecorder.capture: str` = `"tap"` | `"blackhole"`; `status()` adds `"capture"` and `"tap_peak"` (float, max abs over tap channels; `None` on blackhole path).
- Behavior change: `start()` only checks the lock and spawns the thread (device choice moves into `_run()` after the call is detected — spec item 4). Fallback = the current code path (`pick_multi_output` + switch + `find_recording_device()` for `Gravar Reunião`/BlackHole); output restore only when fallback switched it. `_run()` finally: terminate helper (SIGTERM, wait 5 s, then kill) before `release_lock()`.

- [ ] **Step 1: Write failing tests**

```python
from recorder import parse_ready
assert parse_ready("READY tap_channels=1:2 total=3") == (1, 2)
assert parse_ready("ERROR no target audio process") is None
assert parse_ready("") is None

# Fallback + routing tests: monkeypatch recorder.start_tap, recorder.switch_audio_output,
# recorder.get_current_output, recorder.find_recording_device and a fake sd.InputStream
# that yields 3-channel blocks; run MeetingRecorder(auto_stop=False, max_seconds=0.5).
# test_tap_path_does_not_switch_output: start_tap returns (FakePopen, (1, 2)) ->
#   capture == "tap", switch_audio_output never called, FakePopen.terminate called, tap_peak > 0
#   when the fake stream's channels 1-2 carry 0.5.
# test_tap_timeout_falls_back: start_tap returns None -> capture == "blackhole",
#   switch_audio_output called with pick_multi_output(...), then restored to the original.
# test_tap_silence_peak_zero: fake stream channels 1-2 all zeros -> tap_peak == 0.0.
print("all capture tests passed")
```

- [ ] **Step 2: Run, expect FAIL** (`ImportError: parse_ready`).
- [ ] **Step 3: Implement** the interfaces above. `_record()` takes an optional `tap_channels: tuple[int, int] | None` and updates `self.tap_peak` per block from `block[:, first:first+count]`.
- [ ] **Step 4: Run** `test_recorder.py`, `test_watcher.py` → all "passed".
- [ ] **Step 5: Real smoke** — `.venv/bin/python3 -c "import recorder,time; r=recorder.MeetingRecorder(name='smoke', auto_stop=False, max_seconds=5); print(r.start()); r.join(); print(r.status())"` with Chrome playing audio → `capture: 'tap'`, `tap_peak > 0`, WAV exists; then rename `tap/tap-capture` temporarily → `capture: 'blackhole'`. Delete the smoke WAVs.
- [ ] **Step 6: Commit** `feat: record from process tap, keep BlackHole as fallback`.

### Task 4: Silent-tap notification + docs

**Files:**
- Modify: `watcher.py` (`process_recording` signature and the call site after `rec.join()`)
- Modify: `README.md` (headphones section → new "Meeting capture" section)
- Test: `test_watcher.py`

**Interfaces:**
- Consumes: `status()["capture"]`, `status()["tap_peak"]` (Task 3).
- Produces: `TAP_SILENT_PEAK = 0.001`; `tap_was_silent(st: dict) -> bool` (True iff `capture == "tap"` and `tap_peak is not None and tap_peak < TAP_SILENT_PEAK`). `process_recording(wav_path, seconds, tap_silent: bool = False)`: when `tap_silent` and the transcript has speech, `notify("Áudio da call não foi capturado — confira a permissão Gravação de Áudio do Sistema")` and `logger.error`, then continue the normal ata pipeline (mic-side ata still useful).

- [ ] **Step 1: Failing tests** in `test_watcher.py`:

```python
from watcher import tap_was_silent
assert tap_was_silent({"capture": "tap", "tap_peak": 0.0})
assert not tap_was_silent({"capture": "tap", "tap_peak": 0.2})
assert not tap_was_silent({"capture": "blackhole", "tap_peak": None})
# test_silent_tap_notifies: monkeypatch watcher.transcribe/has_speech/generate_ata/notify,
# call process_recording(wav, 120, tap_silent=True) -> notify called with the permission message.
print("all tap-silence tests passed")
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**; watcher call site passes `tap_silent=tap_was_silent(st)`. Also notify on `st["state"] == "error"` if not already done.
- [ ] **Step 4: README**: how capture works (tap first, BlackHole fallback), build line `bash tap/build.sh`, one-time "System Audio Recording" grant (System Settings → Privacy & Security → Screen & System Audio Recording → enable `tap-capture`/python), that Teams/Chrome speaker can be any device, known limitations (mic fixed at start, Chrome captures all its audio, first real meeting = acceptance).
- [ ] **Step 5: Run** both test files → all "passed".
- [ ] **Step 6: Commit** `feat: warn when the call side was not captured`.

### Task 5: Rollout (main checkout, after PR merge)

- [ ] Open PR to `main` (body: O que / Por que / Como testar / Testes e Workflows revisados).
- [ ] After Arthur's merge: in main checkout save local patch of `recorder.py` (`CALL_CHECK_SECONDS = 5`) and `watcher.py` (ata HTML prompt), checkout those files, `git pull`, reapply patches (`git apply`), run both test files, `bash tap/build.sh`, `launchctl kickstart -k gui/$(id -u)/com.monkai.meeting-watcher`, confirm `state = running` and "meeting watcher started" in `~/Library/Logs/meeting-watcher.log`.
- [ ] Tell Arthur: Teams/Chrome speaker can go back to `External Headphones`; first meeting is the acceptance run; capture the Chrome in-call lsof output then for the fixture.
