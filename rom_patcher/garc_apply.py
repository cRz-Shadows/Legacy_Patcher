"""Parallel application of exact GARC patches."""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
import zipfile
from pathlib import Path

from .config import (
    GARC_PATCH_MAX_AUTO_WORKERS,
    GARC_PATCH_MEMORY_BUDGET_MB,
    GARC_PATCH_WORKERS,
    IO_CHUNK,
)
from .delta import apply_raw_delta, apply_xor_zlib
from .files import sha256_file
from .garc import build_canonical_garc_prefix, read_garc_patch_manifest
from .native import (
    _get_native_lz11_library,
    lz11_decode_standard,
    rebuild_lz11_from_recipe,
)

_GARC_WORKER_SOURCE = None
_GARC_WORKER_ZIP = None
_GARC_WORKER_OUTPUT = None


def _garc_worker_init(
    patch_path: str, source_path: str, output_path: str
) -> None:
    global _GARC_WORKER_SOURCE, _GARC_WORKER_ZIP, _GARC_WORKER_OUTPUT
    _GARC_WORKER_SOURCE = open(source_path, "rb")
    _GARC_WORKER_ZIP = zipfile.ZipFile(patch_path, "r")
    _GARC_WORKER_OUTPUT = open(output_path, "r+b", buffering=0)
    _get_native_lz11_library()


def _rebuild_garc_member(
    record: dict, source_file, patch_archive: zipfile.ZipFile
) -> bytes:
    source_file.seek(int(record["source_offset"]))
    source_packed = source_file.read(int(record["source_length"]))
    if len(source_packed) != int(record["source_length"]):
        raise IOError("Unexpected EOF while reading a source GARC member")
    source_encoding = record["source_encoding"]
    if source_encoding == "lz11":
        source_raw = lz11_decode_standard(source_packed)[0]
    elif source_encoding == "raw":
        source_raw = source_packed
    else:
        raise ValueError("Unknown source GARC member encoding")
    del source_packed
    raw_mode = record["raw_mode"]
    raw_size = int(record["raw_size"])
    if raw_mode == "same":
        target_raw = source_raw
    elif raw_mode == "xor_zlib":
        target_raw = apply_xor_zlib(
            source_raw, patch_archive.read(record["raw_blob"]), raw_size
        )
    elif raw_mode == "delta":
        target_raw = apply_raw_delta(
            source_raw, patch_archive.read(record["raw_blob"])
        )
    else:
        raise ValueError("Unknown raw GARC reconstruction mode")
    if len(target_raw) != raw_size:
        raise ValueError("Reconstructed raw GARC member size mismatch")
    target_encoding = record["target_encoding"]
    if target_encoding == "raw":
        packed = target_raw
    elif target_encoding == "lz11":
        packed = rebuild_lz11_from_recipe(
            target_raw, patch_archive.read(record["recipe_blob"])
        )
    else:
        raise ValueError("Unknown target GARC member encoding")
    if len(packed) != int(record["target_length"]):
        raise ValueError("Rebuilt packed GARC member size mismatch")
    return packed


def _garc_worker_rebuild_to_output(record: dict) -> int:
    if (
        _GARC_WORKER_SOURCE is None
        or _GARC_WORKER_ZIP is None
        or _GARC_WORKER_OUTPUT is None
    ):
        raise RuntimeError("GARC worker was not initialized")
    packed = _rebuild_garc_member(record, _GARC_WORKER_SOURCE, _GARC_WORKER_ZIP)
    target_length = int(record["target_length"])
    padded_length = int(record["target_padded_length"])
    _GARC_WORKER_OUTPUT.seek(int(record["_target_offset"]))
    _GARC_WORKER_OUTPUT.write(packed)
    padding = padded_length - target_length
    if padding:
        _GARC_WORKER_OUTPUT.write(b"\xff" * padding)
    return padded_length


