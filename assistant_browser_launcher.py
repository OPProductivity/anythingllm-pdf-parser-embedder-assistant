"""Fixed Windows URI launch action; URI contents are never executed or forwarded."""

from pathlib import Path
import subprocess
import sys


def launch():
    # pythonw runs this short-lived bridge without a flashing console. Keep
    # the actual server on python.exe, exactly as the Desktop Start shortcut.
    executable = Path(sys.executable).resolve().with_name("python.exe")
    return subprocess.Popen(
        [str(executable), "-m", "anythingllm_pdf_assistant_cli", "start", "--browser"],
        cwd=str(Path(__file__).resolve().parent),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


if __name__ == "__main__":
    # Deliberately ignore argv. The protocol cannot choose commands, PDFs,
    # shell expressions, ports, or destinations, even with a malicious URI.
    launch()
