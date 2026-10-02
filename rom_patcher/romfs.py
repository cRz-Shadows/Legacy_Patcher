"""Deterministic RomFS metadata, layout, and hash construction."""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import struct
import unicodedata
import zlib
from pathlib import Path

from .config import (
    IO_CHUNK,
    ROMFS_BLOCK_SIZE,
    ROMFS_HASH_BATCH_BYTES,
    ROMFS_HASH_MAX_AUTO_WORKERS,
    ROMFS_HASH_PARALLEL_MIN_BYTES,
    ROMFS_HASH_WORKERS,
    ROMFS_IVFC_HEADER_ALIGNED_SIZE,
    ROMFS_IVFC_HEADER_SIZE,
    ROMFS_UNUSED_ENTRY,
)


def _romfs_align(value, boundary):
    return (value + boundary - 1) // boundary * boundary


def _romfs_name_parts(name):
    normalized = unicodedata.normalize("NFC", name)
    encoded = normalized.encode("utf-16le")
    sort_key = (
        struct.unpack("<%dH" % (len(encoded) // 2), encoded) if encoded else ()
    )
    return (normalized, encoded, sort_key)


def _romfs_collect_directory(path, name=""):
    normalized, encoded, sort_key = _romfs_name_parts(name)
    node = {
        "path": Path(path),
        "name": normalized,
        "name_bytes": encoded,
        "sort_key": sort_key,
        "dirs": [],
        "files": [],
    }
    with os.scandir(path) as scan:
        entries = list(scan)
    entries.sort(key=lambda entry: _romfs_name_parts(entry.name)[2])
    seen = set()
    for entry in entries:
        normalized_entry, encoded_entry, entry_sort_key = _romfs_name_parts(
            entry.name
        )
        if entry_sort_key in seen:
            raise RuntimeError(
                "RomFS contains names that become identical after Unicode"
                " normalization: %s" % entry.path
            )
        seen.add(entry_sort_key)
        if entry.is_symlink():
            raise RuntimeError(
                "RomFS may not contain symbolic links: %s" % entry.path
            )
        if entry.is_dir(follow_symlinks=False):
            child = _romfs_collect_directory(Path(entry.path), normalized_entry)
            if child["files"] or child["dirs"]:
                node["dirs"].append(child)
        elif entry.is_file(follow_symlinks=False):
            node["files"].append(
                {
                    "path": Path(entry.path),
                    "name": normalized_entry,
                    "name_bytes": encoded_entry,
                    "sort_key": entry_sort_key,
                    "size": entry.stat(follow_symlinks=False).st_size,
                }
            )
        else:
            raise RuntimeError("Unsupported RomFS entry: %s" % entry.path)
    return node


def _romfs_walk_directories(node):
    yield node
    for child in node["dirs"]:
        yield from _romfs_walk_directories(child)


def _romfs_hash_table_count(count):
    if count < 3:
        return 3
    if count < 19:
        return count | 1
    while any((count % prime == 0 for prime in (2, 3, 5, 7, 11, 13, 17))):
        count += 1
    return count


def _romfs_path_hash(parent, name_bytes):
    value = (parent ^ 123456789) & 4294967295
    for index in range(0, len(name_bytes), 2):
        code_unit = name_bytes[index] | name_bytes[index + 1] << 8
        value = (value >> 5 | value << 27 & 4294967295) & 4294967295
        value ^= code_unit
    return value


def _romfs_padded_name(name_bytes):
    return name_bytes + bytes(
        _romfs_align(len(name_bytes), 4) - len(name_bytes)
    )


def _romfs_build_metadata(root):
    directories = list(_romfs_walk_directories(root))
    files = [
        file_node
        for directory in directories
        for file_node in directory["files"]
    ]
    dir_hash = [ROMFS_UNUSED_ENTRY] * _romfs_hash_table_count(len(directories))
    file_hash = [ROMFS_UNUSED_ENTRY] * _romfs_hash_table_count(len(files))
    dir_table = bytearray()
    file_table = bytearray()
    data_cursor = 0
    data_order = []

    def add_directory(node, parent, sibling):
        offset = len(dir_table)
        bucket = _romfs_path_hash(parent, node["name_bytes"]) % len(dir_hash)
        dir_table.extend(
            struct.pack(
                "<6I",
                parent,
                sibling,
                ROMFS_UNUSED_ENTRY,
                ROMFS_UNUSED_ENTRY,
                dir_hash[bucket],
                len(node["name_bytes"]),
            )
        )
        dir_table.extend(_romfs_padded_name(node["name_bytes"]))
        dir_hash[bucket] = offset
        return offset

    def add_file(node, parent, sibling):
        nonlocal data_cursor
        offset = len(file_table)
        bucket = _romfs_path_hash(parent, node["name_bytes"]) % len(file_hash)
        if node["size"]:
            data_cursor = _romfs_align(data_cursor, 16)
            data_offset = data_cursor
            data_cursor += node["size"]
            data_order.append(node)
        else:
            data_offset = 0
        node["data_offset"] = data_offset
        file_table.extend(
            struct.pack(
                "<IIQQII",
                parent,
                sibling,
                data_offset,
                node["size"],
                file_hash[bucket],
                len(node["name_bytes"]),
            )
        )
        file_table.extend(_romfs_padded_name(node["name_bytes"]))
        file_hash[bucket] = offset

    def set_dir_field(directory_offset, field_index, value):
        struct.pack_into(
            "<I", dir_table, directory_offset + field_index * 4, value
        )

    def add_children(node, directory_offset):
        if node["files"]:
            set_dir_field(directory_offset, 3, len(file_table))
            for index, file_node in enumerate(node["files"]):
                sibling = (
                    ROMFS_UNUSED_ENTRY
                    if index + 1 == len(node["files"])
                    else len(file_table)
                    + 32
                    + _romfs_align(len(file_node["name_bytes"]), 4)
                )
                add_file(file_node, directory_offset, sibling)
        child_offsets = []
        if node["dirs"]:
            set_dir_field(directory_offset, 2, len(dir_table))
            for index, child in enumerate(node["dirs"]):
                sibling = (
                    ROMFS_UNUSED_ENTRY
                    if index + 1 == len(node["dirs"])
                    else len(dir_table)
                    + 24
                    + _romfs_align(len(child["name_bytes"]), 4)
                )
                child_offsets.append(
                    add_directory(child, directory_offset, sibling)
                )
            for child, child_offset in zip(node["dirs"], child_offsets):
                add_children(child, child_offset)

    root_offset = add_directory(root, 0, ROMFS_UNUSED_ENTRY)
    add_children(root, root_offset)
    return (
        dir_hash,
        dir_table,
        file_hash,
        file_table,
        data_order,
        data_cursor,
    )


def _hash_romfs_aligned_blocks(data) -> bytes:
    view = memoryview(data)
    try:
        if len(view) % ROMFS_BLOCK_SIZE:
            raise ValueError("RomFS hash batch is not block aligned")
        result = bytearray(len(view) // ROMFS_BLOCK_SIZE * 32)
        output_pos = 0
        for pos in range(0, len(view), ROMFS_BLOCK_SIZE):
            result[output_pos : output_pos + 32] = hashlib.sha256(
                view[pos : pos + ROMFS_BLOCK_SIZE]
            ).digest()
            output_pos += 32
        return bytes(result)
    finally:
        view.release()


def _choose_romfs_hash_workers(level3_size: int) -> int:
    requested = int(ROMFS_HASH_WORKERS)
    if requested > 0:
        return max(1, min(requested, ROMFS_HASH_MAX_AUTO_WORKERS))
    cpu_count = os.cpu_count() or 1
    if level3_size < ROMFS_HASH_PARALLEL_MIN_BYTES or cpu_count < 4:
        return 1
    return min(2, ROMFS_HASH_MAX_AUTO_WORKERS)


class _RomfsBlockHashes:
    """Collect ordered SHA-256 hashes for 4 KiB zero-padded stream blocks."""

    def __init__(self, worker_count=1):
        self.pending = bytearray()
        self.hashes = bytearray()
        self.worker_count = max(1, int(worker_count))
        self.executor = None
        self.futures = []
        if self.worker_count > 1:
            self.executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self.worker_count, thread_name_prefix="RomFSHash"
            )

    def _drain_one(self) -> None:
        future = self.futures.pop(0)
        self.hashes.extend(future.result())

    def _submit(self, data) -> None:
        if self.executor is None:
            self.hashes.extend(_hash_romfs_aligned_blocks(data))
            return
        self.futures.append(
            self.executor.submit(_hash_romfs_aligned_blocks, data)
        )
        if len(self.futures) >= self.worker_count * 3:
            self._drain_one()

    def update(self, data) -> None:
        if not data:
            return
        view = memoryview(data)
        try:
            pos = 0
            if self.pending:
                needed = ROMFS_BLOCK_SIZE - len(self.pending)
                take = min(needed, len(view))
                self.pending.extend(view[:take])
                pos += take
                if len(self.pending) == ROMFS_BLOCK_SIZE:
                    self._submit(bytes(self.pending))
                    self.pending.clear()
            full_bytes = (
                (len(view) - pos) // ROMFS_BLOCK_SIZE * ROMFS_BLOCK_SIZE
            )
            full_end = pos + full_bytes
            while pos < full_end:
                end = min(full_end, pos + ROMFS_HASH_BATCH_BYTES)
                end -= (end - pos) % ROMFS_BLOCK_SIZE
                self._submit(bytes(view[pos:end]))
                pos = end
            if pos < len(view):
                self.pending.extend(view[pos:])
        finally:
            view.release()

    def finish(self) -> bytes:
        if self.pending:
            self.pending.extend(bytes(ROMFS_BLOCK_SIZE - len(self.pending)))
            self._submit(bytes(self.pending))
            self.pending.clear()
        while self.futures:
            self._drain_one()
        if self.executor is not None:
            self.executor.shutdown(wait=True)
            self.executor = None
        return bytes(self.hashes)


def _romfs_hash_blob(blob) -> bytes:
    hashes = _RomfsBlockHashes()
    hashes.update(blob)
    return hashes.finish()


def _write_zero_bytes(write_cb, length: int) -> None:
    if length <= 0:
        return
    zero_block = bytes(min(IO_CHUNK, length))
    remaining = length
    while remaining:
        chunk = zero_block[: min(len(zero_block), remaining)]
        write_cb(chunk)
        remaining -= len(chunk)


def build_deterministic_romfs(
    root_path,
    output_path,
    deferred_validation=None,
    validation_progress_cb=None,
):
    """
    Build the same canonical RomFS while hashing Level 3 during its write pass.
    """
    root_path = Path(root_path)
    root = _romfs_collect_directory(root_path)
    if not root["files"] and (not root["dirs"]):
        raise RuntimeError("RomFS contains no files")
    dir_hash, dir_table, file_hash, file_table, data_order, data_size = (
        _romfs_build_metadata(root)
    )
    dir_hash_blob = struct.pack("<%dI" % len(dir_hash), *dir_hash)
    file_hash_blob = struct.pack("<%dI" % len(file_hash), *file_hash)
    dir_hash_offset = 40
    dir_table_offset = dir_hash_offset + len(dir_hash_blob)
    file_hash_offset = dir_table_offset + len(dir_table)
    file_table_offset = file_hash_offset + len(file_hash_blob)
    data_offset = _romfs_align(file_table_offset + len(file_table), 16)
    info_header = struct.pack(
        "<10I",
        40,
        dir_hash_offset,
        len(dir_hash_blob),
        dir_table_offset,
        len(dir_table),
        file_hash_offset,
        len(file_hash_blob),
        file_table_offset,
        len(file_table),
        data_offset,
    )
    level3_size = data_offset + data_size
    level2_size = (
        _romfs_align(level3_size, ROMFS_BLOCK_SIZE) // ROMFS_BLOCK_SIZE * 32
    )
    level1_size = (
        _romfs_align(level2_size, ROMFS_BLOCK_SIZE) // ROMFS_BLOCK_SIZE * 32
    )
    master_hash_size = (
        _romfs_align(level1_size, ROMFS_BLOCK_SIZE) // ROMFS_BLOCK_SIZE * 32
    )
    level0_size = ROMFS_IVFC_HEADER_ALIGNED_SIZE + master_hash_size
    level3_offset = _romfs_align(level0_size, ROMFS_BLOCK_SIZE)
    level1_offset = level3_offset + _romfs_align(level3_size, ROMFS_BLOCK_SIZE)
    level2_offset = level1_offset + _romfs_align(level1_size, ROMFS_BLOCK_SIZE)
    output_size = level2_offset + _romfs_align(level2_size, ROMFS_BLOCK_SIZE)
    level1_logical = 0
    level2_logical = _romfs_align(level1_size, ROMFS_BLOCK_SIZE)
    level3_logical = _romfs_align(
        level2_logical + level2_size, ROMFS_BLOCK_SIZE
    )
    ivfc_header = bytearray(ROMFS_IVFC_HEADER_SIZE)
    struct.pack_into("<4sII", ivfc_header, 0, b"IVFC", 65536, master_hash_size)
    for index, (logical, size) in enumerate(
        (
            (level1_logical, level1_size),
            (level2_logical, level2_size),
            (level3_logical, level3_size),
        )
    ):
        struct.pack_into(
            "<QQI", ivfc_header, 12 + index * 24, logical, size, 12
        )
    struct.pack_into("<I", ivfc_header, 84, ROMFS_IVFC_HEADER_SIZE)
    deferred = dict(deferred_validation or {})
    validation_total = sum((int(entry["size"]) for entry in deferred.values()))
    validation_done = 0
    if validation_progress_cb and validation_total:
        validation_progress_cb(0, validation_total)
    hash_workers = _choose_romfs_hash_workers(level3_size)
    output_path = Path(output_path)
    with output_path.open("w+b", buffering=0) as output:
        output.truncate(output_size)
        output.seek(0)
        output.write(ivfc_header)
        output.seek(level3_offset)
        level3_hashes = _RomfsBlockHashes(hash_workers)
        level3_position = 0

        def write_level3(data) -> None:
            nonlocal level3_position
            output.write(data)
            level3_hashes.update(data)
            level3_position += len(data)

        for blob in (
            info_header,
            dir_hash_blob,
            dir_table,
            file_hash_blob,
            file_table,
        ):
            write_level3(blob)
        _write_zero_bytes(write_level3, data_offset - level3_position)
        for file_node in data_order:
            expected_position = data_offset + int(file_node["data_offset"])
            if level3_position > expected_position:
                raise RuntimeError("Internal RomFS file-data ordering error")
            _write_zero_bytes(write_level3, expected_position - level3_position)
            rel = file_node["path"].relative_to(root_path).as_posix()
            validation_entry = deferred.pop(rel.casefold(), None)
            sha = hashlib.sha256() if validation_entry is not None else None
            crc = 0
            remaining = int(file_node["size"])
            with file_node["path"].open("rb") as source:
                while remaining:
                    chunk = source.read(min(IO_CHUNK, remaining))
                    if not chunk:
                        raise RuntimeError(
                            "RomFS file changed while it was being read: %s"
                            % file_node["path"]
                        )
                    if validation_entry is not None:
                        sha.update(chunk)
                        crc = zlib.crc32(chunk, crc) & 4294967295
                        validation_done += len(chunk)
                        if validation_progress_cb:
                            validation_progress_cb(
                                validation_done, max(1, validation_total)
                            )
                    write_level3(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise RuntimeError(
                        "RomFS file changed while it was being read: %s"
                        % file_node["path"]
                    )
            if validation_entry is not None:
                if (
                    sha.hexdigest().casefold()
                    != validation_entry["sha256"].casefold()
                ):
                    raise RuntimeError(
                        "Extracted file SHA-256 mismatch for: %s"
                        % validation_entry["path"]
                    )
                if crc != int(validation_entry["crc32"]):
                    raise RuntimeError(
                        "Extracted file CRC32 mismatch for: %s"
                        % validation_entry["path"]
                    )
        _write_zero_bytes(write_level3, level3_size - level3_position)
        _write_zero_bytes(
            write_level3,
            _romfs_align(level3_size, ROMFS_BLOCK_SIZE) - level3_position,
        )
        level2_blob = level3_hashes.finish()
        if len(level2_blob) != level2_size:
            raise RuntimeError("Internal RomFS Level 2 hash-size mismatch")
        if deferred:
            first = next(iter(deferred.values()))
            raise RuntimeError(
                "RomFS validation file was not written: %s" % first["path"]
            )
        if validation_progress_cb and validation_total:
            validation_progress_cb(validation_total, validation_total)
        level1_blob = _romfs_hash_blob(level2_blob)
        master_hash_blob = _romfs_hash_blob(level1_blob)
        if len(level1_blob) != level1_size:
            raise RuntimeError("Internal RomFS Level 1 hash-size mismatch")
        if len(master_hash_blob) != master_hash_size:
            raise RuntimeError("Internal RomFS master hash-size mismatch")
        output.seek(level2_offset)
        output.write(level2_blob)
        output.seek(level1_offset)
        output.write(level1_blob)
        output.seek(ROMFS_IVFC_HEADER_ALIGNED_SIZE)
        output.write(master_hash_blob)
    return output_size