def _estimate_garc_worker_memory(rec: dict) -> int:
    """Conservative peak estimate using the actual reconstruction mode."""
    raw_size = int(rec.get("raw_size", 0))
    source_length = int(rec.get("source_length", 0))
    target_length = int(rec.get("target_length", 0))
    fixed_margin = 64 * 1024 * 1024
    raw_mode = rec.get("raw_mode")
    if raw_mode == "same":
        peak = source_length + raw_size
        if rec.get("target_encoding") == "lz11":
            peak += target_length
    else:
        peak = max(13 * raw_size // 4, raw_size + target_length)
    return fixed_margin + max(peak, source_length + raw_size)


def _choose_garc_worker_count(records: list[dict]) -> int:
    rebuild = [r for r in records if r.get("mode") == "rebuild_raw"]
    total_raw = sum((int(r.get("raw_size", 0)) for r in rebuild))
    if len(rebuild) < 2:
        return 1
    if GARC_PATCH_WORKERS > 0:
        requested = GARC_PATCH_WORKERS
    else:
        if total_raw < 128 * 1024 * 1024:
            return 1
        requested = min(
            GARC_PATCH_MAX_AUTO_WORKERS, max(1, (os.cpu_count() or 2) - 1)
        )
    budget = max(256, int(GARC_PATCH_MEMORY_BUDGET_MB)) * 1024 * 1024
    largest_estimate = max(
        (_estimate_garc_worker_memory(record) for record in rebuild),
        default=budget,
    )
    requested = min(requested, max(1, budget // max(1, largest_estimate)))
    return max(1, min(requested, len(rebuild)))


def apply_garc_patch(
    patch_path: Path,
    source_path: Path,
    progress_cb=None,
    progress_base: int = 0,
    progress_total: int = 1,
    manifest=None,
) -> int:
    patch_path = Path(patch_path)
    source_path = Path(source_path)
    if manifest is None:
        manifest = read_garc_patch_manifest(patch_path)
    if source_path.stat().st_size != int(manifest["source_size"]):
        raise RuntimeError(
            "Source size mismatch for file: %s" % source_path.as_posix()
        )
    records = [dict(record) for record in manifest["records"]]
    prefix = build_canonical_garc_prefix(
        int(manifest["garc_version"]), int(manifest["entry_count"]), records
    )
    cursor = len(prefix)
    for record_index, record in enumerate(records):
        record["_record_index"] = record_index
        record["_target_offset"] = cursor
        cursor += int(record["target_padded_length"])
    expected_size = cursor
    if expected_size != int(manifest["target_size"]):
        raise ValueError("GARC patch manifest target size is inconsistent")
    rebuild_records = [
        record for record in records if record["mode"] == "rebuild_raw"
    ]
    worker_count = _choose_garc_worker_count(records)
    temp_path = source_path.with_suffix(
        source_path.suffix + ".tmp.%d" % os.getpid()
    )
    if temp_path.exists():
        temp_path.unlink()
    completed = len(prefix)
    try:
        with (
            temp_path.open("w+b") as output,
            zipfile.ZipFile(patch_path, "r") as patch_archive,
            source_path.open("rb") as source_file,
        ):
            output.truncate(expected_size)
            output.write(prefix)
            if progress_cb:
                progress_cb(progress_base + completed, progress_total)
            for record in records:
                mode = record["mode"]
                if mode == "rebuild_raw":
                    continue
                target_offset = int(record["_target_offset"])
                target_length = int(record["target_length"])
                padded_length = int(record["target_padded_length"])
                output.seek(target_offset)
                if mode == "copy_packed":
                    source_file.seek(int(record["source_offset"]))
                    remaining = int(record["source_length"])
                    if remaining != target_length:
                        raise ValueError(
                            "Packed-copy GARC member length mismatch"
                        )
                    while remaining:
                        chunk = source_file.read(min(IO_CHUNK, remaining))
                        if not chunk:
                            raise IOError(
                                "Unexpected EOF while copying a source GARC"
                                " member"
                            )
                        output.write(chunk)
                        remaining -= len(chunk)
                elif mode == "literal_packed":
                    packed = patch_archive.read(record["blob"])
                    if len(packed) != target_length:
                        raise ValueError("Literal GARC member length mismatch")
                    output.write(packed)
                else:
                    raise ValueError("Unknown GARC patch member mode")
                padding = padded_length - target_length
                if padding:
                    output.write(b"\xff" * padding)
                completed += padded_length
                if progress_cb:
                    progress_cb(progress_base + completed, progress_total)
        if worker_count > 1:
            context = multiprocessing.get_context("spawn")
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=worker_count,
                mp_context=context,
                initializer=_garc_worker_init,
                initargs=(str(patch_path), str(source_path), str(temp_path)),
            ) as executor:
                futures = [
                    executor.submit(_garc_worker_rebuild_to_output, record)
                    for record in rebuild_records
                ]
                for future in concurrent.futures.as_completed(futures):
                    completed += future.result()
                    if progress_cb:
                        progress_cb(progress_base + completed, progress_total)
        else:
            with (
                zipfile.ZipFile(patch_path, "r") as patch_archive,
                source_path.open("rb") as source_file,
                temp_path.open("r+b", buffering=0) as output,
            ):
                for record in rebuild_records:
                    packed = _rebuild_garc_member(
                        record, source_file, patch_archive
                    )
                    output.seek(int(record["_target_offset"]))
                    output.write(packed)
                    padding = int(record["target_padded_length"]) - int(
                        record["target_length"]
                    )
                    if padding:
                        output.write(b"\xff" * padding)
                    completed += int(record["target_padded_length"])
                    if progress_cb:
                        progress_cb(progress_base + completed, progress_total)
        if completed != expected_size:
            raise RuntimeError(
                "GARC patch did not reconstruct every target byte"
            )
        target_sha256 = sha256_file(temp_path)
        if (
            target_sha256.casefold()
            != str(manifest["target_sha256"]).casefold()
        ):
            raise RuntimeError("GARC patch target SHA-256 mismatch")
        os.replace(temp_path, source_path)
        return expected_size
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
