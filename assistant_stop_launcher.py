"""Windowless Desktop Stop wrapper; termination stays in the owned CLI path."""

import ctypes
from pathlib import Path
import subprocess
import sys

NO_SERVER_TO_STOP_MESSAGE = "PDF Assistant is already stopped. Nothing to stop."


def show_info(message):
    ctypes.windll.user32.MessageBoxW(None, str(message), "PDF Assistant", 0x40)


def show_failure(message):
    ctypes.windll.user32.MessageBoxW(
        None, str(message), "Could not stop PDF Assistant", 0x10,
    )


def stop():
    try:
        result = subprocess.run(
            [str(Path(sys.executable).resolve().with_name("python.exe")),
             "-m", "anythingllm_pdf_assistant_cli", "stop"],
            cwd=str(Path(__file__).resolve().parent),
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True, text=True,
        )
    except OSError as exc:
        show_failure(f"Could not launch the Stop command: {exc}")
        return 1
    if result.returncode:
        show_failure((result.stderr or result.stdout or "Stop did not complete. Please retry.").strip()[-2000:])
    elif NO_SERVER_TO_STOP_MESSAGE in result.stdout:
        show_info(NO_SERVER_TO_STOP_MESSAGE)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(stop())
