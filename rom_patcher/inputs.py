"""Shared file selection for the patcher and patch maker."""

from __future__ import annotations

import sys
from pathlib import Path
from tkinter import filedialog, messagebox

from .config import ROM_EXTS
from .runtime import app_dir


class SelectionCancelled(Exception):
    """The user closed a required file-selection dialog."""


def find_roms(folder: Path) -> list[Path]:
    return sorted(
        (
            path.resolve()
            for path in folder.iterdir()
            if path.is_file() and path.suffix.casefold() in ROM_EXTS
        ),
        key=lambda path: path.name.casefold(),
    )


def choose_all_locations() -> bool:
    return sys.platform == "darwin"


def selection_folder() -> Path:
    return Path.home() if choose_all_locations() else app_dir()


def select_output_folder(folder: Path, parent=None) -> Path:
    if not choose_all_locations():
        return folder
    return select_folder(folder, "Select the output folder", parent)


def select_folder(folder: Path, title: str, parent=None) -> Path:
    selected = filedialog.askdirectory(
        title=title,
        initialdir=str(folder),
        mustexist=True,
        parent=parent,
    )
    if not selected:
        raise SelectionCancelled()
    return Path(selected).resolve()


def select_rom(folder: Path, parent=None) -> Path:
    candidates = [] if choose_all_locations() else find_roms(folder)
    if len(candidates) == 1:
        return candidates[0]

    title = (
        "Select the base ROM, several ROMs were found"
        if candidates
        else "Select a decrypted .3ds or .cci ROM"
    )
    while True:
        selected = filedialog.askopenfilename(
            title=title,
            initialdir=str(folder),
            filetypes=(
                ("Nintendo 3DS ROM", ("*.3ds", "*.cci")),
                ("All files", "*"),
            ),
            parent=parent,
        )
        if not selected:
            raise SelectionCancelled()
        candidate = Path(selected).resolve()
        if candidate.is_file() and candidate.suffix.casefold() in ROM_EXTS:
            return candidate
        messagebox.showerror(
            "Unsupported ROM",
            "Select an existing .3ds or .cci ROM.",
            parent=parent,
        )
        folder = candidate.parent


def select_game_folder(folder: Path, parent=None) -> Path:
    return select_folder(
        folder,
        "Select the updated game folder containing the RSF and game files",
        parent,
    )


def find_text_garc(folder: Path) -> Path | None:
    if folder.is_file():
        return folder
    for relative in ("romfs/a/0/7/4", "a/0/7/4", "4"):
        candidate = folder / relative
        if candidate.is_file():
            return candidate
    return None
