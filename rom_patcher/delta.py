"""Source-referencing raw deltas and XOR changes."""

from __future__ import annotations

import struct
import zlib
from typing import Iterator

from .config import (
    DELTA_EXTEND_CHUNK,
    DELTA_INDEX_MAX_ENTRIES,
    DELTA_INDEX_MIN_BLOCK,
    XOR_CHUNK,
)
from .progress import ConsoleProgress


def _xor_chunk(a: bytes, b: bytes) -> bytes:
    if len(a) != len(b):
        raise ValueError("XOR chunks must have equal lengths")
    if not a:
        return b""

    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(
        len(a), "little"
    )


def build_xor_zlib(source: bytes, target: bytes) -> bytes:
    if len(source) != len(target):
        raise ValueError("XOR patch requires equal raw sizes")
    comp = zlib.compressobj(level=9)
    out = bytearray()
    for pos in range(0, len(source), XOR_CHUNK):
        end = min(len(source), pos + XOR_CHUNK)
        out += comp.compress(_xor_chunk(source[pos:end], target[pos:end]))
    out += comp.flush()
    return bytes(out)


def apply_xor_zlib(source: bytes, blob: bytes, expected_size: int) -> bytes:
    xor_data = zlib.decompress(blob)
    if len(source) != expected_size or len(xor_data) != expected_size:
        raise ValueError("XOR patch size mismatch")
    out = bytearray(expected_size)
    for pos in range(0, expected_size, XOR_CHUNK):
        end = min(expected_size, pos + XOR_CHUNK)
        out[pos:end] = _xor_chunk(source[pos:end], xor_data[pos:end])
    return bytes(out)


