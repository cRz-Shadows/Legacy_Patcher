"""Source-referencing raw deltas and XOR changes."""

from __future__ import annotations

import hashlib
import struct
import zlib

from .config import XOR_CHUNK
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


def _common_prefix_bytes(
    a: bytes, source_offset: int, b: bytes, target_offset: int, limit: int
) -> int:
    matched = 0
    chunk = 1024 * 1024
    while (
        matched + chunk <= limit
        and a[source_offset + matched : source_offset + matched + chunk]
        == b[target_offset + matched : target_offset + matched + chunk]
    ):
        matched += chunk
    while (
        matched < limit
        and a[source_offset + matched] == b[target_offset + matched]
    ):
        matched += 1
    return matched


def build_raw_delta(
    source: bytes, target: bytes, block_size: int = 32 * 1024
) -> bytes:
    """
    Source-referencing rolling delta for the uncommon size-changing member.
    """
    if block_size < 1024:
        raise ValueError("raw delta block size is too small")
    source_index: dict[int, list[tuple[bytes, int]]] = {}
    if len(source) >= block_size:
        for off in range(0, len(source) - block_size + 1, block_size):
            block = source[off : off + block_size]
            weak = zlib.adler32(block) & 0xFFFFFFFF
            strong = hashlib.blake2b(block, digest_size=16).digest()
            source_index.setdefault(weak, []).append((strong, off))

    ops: list[tuple[int, int, bytes | int]] = []
    p = 0
    literal_start = 0
    target_len = len(target)
    progress = ConsoleProgress(max(1, target_len), "size-changing member delta")

    while p + block_size <= target_len and source_index:
        window = target[p : p + block_size]
        weak = zlib.adler32(window) & 0xFFFFFFFF
        match_off = None
        candidates = source_index.get(weak)
        if candidates:
            strong = hashlib.blake2b(window, digest_size=16).digest()
            for candidate_strong, off in candidates:
                if (
                    candidate_strong == strong
                    and source[off : off + block_size] == window
                ):
                    match_off = off
                    break
        if match_off is None:
            p += 1
            progress.update(p)
            continue

        if literal_start < p:
            ops.append((1, 0, target[literal_start:p]))
        max_len = min(len(source) - match_off, target_len - p)
        length = block_size + _common_prefix_bytes(
            source,
            match_off + block_size,
            target,
            p + block_size,
            max_len - block_size,
        )
        ops.append((0, match_off, length))
        p += length
        literal_start = p
        progress.update(p)

    if literal_start < target_len:
        ops.append((1, 0, target[literal_start:]))
    progress.update(target_len, force=True)

    out = bytearray(b"RDP2")
    out += struct.pack("<QQII", len(source), len(target), block_size, len(ops))
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
