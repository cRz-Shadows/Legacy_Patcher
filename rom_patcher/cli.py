"""Program entry points and shared game-file selection."""

from __future__ import annotations

import argparse
import multiprocessing
import struct
import sys
import tempfile
from pathlib import Path

from .config import EXHEADER_PROGRAM_ID_OFFSET, TITLE_ID_MIDDLE_LENGTH
from .identity import (
    get_xy_game,
    parse_title_id,
    parse_title_id_middle,
    read_exheader,
)
from .licenses import show_licenses
from .names import validate_game_name
from .runtime import bundle_dir, resolve_tool, select_tools_dir


def run_patcher() -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(
        description="Apply patch sets to a decrypted .3ds or .cci ROM."
    )
    add_license_argument(parser)
    arguments = parser.parse_args()
    if arguments.licenses:
        show_licenses()
        return 0
    from .patcher import main

    main()
    return 0


def add_license_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--licenses",
        action="store_true",
        help="view licenses and save bundled library sources",
    )


def extract_base(rom: Path, destination: Path) -> None:
    from .runtime import cci_has_romfs, extract_from_rom, unpack_romfs_bin

    tools = select_tools_dir(bundle_dir())
    ctrtool = resolve_tool(tools, "ctrtool")
    destination.mkdir(parents=True, exist_ok=True)
    image = extract_from_rom(ctrtool, rom, destination)
    if not image.is_file():
        if cci_has_romfs(rom):
            raise RuntimeError("ctrtool did not extract the base RomFS image.")
        return
    unpack_romfs_bin(ctrtool, image, destination / "romfs")
    image.unlink()


def repair_game_text(folder: Path, base: Path, output_folder: Path) -> None:
    program_id = struct.unpack_from(
        "<Q", read_exheader(base), EXHEADER_PROGRAM_ID_OFFSET
    )[0]
    if not get_xy_game(program_id):
        return
    from .inputs import find_text_garc

    source = find_text_garc(folder)
    if source is not None:
        from .text_repair import repair_file

        repair_file(source, output_folder / "reports" / "text_repair")


def title_id_argument(value: str) -> int | str:
    try:
        if len(value.strip()) == TITLE_ID_MIDDLE_LENGTH:
            parse_title_id_middle(value)
            return value
        return parse_title_id(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def game_name_argument(value: str) -> str:
    try:
        return validate_game_name(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def run_creator(*, direct: bool) -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(
        description=(
            "Build a ROM from a complete updated game folder."
            if direct
            else "Create patches from an updated 3DS game folder."
        )
    )
    parser.add_argument(
        "--name",
        type=game_name_argument,
        metavar="TEXT",
        help="choose a game name; otherwise ask whether to rename it",
    )
    parser.add_argument(
        "--title-id",
        type=title_id_argument,
        metavar="HEX",
        help=(
            f"choose {TITLE_ID_MIDDLE_LENGTH} middle hexadecimal digits "
            "or a full title ID; "
            "otherwise ask whether to change the base ID"
        ),
    )
    add_license_argument(parser)
    arguments = parser.parse_args()
    if arguments.licenses:
        show_licenses()
        return 0

    import tkinter as tk
    from tkinter import messagebox

    from .dependencies import require_py7zr
    from .inputs import (
        SelectionCancelled,
        select_game_folder,
        select_output_folder,
        select_rom,
        selection_folder,
    )
    from .options import select_identity_options

    root = tk.Tk()
    root.withdraw()
    try:
        if not direct:
            require_py7zr()
        folder = selection_folder()
        source = select_game_folder(folder, parent=root)
        rom = select_rom(folder, parent=root)
        destination = select_output_folder(folder, parent=root)
        with tempfile.TemporaryDirectory(prefix="GameBase_") as temporary:
            base = Path(temporary) / "base"
            print("Extracting the base ROM...", flush=True)
            extract_base(rom, base)
            options = select_identity_options(
                base,
                title_id=arguments.title_id,
                display_name=arguments.name,
                parent=root,
            )
            repair_game_text(source, base, destination)
            if direct:
                from .direct_builder import build_rom

                output = build_rom(
                    source,
                    base,
                    rom,
                    destination,
                    title_id=options.title_id,
                    display_name=options.display_name,
                )
                result = 0
            else:
                from .maker import create_patchset

                result = create_patchset(
                    source,
                    base,
                    destination,
                    title_id=options.title_id,
                    display_name=options.display_name,
                )
                output = destination
        messagebox.showinfo(
            "Complete", f"Output location:\n\n{output}", parent=root
        )
        return result
    except SelectionCancelled:
        return 0
    except (Exception, SystemExit) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        messagebox.showerror("Build failed", str(error), parent=root)
        return 1
    finally:
        root.destroy()


def run_maker() -> int:
    return run_creator(direct=False)


def run_direct_builder() -> int:
    return run_creator(direct=True)


def run_repair() -> int:
    parser = argparse.ArgumentParser(
        description="Repair pk3DS Game Text in the X/Y a/0/7/4 archive."
    )
    parser.add_argument("source", nargs="?", type=Path)
    arguments = parser.parse_args()

    import tkinter as tk
    from tkinter import messagebox

    from .inputs import (
        SelectionCancelled,
        choose_all_locations,
        find_text_garc,
        select_game_folder,
        select_output_folder,
        selection_folder,
    )
    from .text_repair import repair_file

    root = tk.Tk()
    root.withdraw()
    try:
        folder = selection_folder()
        selected = arguments.source
        if selected is None:
            source = None if choose_all_locations() else find_text_garc(folder)
            if source is None:
                selected = select_game_folder(folder, parent=root)
            else:
                selected = source
        source = find_text_garc(selected)
        if source is None:
            raise FileNotFoundError(
                "The selected folder has no a/0/7/4 archive."
            )
        destination = select_output_folder(folder, parent=root)
        repair_file(source, destination / "reports" / "text_repair")
        return 0
    except SelectionCancelled:
        return 0
    except Exception as error:
        messagebox.showerror("Text repair failed", str(error), parent=root)
        return 1
    finally:
        root.destroy()
