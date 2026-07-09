#!/usr/bin/env python3
"""Background watcher: auto-record Teams meetings (Granola-style).

Polls for an active Teams call; when one starts and nothing else is recording
(recorder lock), records it and shows a macOS notification. Runs as a
LaunchAgent (com.monkai.meeting-watcher).
"""
import datetime
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import recorder

POLL_SECONDS = 10
VENV_PYTHON = "/Users/arthurvaz/Audio-MCP-Server/.venv/bin/python3"
CLAUDE_BIN = "/Users/arthurvaz/.local/bin/claude"
ATAS_DIR = "/Users/arthurvaz/Desktop/Monkai/Assistente/data/atas"
EMAIL_TO = "arthur.vaz@monkai.com.br"
ATAS_MEMORY = "/Users/arthurvaz/Desktop/Monkai/Assistente/scripts/atas_memory.py"
ASSISTENTE_PY = "/Users/arthurvaz/Desktop/Monkai/Assistente/.venv/bin/python3"
MIN_UNIQUE_WORDS = 30  # measured: real meetings 252-1368 unique words, blips 4-6

logger = logging.getLogger("watcher")


def notify(message: str, subtitle: str = "", open_path: str = "") -> None:
    """Notification via terminal-notifier (own app identity — allow it in
    Focus/DND to see banners during meetings; click opens the recordings
    folder). Falls back to osascript if terminal-notifier is missing."""
    cmd = ["terminal-notifier", "-title", "Gravador de Reunião",
           "-message", message, "-sound", "default"]
    if subtitle:
        cmd += ["-subtitle", subtitle]
    if open_path:
        cmd += ["-open", f"file://{open_path}"]
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=10)
    except FileNotFoundError:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "Gravador de Reunião"'],
            check=False, capture_output=True,
        )


def transcribe(wav_path: str) -> str | None:
    """Transcribe locally in a subprocess (frees the ~1.5 GB model RAM on exit;
    no MCP tool timeout). Returns transcript path or None."""
    transcript_path = wav_path.rsplit(".", 1)[0] + "_transcript.txt"
    code = (
        "import mlx_whisper, sys\n"
        "r = mlx_whisper.transcribe(sys.argv[1], "
        "path_or_hf_repo='mlx-community/whisper-large-v3-turbo', language='pt')\n"
        "lines = [f\"[{int(s['start']//60):02d}:{int(s['start']%60):02d}] "
        "{s['text'].strip()}\" for s in r['segments']]\n"
        "open(sys.argv[2], 'w', encoding='utf-8').write('\\n'.join(lines))\n"
    )
    result = subprocess.run([VENV_PYTHON, "-c", code, wav_path, transcript_path],
                            capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        logger.error("transcription failed: %s", result.stderr[-500:])
        return None
    return transcript_path


def generate_ata(transcript_path: str, wav_name: str, minutes: float) -> str | None:
    """Headless Claude: name the meeting from the ms365 calendar, write the
    ata (with YAML frontmatter) and email it. Returns the ata file path on
    success, or None on failure."""
    prompt = f"""Voce e o assistente do Arthur. Uma reuniao acabou de ser gravada e transcrita automaticamente.

Transcript: {transcript_path} (gravacao {wav_name}, {minutes:.0f} min — o timestamp YYYYMMDD_HHMMSS no nome do arquivo e o inicio da gravacao).

Faca, sem pedir confirmacao:
1. Leia o transcript.
2. Busque no calendario ms365 (mcp__ms365__get-calendar-view) o evento que cobre o horario da gravacao; se achar, use o titulo dele como nome da reuniao; senao use "Reuniao" + data/hora.
3. Escreva a ata em portugues e salve em {ATAS_DIR}/YYYY-MM-DD_<nome-curto>.md (nome-curto em kebab-case, SEM espacos), comecando OBRIGATORIAMENTE com frontmatter YAML:
---
titulo: <titulo do evento ou "Reuniao">
data: <YYYY-MM-DD HH:MM do inicio da gravacao>
cliente: <vivo|nestle|unidas|interno|outro>
produto: <produto MonkAI relacionado ou "geral">
participantes: [<nomes identificaveis no transcript>]
tags: [<3-8 tags do assunto>]
---
Depois do frontmatter: data/hora, resumo, pontos discutidos, decisoes, action items com responsaveis, proximos passos.
4. Envie a ata por email para {EMAIL_TO} via mcp__ms365__send-mail — assunto "Ata de Reuniao - <nome> - DD/MM/AAAA", corpo HTML bem formatado.
5. Responda APENAS com o caminho absoluto do arquivo da ata salvo (nada mais)."""
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--allowedTools",
             "Read,Write,mcp__ms365__get-calendar-view,mcp__ms365__send-mail"],
            capture_output=True, text=True, timeout=900,
            cwd="/Users/arthurvaz",  # projeto onde o MCP ms365 esta configurado
            env={**os.environ, "PATH": os.environ.get("PATH", "") + ":/Users/arthurvaz/.local/bin"},
        )
        logger.info("claude ata run (rc=%d): %s", result.returncode, result.stdout[-300:])
        if result.returncode != 0:
            return None
        m = re.search(r"(/\S+\.md)", result.stdout)
        return m.group(1) if m else None
    except subprocess.TimeoutExpired:
        logger.error("claude ata run timed out")
        return None


