"""Choose the output game's identity before changing any input files."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Callable, TypeVar

from .config import (
    EXHEADER_PROGRAM_ID_OFFSET,
    GAME_NAME_MAX_LENGTH,
    LEGACY_TITLE_ID_EXAMPLES,
    TITLE_ID_MIDDLE_LENGTH,
    TITLE_ID_PREFIX,
    TITLE_ID_SUFFIX,
)
from .identity import (
    parse_title_id_middle,
    read_exheader,
    validate_requested_title_id,
)
from .inputs import SelectionCancelled
from .names import validate_game_name

Value = TypeVar("Value")

TITLE_ID_DESCRIPTION = (
    "The Title ID identifies the game and selects its save data and emulator "
    "custom texture folders. Use a different ID to keep these separate from "
    "the base game."
)

TITLE_ID_WARNING = (
    "Do not use an ID already used by another game. Matching IDs can share "
    "saves and emulator textures. Use the Legacy examples only for their "
    "respective games."
)

GAME_NAME_DESCRIPTION = (
    "The game name is displayed on the 3DS HOME Menu and in emulator game "
    "lists. A new name also becomes the output ROM filename."
)


def format_title_id_examples() -> str:
    return "\n".join(
        f"{name}: {TITLE_ID_PREFIX}{digits}{TITLE_ID_SUFFIX} (enter {digits})"
        for name, digits in LEGACY_TITLE_ID_EXAMPLES
    )


class TitleIdDialog(simpledialog.Dialog):
    def __init__(self, parent, original_id: int, validate_value):
        self.original_id = original_id
        self.validate_value = validate_value
        super().__init__(parent, title="New title ID")

    def body(self, master):
        base_id = f"{self.original_id:016X}"
        prefix = base_id[: len(TITLE_ID_PREFIX)]
        suffix = base_id[-len(TITLE_ID_SUFFIX) :]
        for text in (
            f"Base title ID: {base_id}",
            f"Enter only the {TITLE_ID_MIDDLE_LENGTH} middle hexadecimal "
            "digits (0-9, A-F).",
        ):
            ttk.Label(master, text=text, justify="left", wraplength=480).pack(
                anchor="w", pady=(0, 8)
            )
        field = ttk.Frame(master)
        field.pack(anchor="w", pady=(0, 12))
        ttk.Label(field, text=f"{prefix}-").pack(side="left")
        self.entry = ttk.Entry(
            field, width=TITLE_ID_MIDDLE_LENGTH + 2, font="TkFixedFont"
        )
        self.entry.pack(side="left", padx=4)
        ttk.Label(field, text=f"-{suffix}").pack(side="left")
        for text in (format_title_id_examples(), TITLE_ID_WARNING):
            ttk.Label(master, text=text, justify="left", wraplength=480).pack(
                anchor="w", pady=(0, 8)
            )
        return self.entry

    def validate(self):
        try:
            self.result = self.validate_value(self.entry.get())
        except ValueError as error:
            messagebox.showerror("New title ID", str(error), parent=self)
            return False
        return True


@dataclass(frozen=True)
class IdentityOptions:
    title_id: int | None = None
    display_name: str | None = None


def ask_to_change(title: str, prompt: str, parent) -> bool:
    choice = messagebox.askyesnocancel(
        title, prompt, default=messagebox.NO, parent=parent
    )
    if choice is None:
        raise SelectionCancelled
    return choice


def ask_for_value(
    title: str,
    prompt: str,
    validate_value: Callable[[str], Value],
    parent,
) -> Value:
    value = ""
    while True:
        value = simpledialog.askstring(
            title, prompt, initialvalue=value, parent=parent
        )
        if value is None:
            raise SelectionCancelled
        try:
            return validate_value(value)
        except ValueError as error:
            messagebox.showerror(title, str(error), parent=parent)


def select_identity_options(
    base: Path,
    *,
    title_id: int | str | None = None,
    display_name: str | None = None,
    parent=None,
) -> IdentityOptions:
    exheader = read_exheader(base)
    original_id = struct.unpack_from(
        "<Q", exheader, EXHEADER_PROGRAM_ID_OFFSET
    )[0]

    def validate_new_title_id(value: str) -> int:
        requested_id = parse_title_id_middle(value, original_id)
        validate_requested_title_id(exheader, requested_id)
        return requested_id

    if isinstance(title_id, str):
        title_id = validate_new_title_id(title_id)
    elif title_id is not None:
        validate_requested_title_id(exheader, title_id)
    elif ask_to_change(
        "Title ID",
        f"Base title ID: {original_id:016X}\n\n"
        f"{TITLE_ID_DESCRIPTION}\n\n"
        "Give the game a new title ID?\n"
        "Choose No to keep the base ROM's ID.\n\n"
        f"{TITLE_ID_WARNING}",
        parent,
    ):
        dialog = TitleIdDialog(parent, original_id, validate_new_title_id)
        if dialog.result is None:
            raise SelectionCancelled
        title_id = dialog.result
    if display_name is not None:
        display_name = validate_game_name(display_name)
    elif ask_to_change(
        "Game name",
        f"{GAME_NAME_DESCRIPTION}\n\n"
        "Rename the game?\n"
        "Choose No to keep the base ROM's name.",
        parent,
    ):
        display_name = ask_for_value(
            "New game name",
            f"Enter a game name, up to {GAME_NAME_MAX_LENGTH} "
            "characters.\nUse letters, numbers, punctuation and spaces.",
            validate_game_name,
            parent,
        )
    return IdentityOptions(title_id, display_name)
