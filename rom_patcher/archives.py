"""Portable patch archive publishing and safe extraction."""

from __future__ import annotations

import atexit
import os
import re
import shutil
import stat
import zipfile
from pathlib import Path

from .config import (
    BASE_MANIFEST_NAME,
    CODE_BIN_RELPATH,
    GARC_PATCH_EXT,
    PATCH_ARCHIVE_MARKER,
    PATCH_ARCHIVE_NAMES,
    PATCHES_ROOT_DIR,
    PATCHSET_ZIP_EXT,
    SEVENZIP_DICTIONARY_MB,
    SEVENZIP_LEVEL,
    SEVENZIP_WORD_SIZE,
)
from .dependencies import require_py7zr
from .files import iter_files, sha256_file
from .patches import read_base_manifest
from .runtime import create_hidden_workdir, safe_rmtree

# The patcher runs discovery more than once, so each zip is extracted once.
_EXTRACTED_PATCH_ZIPS: dict[Path, Path] = {}


def py7zr_compression_filters() -> list[dict]:
    module = require_py7zr()
    return [
        {
            "id": module.FILTER_LZMA2,
            "preset": SEVENZIP_LEVEL,
            "dict_size": SEVENZIP_DICTIONARY_MB * 1024 * 1024,
            "nice_len": SEVENZIP_WORD_SIZE,
        }
    ]


