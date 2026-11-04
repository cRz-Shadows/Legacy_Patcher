"""Streaming file checks and source manifests."""

from __future__ import annotations

import hashlib
import os
import unicodedata
import zlib
from pathlib import Path
from typing import Iterable

from .config import (
    BASE_MANIFEST_EXCLUDE_RELPATHS,
    BASE_MANIFEST_FORMAT,
    IO_CHUNK,
    OS_METADATA_NAMES,
    OS_METADATA_PREFIX,
    WINDOWS_DEVICE_NAMES,
)
from .progress import ConsoleProgress


def file_hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        while True:
            b = f.read(IO_CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def files_differ(a: Path, b: Path, algo: str) -> bool:
    try:
        sa = a.stat().st_size
        sb = b.stat().st_size
    except OSError:
        return True
    if sa != sb:
        return True
    return file_hash(a, algo) != file_hash(b, algo)


def iter_files(root: Path) -> Iterable[tuple[str, Path]]:
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            yield rel, p


def build_index(root: Path, ignore_case: bool) -> dict[str, Path]:
    idx: dict[str, Path] = {}
    for rel, abs_path in iter_files(root):
        key = rel.lower() if ignore_case else rel
        idx[key] = abs_path
    return idx


def is_os_metadata(path: Path) -> bool:
    name = path.name.casefold()
    return name in OS_METADATA_NAMES or name.startswith(OS_METADATA_PREFIX)


def _fold_romfs_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _is_windows_device_name(name: str) -> bool:
    """Windows reserves device names even with an extension, as in NUL .txt."""
    return name.partition(".")[0].rstrip(" ").upper() in WINDOWS_DEVICE_NAMES


def _index_romfs_names(directory: Path) -> dict[str, str | None]:
    """Map each name to its directory spelling, or None if occupied."""
    names: dict[str, str | None] = {}
    if not directory.is_dir():
        return names
    if directory.resolve() != directory:
        raise RuntimeError(f"Linked RomFS directory: {directory}")
    for entry in directory.iterdir():
        key = _fold_romfs_name(entry.name)
        if key not in names and entry.is_dir() and not entry.is_symlink():
            names[key] = entry.name
        else:
            names[key] = None
    return names


def validate_romfs_additions(
    root: Path | None, relative_paths: Iterable[str]
) -> None:
    """Check added paths for case and Unicode collisions before writing."""
    directories: dict[Path, dict[str, str | None]] = {}
    base = root.resolve() if root is not None else Path()
    for relative_path in relative_paths:
        parts = relative_path.split("/")
        if (
            len(parts) < 2
            or parts[0] != "romfs"
            or any(
                not part
                or part.endswith((".", " "))
                or any(char in '\\:<>"|?*' or ord(char) < 32 for char in part)
                or _is_windows_device_name(part)
                for part in parts
            )
        ):
            raise RuntimeError(
                f"New file needs a valid romfs/ path: {relative_path}"
            )

        parent = base
        for index, part in enumerate(parts):
            if parent not in directories:
                directories[parent] = (
                    _index_romfs_names(parent) if root is not None else {}
                )
            names = directories[parent]
            name_key = _fold_romfs_name(part)
            directory_name = part if index < len(parts) - 1 else None
            if name_key in names and (
                directory_name is None or names[name_key] != directory_name
            ):
                raise RuntimeError(
                    f"Conflicting new RomFS path: {relative_path}"
                )
            names[name_key] = directory_name
            parent /= part


def build_base_manifest(root: Path) -> dict:
    """
    Hash every individual file in the clean extracted base tree.

    SHA-256 is used to validate the user's extraction. CRC32 is recorded in the
    same pass so BPS source requirements can be checked without reading files a
    second time during installation.
    """
    excluded = {
        x.replace("\\", "/").casefold() for x in BASE_MANIFEST_EXCLUDE_RELPATHS
    }
    files: list[tuple[str, Path, int]] = []
    total = 0
    for rel, path in iter_files(root):
        rel_norm = rel.replace("\\", "/")
        if rel_norm.casefold() in excluded:
            continue
        size = path.stat().st_size
        files.append((rel_norm, path, size))
        total += size
    files.sort(key=lambda item: item[0].casefold())

    progress = ConsoleProgress(
        max(1, total), "Hashing complete base extraction"
    )
    entries: list[dict] = []
    done = 0
    for rel, path, size in files:
        sha = hashlib.sha256()
        crc = 0
        with path.open("rb") as f:
            while True:
                chunk = f.read(IO_CHUNK)
                if not chunk:
                    break
                sha.update(chunk)
                crc = zlib.crc32(chunk, crc) & 0xFFFFFFFF
                done += len(chunk)
                progress.update(done, detail=rel)
        entries.append(
            {
                "path": rel,
                "size": size,
                "sha256": sha.hexdigest(),
                "crc32": f"{crc:08x}",
            }
        )
    progress.update(max(1, total), detail=f"{len(entries)} files", force=True)
    return {
        "format": BASE_MANIFEST_FORMAT,
        "hash": "sha256",
        "files": entries,
    }


def safe_rename(src: Path, dst: Path) -> None:
    """
    Rename src -> dst with some collision handling:
    - If dst already exists, we prefer dst and remove src.
    """
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():

        try:
            src.unlink()
        except Exception:

            os.replace(src, dst)
        return
    os.replace(src, dst)


def sha256_file(
    path: Path, progress_cb=None, done_base: int = 0, total: int = 1
) -> str:
    h = hashlib.sha256()
    done = 0
    with path.open("rb") as f:
        while True:
            b = f.read(IO_CHUNK)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress_cb:
                progress_cb(done_base + done, total)
    return h.hexdigest()


def crc32_file(path: Path) -> int:
    c = 0
    with path.open("rb") as f:
        while True:
            b = f.read(IO_CHUNK)
            if not b:
                break
            c = zlib.crc32(b, c) & 0xFFFFFFFF
    return c
