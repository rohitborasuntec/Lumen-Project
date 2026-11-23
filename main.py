import asyncio
import os
import sys
from pathlib import Path


# `python3 main.py` can resolve to the system Python even when a venv is
# intended. Re-launch with this project's environment before importing app code.
PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / "myenv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.prefix).resolve() != VENV_DIR.resolve():
    original_args = getattr(sys, "orig_argv", [sys.executable, *sys.argv])
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *original_args[1:]])

from src.automation.lumen import Lumen


if __name__ == "__main__":
    asyncio.run(Lumen("IMAP DETAILS.xlsx").run())