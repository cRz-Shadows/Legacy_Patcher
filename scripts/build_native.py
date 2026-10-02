"""Compile the LZ11 helper into the generated native-tool folder."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rom_patcher.native import load_native_library
from rom_patcher.toolchain import PlatformTools, get_platform_tools
from scripts.paths import SCRIPTS_ROOT, TOOLS_ROOT

SOURCE = SCRIPTS_ROOT / "native" / "legacy_lz11.c"


@dataclass(frozen=True)
class Compiler:
    executable: str
    environment: dict[str, str] | None = None

    @property
    def is_msvc(self) -> bool:
        return Path(self.executable).name.casefold() in {"cl", "cl.exe"}


def find_msvc() -> Compiler | None:
    existing = shutil.which("cl")
    target_architecture = os.environ.get("VSCMD_ARG_TGT_ARCH", "").casefold()
    if existing and target_architecture == "x64":
        return Compiler(existing)

    locator = shutil.which("vswhere.exe")
    if not locator:
        program_files = os.environ.get("ProgramFiles(x86)")
        if program_files:
            path = (
                Path(program_files)
                / "Microsoft Visual Studio"
                / "Installer"
                / "vswhere.exe"
            )
            if path.is_file():
                locator = str(path)
    if not locator:
        return Compiler(existing) if existing else None

    result = subprocess.run(
        [
            locator,
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
            "-utf8",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8-sig",
    )
    installation = result.stdout.strip()
    if not installation:
        return None
    setup = Path(installation) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not setup.is_file():
        return None

    # Unicode output preserves paths from localized Windows installations.
    command_shell = os.environ.get("COMSPEC", "cmd.exe")
    command = f'"{command_shell}" /d /u /s /c "call "{setup}" >nul && set"'
    result = subprocess.run(
        command,
        executable=command_shell,
        check=True,
        capture_output=True,
        encoding="utf-16-le",
    )
    environment = dict(os.environ)
    for line in result.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator and name:
            environment[name.upper()] = value
    executable = shutil.which("cl", path=environment.get("PATH"))
    return Compiler(executable, environment) if executable else None


def find_compiler() -> Compiler:
    selected = os.environ.get("CC")
    if selected:
        executable = shutil.which(selected)
        if not executable:
            raise RuntimeError("CC must name an available C compiler.")
        return Compiler(executable)
    if os.name == "nt":
        compiler = find_msvc()
        if compiler:
            return compiler
    for name in ("clang", "gcc", "cc"):
        executable = shutil.which(name)
        if executable:
            return Compiler(executable)
    raise RuntimeError(
        "No C compiler was found. Install the compiler listed for your "
        "system in README.md, or set CC to its executable."
    )


def compile_command(
    compiler: Compiler, output: Path, target: PlatformTools
) -> list[str]:
    if compiler.is_msvc:
        return [
            compiler.executable,
            "/nologo",
            "/O2",
            "/MT",
            "/LD",
            "/std:c11",
            str(SOURCE),
            "/link",
            "/OUT:" + str(output),
        ]
    command = [compiler.executable, "-O3", "-std=c99", "-Wall", "-Wextra"]
    if os.name != "nt":
        command.append("-fPIC")
    command.append("-dynamiclib" if sys.platform == "darwin" else "-shared")
    if sys.platform == "darwin":
        command.extend(("-arch", target.architecture))
    command.extend(("-o", str(output), str(SOURCE)))
    return command


def build_native(targets: list[PlatformTools]) -> None:
    compiler = find_compiler()
    current = get_platform_tools()
    for target in targets:
        folder = TOOLS_ROOT / target.folder
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / target.native_library
        print(f"Compiling LZ11 for {target.release_label}...", flush=True)
        # Isolate compiler sidecars and replace the library after linking.
        with tempfile.TemporaryDirectory(dir=folder) as temporary:
            working = Path(temporary)
            staged = working / target.native_library
            subprocess.run(
                compile_command(compiler, staged, target),
                cwd=working,
                env=compiler.environment,
                check=True,
            )
            os.replace(staged, output)
        if target == current:
            load_native_library(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        build_native([get_platform_tools()])
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Native build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
