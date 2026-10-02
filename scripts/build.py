"""Prepare dependencies and build the patcher and maker for this system."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rom_patcher.toolchain import PlatformTools, get_platform_tools
from scripts.build_native import build_native
from scripts.paths import BUILD_ROOT, PROJECT_ROOT, SCRIPTS_ROOT


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def environment_python() -> Path:
    environment = BUILD_ROOT / ".venv"
    python = environment / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    if not python.is_file():
        print("Creating the build environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    return python


def install_requirements(python: Path) -> None:
    print("Installing the project dependencies...", flush=True)
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--requirement",
            str(PROJECT_ROOT / "requirements.txt"),
        ]
    )


def check_python(python: Path) -> None:
    run([str(python), "-c", "import tkinter; import py7zr; import PyInstaller"])


def check_tools() -> None:
    from rom_patcher.runtime import (
        bundle_dir,
        resolve_tool,
        select_tools_dir,
    )

    tools = select_tools_dir(bundle_dir())
    for name in ("ctrtool", "makerom"):
        resolve_tool(tools, name)


def freeze_command(
    python: Path,
    target: PlatformTools,
    destination: Path,
) -> list[str]:
    return [
        str(python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(destination),
        "--workpath",
        str(BUILD_ROOT / "work" / target.release_label),
        str(SCRIPTS_ROOT / "programs.spec"),
    ]


def build_programs(python: Path, target: PlatformTools) -> None:
    destination = PROJECT_ROOT / "dist" / target.release_label
    staging = BUILD_ROOT / "release" / target.release_label
    if staging.exists():
        shutil.rmtree(staging)
    print("Building the patcher and patch maker...", flush=True)
    run(freeze_command(python, target, staging))
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(staging, destination)
    print(f"Built programs: {destination}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="prepare source use without creating executables",
    )
    arguments = parser.parse_args()
    if sys.version_info < (3, 10) or sys.maxsize <= 2**32:
        parser.error("Use a 64-bit Python 3.10 or newer")
    try:
        temporary = BUILD_ROOT / "tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        for name in ("TMPDIR", "TEMP", "TMP"):
            os.environ[name] = str(temporary)
        target = get_platform_tools()
        python = environment_python()
        install_requirements(python)
        check_python(python)
        run(
            [
                str(python),
                "-c",
                "from rom_patcher.tool_downloads import prepare_tools\n"
                "from rom_patcher.toolchain import get_platform_tools\n"
                "from scripts.paths import BUILD_ROOT, TOOLS_ROOT\n"
                "prepare_tools([get_platform_tools()], TOOLS_ROOT, "
                "BUILD_ROOT / 'downloads')",
            ]
        )
        build_native([target])
        check_tools()
        if not arguments.setup_only:
            build_programs(python, target)
        return 0
    except (
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        subprocess.CalledProcessError,
    ) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        print("Check the prerequisites in README.md.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