def compress_patch_subtree(patchset_root: Path, name: str) -> Path:
    """Create a portable name.7z without unsafe Windows directory modes."""
    module = require_py7zr()
    source_dir = patchset_root / name
    source_dir.mkdir(parents=True, exist_ok=True)
    archive = patchset_root / f"{name}.7z"
    if archive.exists():
        archive.unlink()

    with module.SevenZipFile(
        archive,
        mode="w",
        filters=py7zr_compression_filters(),
        dereference=True,
    ) as seven_zip:
        files = sorted(
            (path for path in source_dir.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(source_dir).as_posix().casefold(),
        )
        if not files:
            seven_zip.writestr(b"", f"{name}/{PATCH_ARCHIVE_MARKER}")
        else:
            for path in files:

                path.chmod(path.stat().st_mode | 0o600)
                arcname = f"{name}/{path.relative_to(source_dir).as_posix()}"
                seven_zip.write(path, arcname=arcname)
    if not archive.is_file():
        raise RuntimeError(f"py7zr did not create {archive.name}")

    with module.SevenZipFile(archive, mode="r") as seven_zip:
        bad_directories = [
            item.filename for item in seven_zip.files if item.is_directory
        ]
        if bad_directories:
            raise RuntimeError(
                f"Portable archive check failed for {archive.name}: "
                f"unexpected directory entry {bad_directories[0]}"
            )
    shutil.rmtree(source_dir)
    return archive


def publish_patchset(staged: Path, destination: Path) -> None:
    """
    Replace an old generated patch set and its zip only after each new one
    is complete.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(
        f".{destination.name}.previous.{os.getpid()}"
    )
    if backup.exists():
        shutil.rmtree(backup)

    moved_old = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        shutil.move(str(staged), str(destination))
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        if moved_old and backup.exists():
            os.replace(backup, destination)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)
    archive = destination.with_name(destination.name + PATCHSET_ZIP_EXT)
    partial_archive = destination.with_name(
        f".{archive.name}.partial.{os.getpid()}"
    )
    try:
        with zipfile.ZipFile(partial_archive, "w") as zip_file:
            for relative_path, path in iter_files(destination):
                zip_file.write(path, f"{destination.name}/{relative_path}")
        os.replace(partial_archive, archive)
    finally:
        partial_archive.unlink(missing_ok=True)


def _patchset_entry(patchset_dir: Path):
    """Return one patch-set descriptor, or None if the folder is not a set."""
    patchset_dir = Path(patchset_dir)
    if not patchset_dir.is_dir():
        return None
    if not (patchset_dir / BASE_MANIFEST_NAME).is_file():
        return None
    has_loose_patch = any(
        path.is_file()
        and (
            path.name.lower().endswith(".bps")
            or path.name.lower().endswith(GARC_PATCH_EXT)
        )
        for path in patchset_dir.rglob("*")
    )
    has_archive = any(
        ((patchset_dir / name).is_file() for name in PATCH_ARCHIVE_NAMES)
    )
    if not (has_loose_patch or has_archive):
        return None
    rsf_files = sorted(
        (p for p in patchset_dir.glob("*.rsf") if p.is_file()),
        key=lambda p: p.name.casefold(),
    )
    if not rsf_files:
        return None
    return (patchset_dir, rsf_files[0].relative_to(patchset_dir))


def find_patchsets(patches_root: Path):
    if not patches_root.is_dir():
        raise RuntimeError(
            "Missing patches folder:\n\n%s" % patches_root.as_posix()
        )
    children = sorted(
        (child for child in patches_root.iterdir() if child.is_dir()),
        key=lambda p: p.name.casefold(),
    )
    patchsets = []
    for child in children:
        entry = _patchset_entry(child)
        if entry is not None:
            patchsets.append(entry)
    if patchsets:
        return patchsets
    for wrapper in children:
        grandchildren = sorted(
            (child for child in wrapper.iterdir() if child.is_dir()),
            key=lambda p: p.name.casefold(),
        )
        for child in grandchildren:
            entry = _patchset_entry(child)
            if entry is not None:
                patchsets.append(entry)
    if not patchsets:
        for archive in patches_root.iterdir():
            if archive.suffix.casefold() == PATCHSET_ZIP_EXT:
                patchsets.extend(_find_zip_patchsets(archive))
    patchsets.sort(
        key=lambda t: (t[0].parent.name.casefold(), t[0].name.casefold())
    )
    return patchsets


def _find_zip_patchsets(archive: Path):
    key = archive.resolve()
    if key not in _EXTRACTED_PATCH_ZIPS:
        try:
            zip_file = zipfile.ZipFile(archive)
        except (OSError, zipfile.BadZipFile):
            return []
        with zip_file:
            if not any(
                Path(name).name == BASE_MANIFEST_NAME
                for name in zip_file.namelist()
            ):
                return []
            extracted_root = create_hidden_workdir() / archive.stem
            atexit.register(safe_rmtree, extracted_root.parent)
            # extractall drops absolute and ".." parts of member names.
            zip_file.extractall(extracted_root)
        _EXTRACTED_PATCH_ZIPS[key] = extracted_root
    entries = (
        _patchset_entry(manifest.parent)
        for manifest in _EXTRACTED_PATCH_ZIPS[key].rglob(BASE_MANIFEST_NAME)
    )
    return [entry for entry in entries if entry is not None]


def find_patchsets_from_selection(selected_folder: Path):
    """
    Accept one patch set, the patches root, or its containing release root.
    """
    selected_folder = Path(selected_folder)
    if not selected_folder.is_dir():
        raise RuntimeError(
            "Selected patches location is not a folder:\n\n%s"
            % selected_folder.as_posix()
        )
    individual = _patchset_entry(selected_folder)
    if individual is not None:
        return [individual]
    nested_patches = selected_folder / PATCHES_ROOT_DIR
    patches_root = (
        nested_patches if nested_patches.is_dir() else selected_folder
    )
    return find_patchsets(patches_root)


def _manifest_code_entry(patchset_dir: Path) -> dict:
    manifest = read_base_manifest(patchset_dir)
    if manifest is None:
        raise RuntimeError("Missing base manifest")
    matches = [
        entry
        for entry in manifest["files"]
        if str(entry.get("path", "")).replace("\\", "/").casefold()
        == CODE_BIN_RELPATH.casefold()
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Base manifest must contain exactly one exefs/code.bin entry"
        )
    return matches[0]


def match_patchsets_by_code(patchsets, extracted_code: Path):
    """
    Match every patch set from its root manifest without extracting 7z files.
    """
    if not extracted_code.is_file():
        raise RuntimeError("Extracted ROM is missing exefs/code.bin")
    actual_size = extracted_code.stat().st_size
    size_candidates = []
    rejected = []
    for patchset_dir, rsf_rel in patchsets:
        try:
            entry = _manifest_code_entry(patchset_dir)
            if int(entry["size"]) != actual_size:
                rejected.append(
                    (patchset_dir.name, "code.bin size does not match")
                )
                continue
            size_candidates.append((patchset_dir, rsf_rel, entry))
        except Exception as exc:
            rejected.append((patchset_dir.name, str(exc).splitlines()[0]))
    if not size_candidates:
        return ([], rejected)
    actual_sha = sha256_file(extracted_code)
    matches = []
    for patchset_dir, rsf_rel, entry in size_candidates:
        if actual_sha.casefold() == str(entry["sha256"]).casefold():
            matches.append((patchset_dir, rsf_rel))
        else:
            rejected.append(
                (patchset_dir.name, "code.bin SHA-256 does not match")
            )
    return (matches, rejected)


def _validate_archive_layout(seven_zip, archive: Path, expected_root: str):
    member_count = 0
    for raw_name in seven_zip.getnames():
        member = str(raw_name).replace("\\", "/")
        if not member:
            continue
        if member.startswith(("/", "//")) or re.match("^[A-Za-z]:", member):
            raise RuntimeError(
                "Unsafe absolute path in archive: %s" % archive.name
            )
        parts = [part for part in member.split("/") if part not in ("", ".")]
        if (
            not parts
            or ".." in parts
            or parts[0].casefold() != expected_root.casefold()
        ):
            raise RuntimeError(
                "%s contains an entry outside %s/"
                % (archive.name, expected_root)
            )
        member_count += 1
    if member_count == 0:
        raise RuntimeError(
            "Archive contains no readable entries: %s" % archive.name
        )
    for archived_file in seven_zip.files:
        if (
            archived_file.is_symlink
            or archived_file.is_junction
            or archived_file.is_socket
        ):
            raise RuntimeError(
                "Patch archives may not contain links or special files: %s"
                % archive.name
            )


class _SafeArchiveOutput:
    """
    Disk-backed py7zr output that never applies permissions from an archive.
    """

    def __init__(self, filename: str, destination_root: Path):
        root = destination_root.resolve()
        path = Path(filename).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise RuntimeError(
                "Archive attempted to write outside the patch staging folder"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(
            path.parent.stat().st_mode
            | stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
        )
        self._path = path
        self._file = path.open("w+b")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def write(self, data):
        return self._file.write(data)

    def read(self, size=None):
        return self._file.read() if size is None else self._file.read(size)

    def seek(self, offset, whence=0):
        return self._file.seek(offset, whence)

    def flush(self):
        self._file.flush()

    def size(self):
        position = self._file.tell()
        self._file.seek(0, os.SEEK_END)
        result = self._file.tell()
        self._file.seek(position, os.SEEK_SET)
        return result

    def close(self):
        if not self._file.closed:
            self._file.close()


class _SafeArchiveFactory:

    def __init__(self, destination_root: Path):
        self.destination_root = destination_root.resolve()

    def create(self, filename: str):
        return _SafeArchiveOutput(filename, self.destination_root)


def _normalize_patchset_permissions(destination: Path):
    """
    Ensure copied and 7z-extracted patch data is accessible to this user.
    """
    for current_root, dirnames, filenames in os.walk(
        str(destination), topdown=True, followlinks=False
    ):
        root = Path(current_root)
        paths = [(root, True)]
        paths.extend(((root / name, True) for name in dirnames))
        paths.extend(((root / name, False) for name in filenames))
        for path, is_directory in paths:
            if path.is_symlink():
                raise RuntimeError(
                    "Patch archives may not contain symbolic links"
                )
            try:
                mode = path.stat().st_mode
                required = stat.S_IRUSR | stat.S_IWUSR
                if is_directory:
                    required |= stat.S_IXUSR
                path.chmod(mode | required)
            except OSError as exc:
                raise RuntimeError(
                    "Could not make extracted patch data accessible: %s (%s)"
                    % (path.as_posix(), exc)
                )


def materialize_patchset(patchset_dir: Path, destination: Path) -> None:
    """Copy patch metadata and safely extract archived patch trees."""
    safe_rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    archive_names = {name.casefold() for name in PATCH_ARCHIVE_NAMES}
    archived_trees = {
        Path(name).stem.casefold()
        for name in PATCH_ARCHIVE_NAMES
        if (patchset_dir / name).is_file()
    }
    for child in patchset_dir.iterdir():
        name_key = child.name.casefold()
        if name_key in archive_names:
            continue
        if child.is_dir() and name_key in archived_trees:
            continue
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        elif child.is_file():
            shutil.copy2(child, target)
    for archive_name in PATCH_ARCHIVE_NAMES:
        archive = patchset_dir / archive_name
        if not archive.is_file():
            continue
        expected_root = Path(archive_name).stem
        module = require_py7zr()
        with module.SevenZipFile(archive, mode="r") as seven_zip:
            _validate_archive_layout(seven_zip, archive, expected_root)
            extracted_root = destination / expected_root
            extracted_root.mkdir(parents=True, exist_ok=True)
            extracted_root.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            seven_zip.extractall(
                path=destination, factory=_SafeArchiveFactory(destination)
            )
        extracted_root = destination / expected_root
        if not extracted_root.is_dir():
            raise RuntimeError(
                "%s did not contain the expected %s/ folder"
                % (archive.name, expected_root)
            )
        archive_marker = extracted_root / ".patch_archive_root"
        if archive_marker.is_file():
            archive_marker.unlink()
    _normalize_patchset_permissions(destination)
