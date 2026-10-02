"""Consistent names for generated ROMs and patch sets."""

from __future__ import annotations

from pathlib import Path

from .config import GAME_NAME_MAX_LENGTH


def validate_game_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Enter a game name.")
    if len(name) > GAME_NAME_MAX_LENGTH:
        raise ValueError(f"Use no more than {GAME_NAME_MAX_LENGTH} characters.")
    if any(
        not character.isprintable() or ord(character) > 0xFFFF
        for character in name
    ):
        raise ValueError(
            "Use printable text without line breaks, control characters, "
            "or characters outside the basic Unicode range."
        )
    return name.strip()


def sanitize_filename(name: str) -> str:
    reserved = '<>:"/\\|?*'
    cleaned = (
        "".join(
            "_" if character in reserved or ord(character) < 32 else character
            for character in name
        )
        .strip()
        .rstrip(" .")
    )
    cleaned = cleaned or "output"
    reserved_names = {"CON", "PRN", "AUX", "NUL"}
    reserved_names.update(
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in (*range(1, 10), "¹", "²", "³")
    )
    if cleaned.split(".", 1)[0].upper() in reserved_names:
        cleaned = "_" + cleaned
    return cleaned


def make_patchset_name(
    game_name: str,
    display_name: str,
    unique_id: int,
    *,
    changes_title_id: bool,
) -> str:
    if display_name:
        stem = sanitize_filename(display_name.casefold().replace(" ", "_"))
    else:
        stem = sanitize_filename(game_name.casefold())
        if changes_title_id:
            stem += f"_{unique_id:05x}"
    return stem + "_patches"


def make_output_base(
    source_rom: Path,
    display_name: str | None = None,
) -> str:
    if display_name is not None:
        return sanitize_filename(display_name)
    return sanitize_filename(source_rom.stem + "_patched")


def choose_non_overwriting_output(
    folder: Path, base: str, extension: str = ".3ds"
) -> Path:
    candidate = folder / (base + extension)
    number = 2
    while candidate.exists():
        candidate = folder / f"{base} ({number}){extension}"
        number += 1
    return candidate
