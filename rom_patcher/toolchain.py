"""Shared platform names and locations for the native tools."""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import TOOLS_DIR

BUILD_DIRECTORY = "build"

MACOS_ARCHITECTURES = ("arm64", "x86_64")


@dataclass(frozen=True)
class PlatformTools:
    folder: str
    native_library: str
    patcher: str = "patcher.py"

    @property
    def release_label(self) -> str:
        """Name the folder shared by every architecture of one system."""
        return self.folder.split("/")[0]


def get_platform_tools(
    system: str | None = None, machine: str | None = None
) -> PlatformTools:
    system = system or sys.platform
    machine = (machine or platform.machine()).casefold()
    architecture = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }.get(machine, machine)

    if system == "darwin" and architecture in MACOS_ARCHITECTURES:
        return PlatformTools(f"macos/{architecture}", "liblegacy_lz11.dylib")
    if architecture == "x86_64":
        if system == "win32":
            return PlatformTools("windows-x86_64", "legacy_lz11.dll")
        if system.startswith("linux"):
            return PlatformTools("linux-x86_64", "liblegacy_lz11.so")
    raise RuntimeError(
        "Builds support Windows and Linux x86_64, and macOS arm64 or x86_64."
    )


def tools_root(resources: Path) -> Path:
    if getattr(sys, "frozen", False):
        return resources / TOOLS_DIR
    return resources / BUILD_DIRECTORY / TOOLS_DIR


def tool_cache_root() -> Path:
    if sys.platform == "win32":
        folder = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    elif sys.platform == "darwin":
        folder = Path.home() / "Library" / "Caches"
    else:
        folder = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return folder / "3ds-rom-patcher"
