#!/usr/bin/env python3
"""Background watcher: auto-record Teams meetings (Granola-style).

Polls for an active Teams call; when one starts and nothing else is recording
(recorder lock), records it and shows a macOS notification. Runs as a
LaunchAgent (com.monkai.meeting-watcher).
"""
import datetime
import logging
import subprocess
import time

import recorder

POLL_SECONDS = 10

logger = logging.getLogger("watcher")


def notify(message: str) -> None:
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{message}" with title "Gravador de Reunião"'],
        check=False, capture_output=True,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.info("meeting watcher started (poll every %ss)", POLL_SECONDS)
    while True:
        try:
            if not recorder.LOCK_FILE.exists() and recorder.is_teams_in_call():
                name = "reuniao_" + datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
                rec = recorder.MeetingRecorder(name=name, auto_stop=True)
                error = rec.start()
                if error:
                    logger.error("could not start recording: %s", error)
                    notify(f"Erro ao gravar reunião: {error[:80]}")
                    time.sleep(60)  # don't spam on persistent config errors
                    continue
                logger.info("recording started: %s", rec.wav_path)
                notify("Reunião do Teams detectada — gravando…")
                rec.join()  # recording auto-stops when the call ends
                st = rec.status()
                logger.info("recording finished: %s", st)
                if st["state"] == "done" and st["seconds"] > 30:
                    notify(f"Gravação salva ({st['seconds'] / 60:.0f} min) — "
                           "peça a ata no Claude")
                elif st["state"] == "error":
                    notify(f"Gravação falhou: {(st['error'] or '')[:80]}")
        except Exception:
            logger.exception("watcher loop error")
            time.sleep(60)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
