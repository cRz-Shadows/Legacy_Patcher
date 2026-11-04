"""Bundle both programs with their required licenses and library sources."""

import sys
from pathlib import Path

from PyInstaller.building.api import EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all, copy_metadata

sys.path.insert(0, str(Path(SPECPATH).parent))

from rom_patcher.toolchain import get_platform_tools
from scripts.collect_licenses import (
    collect_licenses,
    read_analysis_contents,
    verify_bundled_licenses,
)
from scripts.paths import PROJECT_ROOT, TOOLS_ROOT

target = get_platform_tools()
data, binaries, hidden_imports = collect_all("py7zr")
data += copy_metadata("py7zr")
data += [
    (str(PROJECT_ROOT / "rom_patcher/toolchain.json"), "rom_patcher"),
    (str(PROJECT_ROOT / "rom_patcher/data"), "rom_patcher/data"),
]
binaries += [
    (
        str(TOOLS_ROOT / target.release_label / target.native_library),
        f"tools/{target.release_label}",
    )
]
excludes = ["numpy"]
target_arch = None
if sys.platform == "darwin":
    excludes.append("psutil")
    target_arch = "universal2"

programs = [
    (target.patcher, "legacy_patcher"),
    ("make_patches.py", "legacy_patch_maker"),
]
analyses = [
    Analysis(
        [str(PROJECT_ROOT / source)],
        pathex=[str(PROJECT_ROOT)],
        binaries=binaries,
        datas=data,
        hiddenimports=hidden_imports,
        excludes=excludes,
    )
    for source, name in programs
]
license_data = collect_licenses(read_analysis_contents(analyses))

for (source, name), analysis in zip(programs, analyses):
    executable = EXE(
        PYZ(analysis.pure),
        analysis.scripts,
        analysis.binaries,
        analysis.datas + license_data,
        name=name,
        upx=False,
        target_arch=target_arch,
        console=(
            source == "make_patches.py" or sys.platform.startswith("linux")
        ),
    )
    verify_bundled_licenses(executable.name, license_data)