def _count_common_prefix(
    source: memoryview,
    target: memoryview,
    s_pos: int,
    t_pos: int,
    max_len: int,
) -> int:
    lo, hi = 0, max_len
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if source[s_pos : s_pos + mid] == target[t_pos : t_pos + mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def extend_match(
    source: memoryview,
    target: memoryview,
    s_pos: int,
    t_pos: int,
    max_len: int,
) -> int:
    matched = 0
    while matched + DELTA_EXTEND_CHUNK <= max_len:
        if (
            source[s_pos + matched : s_pos + matched + DELTA_EXTEND_CHUNK]
            == target[t_pos + matched : t_pos + matched + DELTA_EXTEND_CHUNK]
        ):
            matched += DELTA_EXTEND_CHUNK
        else:
            break
    rem = min(DELTA_EXTEND_CHUNK, max_len - matched)
    if rem > 0:
        matched += _count_common_prefix(
            source, target, s_pos + matched, t_pos + matched, rem
        )
    return matched


def _choose_index_block_size(source_size: int) -> int:
    """Use longer blocks for large sources to bound the index size."""
    block = DELTA_INDEX_MIN_BLOCK
    while source_size // block > DELTA_INDEX_MAX_ENTRIES:
        block *= 2
    return block


def _build_source_index(
    source: memoryview, block: int
) -> tuple[dict[int, int], dict[int, list[int]]]:
    index: dict[int, int] = {}
    collisions: dict[int, list[int]] = {}
    for offset in range(0, len(source) - block + 1, block):
        window = source[offset : offset + block]
        checksum = zlib.crc32(window)
        candidate = index.get(checksum)
        if candidate is None:
            index[checksum] = offset
            continue
        candidates = collisions.get(checksum, (candidate,))
        if all(
            source[position : position + block] != window
            for position in candidates
        ):
            collisions.setdefault(checksum, [candidate]).append(offset)
    return index, collisions


def iter_source_runs(
    source: bytes | memoryview, target: bytes | memoryview
) -> Iterator[tuple[int, int, int]]:
    """
    Yield (target offset, source offset, length) for each run of target
    bytes that the source can supply, in target order.

    Equal bytes at the same offset are always copied. Moved runs use the
    previous source offset or aligned index blocks, extended backwards into
    pending literals. Short moved runs without an indexed block can remain
    literal data.
    """
    source = memoryview(source)
    target = memoryview(target)
    block = _choose_index_block_size(len(source))
    index = None
    collisions: dict[int, list[int]] = {}
    shift = 0
    uncovered = 0

    def build_run(s_pos: int, t_pos: int) -> tuple[int, int, int]:
        max_len = min(len(target) - t_pos, len(source) - s_pos)
        length = extend_match(source, target, s_pos, t_pos, max_len)
        return (t_pos, s_pos, length)

    def find_continuing_run(position: int) -> tuple[int, int, int] | None:
        best = None
        offsets = (position,) if shift == 0 else (position, position + shift)
        for s_pos in offsets:
            if 0 <= s_pos < len(source) and source[s_pos] == target[position]:
                run = build_run(s_pos, position)
                if best is None or run[2] > best[2]:
                    best = run
        return best

    def find_indexed_run(start: int, stop: int) -> tuple[int, int, int] | None:
        """Find a moved run without overlapping previously yielded data."""
        nonlocal index, collisions
        stop = min(stop, len(target) - block + 1)
        if start >= stop:
            return None
        if index is None:
            index, collisions = _build_source_index(source, block)
        for position in range(start, stop):
            window = target[position : position + block]
            checksum = zlib.crc32(window)
            candidate = index.get(checksum)
            if candidate is None:
                continue
            s_pos = next(
                (
                    offset
                    for offset in collisions.get(checksum, (candidate,))
                    if source[offset : offset + block] == window
                ),
                None,
            )
            if s_pos is None:
                continue
            t_pos, s_pos, length = build_run(s_pos, position)
            while (
                t_pos > uncovered
                and s_pos > 0
                and source[s_pos - 1] == target[t_pos - 1]
            ):
                t_pos -= 1
                s_pos -= 1
                length += 1
            return (t_pos, s_pos, length)
        return None

    position = 0
    while position < len(target):
        run = find_continuing_run(position)
        if run is None:
            run = find_indexed_run(position, position + 1)
        elif run[2] < block:
            # An index hit can recover moved data hidden by a chance match.
            moved = find_indexed_run(position, position + block)
            if moved is not None and moved[0] <= position:
                run = moved
        if run is None:
            position += 1
            continue
        t_pos, s_pos, length = run
        yield run
        if s_pos != t_pos:
            shift = s_pos - t_pos
        position = uncovered = t_pos + length


def build_raw_delta(source: bytes, target: bytes) -> bytes:
    """Build a compressed RDP2 delta for a size-changing member."""
    ops: list[tuple[int, int, bytes | int]] = []
    written = 0
    progress = ConsoleProgress(
        max(1, len(target)), "size-changing member delta"
    )
    for t_pos, s_pos, length in iter_source_runs(source, target):
        if written < t_pos:
            ops.append((1, 0, target[written:t_pos]))
        ops.append((0, s_pos, length))
        written = t_pos + length
        progress.update(written)
    if written < len(target):
        ops.append((1, 0, target[written:]))
    progress.update(len(target), force=True)

    out = bytearray(b"RDP2")
    # The block-size field is kept for the format; apply_raw_delta ignores it.
    out += struct.pack("<QQII", len(source), len(target), 0, len(ops))
    for kind, offset, value in ops:
        out.append(kind)
        if kind == 0:
            out += struct.pack("<QQ", offset, int(value))
        else:
            data = bytes(value)
            out += struct.pack("<Q", len(data))
            out += data
    return zlib.compress(bytes(out), 9)


def apply_raw_delta(source: bytes, compressed_patch: bytes) -> bytes:
    patch = zlib.decompress(compressed_patch)
    if len(patch) < 28 or patch[:4] != b"RDP2":
        raise ValueError("invalid raw delta")
    source_size, target_size, _block_size, op_count = struct.unpack_from(
        "<QQII", patch, 4
    )
    if len(source) != source_size:
        raise ValueError("raw delta source size mismatch")
    pos = 28
    out = bytearray()
    for _ in range(op_count):
        if pos >= len(patch):
            raise ValueError("truncated raw delta")
        kind = patch[pos]
        pos += 1
        if kind == 0:
            if pos + 16 > len(patch):
                raise ValueError("truncated raw copy operation")
            offset, length = struct.unpack_from("<QQ", patch, pos)
            pos += 16
            if offset + length > len(source):
                raise ValueError("raw copy outside source")
            out += source[offset : offset + length]
        elif kind == 1:
            if pos + 8 > len(patch):
                raise ValueError("truncated raw data operation")
            length = struct.unpack_from("<Q", patch, pos)[0]
            pos += 8
            if pos + length > len(patch):
                raise ValueError("truncated raw literal")
            out += patch[pos : pos + length]
            pos += length
        else:
            raise ValueError("unknown raw delta operation")
    if pos != len(patch) or len(out) != target_size:
        raise ValueError("raw delta output mismatch")
    return bytes(out)
