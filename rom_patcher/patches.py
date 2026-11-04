"""Patch planning, complete base validation, and application."""

from __future__ import annotations

import json
import mmap
import os
import tempfile
from contextlib import contextmanager, nullcontext, suppress
from pathlib import Path
from typing import Optional

from .bps import (
    _open_patch_file,
    _read_bps_metadata_file,
    _sha256_crc32_file_progress,
    apply_bps_patch_inplace,
    crc32_file_progress,
)
from .config import (
    BASE_MANIFEST_FORMAT,
    BASE_MANIFEST_NAME,
    BASE_VALIDATION_IGNORE_RELPATHS,
    GARC_PATCH_EXT,
)
from .files import sha256_file, validate_romfs_additions
from .garc import read_garc_patch_manifest
from .garc_apply import apply_garc_patch


def read_base_manifest(patchset_root: Path) -> Optional[dict]:
    path = Path(patchset_root) / BASE_MANIFEST_NAME
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != BASE_MANIFEST_FORMAT or not isinstance(
        data.get("files"), list
    ):
        raise RuntimeError("Unsupported or corrupt full base-file manifest")
    return data


def list_patch_files(patchset_root):
    files = []
    for p in Path(patchset_root).rglob("*"):
        if not p.is_file():
            continue
        lower = p.name.lower()
        rel = p.relative_to(patchset_root).as_posix()
        if lower.endswith(".bps"):
            files.append(("bps", rel[:-4], p))
        elif lower.endswith(GARC_PATCH_EXT):
            files.append(("garc", rel[: -len(GARC_PATCH_EXT)], p))
    files.sort(key=lambda item: item[2].as_posix().casefold())
    return files


def build_patch_plan(patchset_root, workdir=None):
    patch_list = list_patch_files(patchset_root)
    if not patch_list:
        raise RuntimeError(
            "No supported patch files found in: %s" % patchset_root
        )
    plan = []
    for kind, rel_target, patch_path in patch_list:
        target_file = (
            Path(workdir) / Path(rel_target) if workdir is not None else None
        )
        if kind == "bps":
            metadata = _read_bps_metadata_file(patch_path)
            item = {
                "kind": kind,
                "rel_target": rel_target,
                "patch_path": patch_path,
                "target_file": target_file,
                **metadata,
            }
        else:
            manifest = read_garc_patch_manifest(patch_path)
            item = {
                "kind": kind,
                "rel_target": rel_target,
                "patch_path": patch_path,
                "target_file": target_file,
                "source_size": int(manifest["source_size"]),
                "target_size": int(manifest["target_size"]),
                "source_sha256": str(manifest["source_sha256"]),
                "target_sha256": str(manifest["target_sha256"]),
                "manifest": manifest,
            }
        if item.get("adds_file"):
            if item["source_size"] or item["source_crc"]:
                raise RuntimeError(
                    f"BPS addition has a nonempty source: {rel_target}"
                )
        elif target_file is not None and not target_file.is_file():
            raise RuntimeError(
                "Target file for patch not found: %s" % rel_target
            )
        plan.append(item)
    validate_romfs_additions(
        Path(workdir) if workdir is not None else None,
        (item["rel_target"] for item in plan if item.get("adds_file")),
    )
    return plan


def _validate_manifest_structure(manifest: dict, workdir: Path):
    ignored = {
        x.replace("\\", "/").casefold() for x in BASE_VALIDATION_IGNORE_RELPATHS
    }
    expected = {}
    for entry in manifest["files"]:
        rel = str(entry.get("path", "")).replace("\\", "/")
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise RuntimeError("Invalid path in full base-file manifest")
        key = rel.casefold()
        if key in expected:
            raise RuntimeError(
                "Duplicate path in full base-file manifest: %s" % rel
            )
        expected[key] = entry
    actual = {}
    for path in Path(workdir).rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(workdir).as_posix()
        key = rel.casefold()
        if key in ignored:
            continue
        if key in actual:
            raise RuntimeError(
                "Duplicate extracted path after case folding: %s" % rel
            )
        actual[key] = (rel, path)
    missing = [
        str(entry["path"])
        for key, entry in expected.items()
        if key not in actual
    ]
    unexpected = [
        rel for key, (rel, _path) in actual.items() if key not in expected
    ]
    if missing:
        raise RuntimeError("Missing extracted base file: %s" % missing[0])
    if unexpected:
        raise RuntimeError("Unexpected extracted base file: %s" % unexpected[0])
    for key, entry in expected.items():
        rel, path = actual[key]
        expected_size = int(entry["size"])
        if path.stat().st_size != expected_size:
            raise RuntimeError("Extracted file size mismatch for: %s" % rel)
    return (expected, actual)


