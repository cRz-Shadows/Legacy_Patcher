"""Shared repository and generated-output locations."""

from pathlib import Path

from rom_patcher.config import TOOLS_DIR
from rom_patcher.toolchain import BUILD_DIRECTORY

SCRIPTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_ROOT.parent
BUILD_ROOT = PROJECT_ROOT / BUILD_DIRECTORY
TOOLS_ROOT = BUILD_ROOT / TOOLS_DIR


def source_files() -> list[Path]:
    files = list(PROJECT_ROOT.glob("*.py"))
    files.extend(
        PROJECT_ROOT / name
        for name in (".gitignore", "README.md", "requirements.txt", "LICENSE")
    )
    for folder in (PROJECT_ROOT / "rom_patcher", SCRIPTS_ROOT):
        files.extend(
            path
            for path in folder.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    return sorted(files)
