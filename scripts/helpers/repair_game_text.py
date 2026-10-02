"""Run the validated Game Text repair independently."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rom_patcher.cli import run_repair

if __name__ == "__main__":
    raise SystemExit(run_repair())