def prepare_base_validation(
    patchset_root, workdir, progress_cb, patch_plan=None
) -> dict:
    """
    Validate patch sources now and defer untouched RomFS hashing to its copy
    pass.
    """
    patchset_root = Path(patchset_root)
    workdir = Path(workdir)
    if patch_plan is None:
        patch_plan = build_patch_plan(patchset_root, workdir)
    manifest = read_base_manifest(patchset_root)
    if manifest is None:
        total = sum((int(item["source_size"]) for item in patch_plan))
        if progress_cb:
            progress_cb(0, max(1, total))
        done_base = 0
        for item in patch_plan:
            if item.get("adds_file"):
                continue
            target_file = item["target_file"]
            rel = item["rel_target"]
            source_size = int(item["source_size"])
            if target_file.stat().st_size != source_size:
                raise RuntimeError("Extracted file size mismatch for: %s" % rel)
            if item["kind"] == "bps":
                actual_crc = crc32_file_progress(
                    target_file, progress_cb, done_base, max(1, total)
                )
                if actual_crc != int(item["source_crc"]):
                    raise RuntimeError(
                        "Extracted file CRC mismatch for: %s" % rel
                    )
            else:
                actual_sha = sha256_file(
                    target_file, progress_cb, done_base, max(1, total)
                )
                if actual_sha.casefold() != item["source_sha256"].casefold():
                    raise RuntimeError(
                        "Extracted file SHA-256 mismatch for: %s" % rel
                    )
            done_base += source_size
        if progress_cb:
            progress_cb(max(1, total), max(1, total))
        return {
            "complete_manifest": False,
            "deferred_romfs": {},
            "patch_plan": patch_plan,
        }
    expected, actual = _validate_manifest_structure(manifest, workdir)
    patch_keys = {
        item["rel_target"].replace("\\", "/").casefold() for item in patch_plan
    }
    for item in patch_plan:
        if item.get("adds_file"):
            continue
        key = item["rel_target"].replace("\\", "/").casefold()
        if key not in expected:
            raise RuntimeError(
                "Patch target missing from full base manifest: %s"
                % item["rel_target"]
            )
        entry = expected[key]
        if item["kind"] == "bps":
            if int(item["source_size"]) != int(entry["size"]) or int(
                item["source_crc"]
            ) != int(str(entry["crc32"]), 16):
                raise RuntimeError(
                    "BPS source requirements disagree with base manifest: %s"
                    % item["rel_target"]
                )
        else:
            if int(item["source_size"]) != int(entry["size"]):
                raise RuntimeError(
                    "GARC source size disagrees with base manifest: %s"
                    % item["rel_target"]
                )
            if (
                item["source_sha256"].casefold()
                != str(entry["sha256"]).casefold()
            ):
                raise RuntimeError(
                    "GARC source hash disagrees with base manifest: %s"
                    % item["rel_target"]
                )
    deferred_romfs = {}
    immediate = []
    for key, entry in expected.items():
        rel = str(entry["path"]).replace("\\", "/")
        size = int(entry["size"])
        is_romfs = rel.casefold().startswith("romfs/")
        if is_romfs and key not in patch_keys and (size > 0):
            romfs_rel = rel.split("/", 1)[1]
            deferred_romfs[romfs_rel.casefold()] = {
                "path": rel,
                "size": size,
                "sha256": str(entry["sha256"]),
                "crc32": int(str(entry["crc32"]), 16),
            }
        else:
            immediate.append((key, entry))
    immediate.sort(key=lambda item: str(item[1]["path"]).casefold())
    total = sum((int(entry["size"]) for _key, entry in immediate))
    if progress_cb:
        progress_cb(0, max(1, total))
    done_base = 0
    for key, entry in immediate:
        rel, path = actual[key]
        sha, crc = _sha256_crc32_file_progress(
            path, progress_cb, done_base, max(1, total)
        )
        if sha.casefold() != str(entry["sha256"]).casefold():
            raise RuntimeError("Extracted file SHA-256 mismatch for: %s" % rel)
        if crc != int(str(entry["crc32"]), 16):
            raise RuntimeError("Extracted file CRC32 mismatch for: %s" % rel)
        done_base += int(entry["size"])
    if progress_cb:
        progress_cb(max(1, total), max(1, total))
    return {
        "complete_manifest": True,
        "deferred_romfs": deferred_romfs,
        "patch_plan": patch_plan,
    }


@contextmanager
def _stage_bps_addition(destination: Path):
    """Publish the new file only after BPS verifies the staged result."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        staged = Path(directory) / destination.name
        staged.touch()
        yield staged
        destination.touch(exist_ok=False)
        try:
            os.replace(staged, destination)
        except OSError:
            with suppress(OSError):
                destination.unlink(missing_ok=True)
            raise


def apply_patches_dir(
    patchset_root, workdir, progress_cb, validation_state=None
):
    if validation_state is not None:
        patch_plan = validation_state["patch_plan"]
        base_manifest_validated = bool(validation_state["complete_manifest"])
    else:
        patch_plan = build_patch_plan(patchset_root, workdir)
        base_manifest_validated = False
    total = sum((int(item["target_size"]) for item in patch_plan))
    if progress_cb:
        progress_cb(0, max(1, total))
    completed = 0
    for item in patch_plan:
        target_size = int(item["target_size"])
        if item["kind"] == "bps":
            destination = item["target_file"]
            target = (
                _stage_bps_addition(destination)
                if item.get("adds_file")
                else nullcontext(destination)
            )
            with _open_patch_file(item["patch_path"]) as patch_file:
                with (
                    mmap.mmap(
                        patch_file.fileno(), 0, access=mmap.ACCESS_READ
                    ) as mapped_patch,
                    target as target_file,
                ):
                    apply_bps_patch_inplace(
                        mapped_patch,
                        target_file,
                        progress_cb,
                        completed,
                        max(1, total),
                        verify_source_crc=not base_manifest_validated,
                        expected_metadata=item,
                    )
        else:
            apply_garc_patch(
                item["patch_path"],
                item["target_file"],
                progress_cb,
                completed,
                max(1, total),
                manifest=item["manifest"],
            )
        completed += target_size
