"""Build a complete updated game tree with the shared ROM builder."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .maker import prepare_updated_tree, write_prepared_rsf
from .names import choose_non_overwriting_output, make_output_base
from .rom_builder import capture_original_cci_card_info, run_makerom
from .runtime import (
    bundle_dir,
    ensure_build_free_space,
    resolve_tool,
    select_tools_dir,
)


def build_rom(
    source: Path,
    base: Path,
    original_rom: Path,
    output_folder: Path,
    *,
    title_id: int | None = None,
    display_name: str | None = None,
) -> Path:
    tools = select_tools_dir(bundle_dir())
    makerom = resolve_tool(tools, "makerom")
    with tempfile.TemporaryDirectory(prefix="GameBuild_") as temporary:
        work = Path(temporary)
        ensure_build_free_space(
            work.parent, output_folder, original_rom.stat().st_size
        )
        shutil.copytree(source, work, dirs_exist_ok=True)
        game = prepare_updated_tree(
            work,
            base,
            title_id=title_id,
            display_name=display_name,
        )
        rsf = work / "_prepared_build.rsf"
        write_prepared_rsf(game, rsf)
        for name in ("logo.bin", "plain.bin"):
            if (base / name).is_file() and not (work / name).is_file():
                shutil.copy2(base / name, work / name)
        output = choose_non_overwriting_output(
            output_folder,
            make_output_base(
                original_rom,
                (
                    game.identity.display_name
                    if display_name is not None
                    else None
                ),
            ),
        )
        run_makerom(
            makerom,
            work,
            rsf,
            output,
            capture_original_cci_card_info(original_rom),
        )
    return output