def has_speech(transcript_path: str) -> bool:
    """True when the transcript holds real dialogue rather than silence.

    Whisper hallucinates filler on a silent track ('Abertura', 'Fim', empty
    segments), so neither line count nor duration proves speech — vocabulary
    size does. A meeting that never happened repeats a handful of words.
    """
    words: set[str] = set()
    for line in Path(transcript_path).read_text(encoding="utf-8").splitlines():
        text = re.sub(r"^\[\d+:\d+\]\s*", "", line).strip()
        words.update(w.lower() for w in text.split())
    return len(words) >= MIN_UNIQUE_WORDS


def process_recording(wav_path: str, seconds: float) -> None:
    """Post-meeting pipeline: transcribe -> delete audio -> ata + email."""
    transcript_path = transcribe(wav_path)
    if not transcript_path:
        notify("Falha na transcrição — áudio mantido em ~/Recordings",
               open_path=str(recorder.RECORDINGS_DIR))
        return
    os.unlink(wav_path)  # transcript is the artifact; meeting audio is sensitive
    logger.info("transcribed -> %s (audio deleted)", transcript_path)
    if not has_speech(transcript_path):
        # Teams opens the same media sockets when merely gathering ICE candidates
        # (pre-join, ringing, device test), so the recorder cannot tell those from
        # a real call. Silence is the only honest proof: drop it, no ata, no email.
        logger.info("no speech in %s — discarded", transcript_path)
        os.unlink(transcript_path)
        return
    ata_path = generate_ata(transcript_path, Path(wav_path).name, seconds / 60)
    if ata_path:
        try:
            subprocess.run([ASSISTENTE_PY, ATAS_MEMORY, "ingest", ata_path],
                           capture_output=True, text=True, timeout=120, check=True)
            logger.info("ata indexed in grkmemory: %s", ata_path)
        except Exception:
            logger.exception("ata memory ingest failed (reindex later recovers)")
        notify(f"Reunião concluída ({seconds / 60:.0f} min) — ata enviada por email ✉️",
               subtitle=EMAIL_TO, open_path=ATAS_DIR)
    else:
        notify("Transcrição pronta, mas a ata/email falhou — peça a ata no Claude",
               subtitle=Path(transcript_path).name,
               open_path=str(recorder.RECORDINGS_DIR))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.info("meeting watcher started (poll every %ss)", POLL_SECONDS)
    positives = 0  # consecutive polls seeing a call — Teams blips open sockets
    while True:    # for ~10s without a real call; require 2 (~20s) to start
        try:
            if recorder.LOCK_FILE.exists() or not recorder.is_teams_in_call():
                positives = 0
            else:
                positives += 1
            if positives >= 2:
                positives = 0
                name = "reuniao_" + datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
                rec = recorder.MeetingRecorder(name=name, auto_stop=True)
                error = rec.start()
                if error:
                    logger.error("could not start recording: %s", error)
                    notify(f"Erro ao gravar reunião: {error[:80]}")
                    time.sleep(60)  # don't spam on persistent config errors
                    continue
                logger.info("recording started: %s", rec.wav_path)
                notify(f"Gravando: {rec.wav_path.name}",
                       subtitle="Salvando em ~/Recordings (clique para abrir)",
                       open_path=str(recorder.RECORDINGS_DIR))
                rec.join()  # recording auto-stops when the call ends
                st = rec.status()
                logger.info("recording finished: %s", st)
                if st["state"] == "done" and st["seconds"] > 60:
                    # ponytail: pipeline inline — watcher pausa o polling durante
                    # transcricao/ata (reunioes simultaneas nao existem p/ 1 pessoa)
                    process_recording(str(rec.wav_path), st["seconds"])
                elif st["state"] == "done":
                    # blip/entrada-e-saida: sem conteudo de reuniao — descartar quieto
                    logger.info("short recording (%.0fs) discarded", st["seconds"])
                    os.unlink(rec.wav_path)
                elif st["state"] == "error":
                    notify(f"Gravação falhou: {(st['error'] or '')[:80]}")
        except Exception:
            logger.exception("watcher loop error")
            time.sleep(60)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
