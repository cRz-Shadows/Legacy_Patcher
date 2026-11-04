"""Prepare dependencies and build the patcher and maker for this system."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import sysconfig
import venv
import zipfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rom_patcher.toolchain import (
    MACOS_ARCHITECTURES,
    PlatformTools,
    get_platform_tools,
)
from scripts.build_native import build_native
from scripts.paths import BUILD_ROOT, PROJECT_ROOT, SCRIPTS_ROOT

SINGLE_ARCHITECTURE_PACKAGES = (
    "from importlib.metadata import distributions\n"
    "for package in distributions():\n"
    "    tags = (package.read_text('WHEEL') or '').split()\n"
    "    if any(tag.endswith(('_arm64', '_x86_64')) for tag in tags):\n"
    "        print(f'{package.name}=={package.version}')\n"
)


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def interpreter_platform(python: Path) -> str:
    result = subprocess.run(
        [
            str(python),
            "-c",
            "import sysconfig; print(sysconfig.get_platform())",
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def environment_python() -> Path:
    environment = BUILD_ROOT / ".venv"
    python = environment / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    if (
        not python.is_file()
        or interpreter_platform(python) != sysconfig.get_platform()
    ):
        print("Creating the build environment...", flush=True)
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
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


def build_universal2_packages(python: Path) -> None:
    """
    Rebuild single-architecture packages from source as universal2.

    pip prefers single-architecture wheels, and some packages publish no
    universal2 wheel, so PyInstaller would otherwise reject them.
    """
    packages = subprocess.run(
        [str(python), "-c", SINGLE_ARCHITECTURE_PACKAGES],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not packages:
        return
    print(f"Building universal2 packages: {', '.join(packages)}", flush=True)
    names = ",".join(package.partition("==")[0] for package in packages)
    os.environ["ARCHFLAGS"] = " ".join(
        f"-arch {architecture}" for architecture in MACOS_ARCHITECTURES
    )
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--force-reinstall",
            "--no-deps",
            f"--no-binary={names}",
            *packages,
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
    universal2 = sysconfig.get_platform().endswith("universal2")
    if sys.platform == "darwin" and not universal2:
        parser.error("On macOS, use the python.org Python shown in README.md")
    try:
        temporary = BUILD_ROOT / "tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        for name in ("TMPDIR", "TEMP", "TMP"):
            os.environ[name] = str(temporary)
        target = get_platform_tools()
        python = environment_python()
        install_requirements(python)
        if sys.platform == "darwin":
            build_universal2_packages(python)
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
