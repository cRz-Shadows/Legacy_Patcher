"""Run the optional direct ROM builder."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rom_patcher.cli import run_direct_builder

if __name__ == "__main__":
    raise SystemExit(run_direct_builder())
