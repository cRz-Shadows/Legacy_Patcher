"""Portable LZ11 decoding and exact compressor recipes."""

from __future__ import annotations

import struct
import zlib
from typing import Optional


def lz11_decode_standard(blob: bytes) -> tuple[bytes, int]:
    if len(blob) < 4 or blob[0] != 0x11:
        raise ValueError("not standard LZ11")
    raw_size = blob[1] | (blob[2] << 8) | (blob[3] << 16)
    if raw_size <= 0:
        raise ValueError("unsupported/invalid LZ11 size")

    out = bytearray(raw_size)
    in_pos = 4
    out_pos = 0
    blob_len = len(blob)
    while out_pos < raw_size:
        if in_pos >= blob_len:
            raise ValueError("truncated LZ11 flags")
        flags = blob[in_pos]
        in_pos += 1

        if flags == 0:
            count = min(8, raw_size - out_pos)
            if in_pos + count > blob_len:
                raise ValueError("truncated LZ11 literal group")
            out[out_pos : out_pos + count] = blob[in_pos : in_pos + count]
            in_pos += count
            out_pos += count
            continue

        for bit in range(7, -1, -1):
            if out_pos >= raw_size:
                break
            if not (flags & (1 << bit)):
                if in_pos >= blob_len:
                    raise ValueError("truncated LZ11 literal")
                out[out_pos] = blob[in_pos]
                in_pos += 1
                out_pos += 1
                continue

            if in_pos >= blob_len:
                raise ValueError("truncated LZ11 token")
            b1 = blob[in_pos]
            hi = b1 >> 4
            if hi == 0:
                if in_pos + 3 > blob_len:
                    raise ValueError("truncated LZ11 long token")
                b2, b3 = blob[in_pos + 1], blob[in_pos + 2]
                length = (((b1 & 0x0F) << 4) | (b2 >> 4)) + 0x11
                disp = ((b2 & 0x0F) << 8) | b3
                token_len = 3
            elif hi == 1:
                if in_pos + 4 > blob_len:
                    raise ValueError("truncated LZ11 very-long token")
                b2, b3, b4 = (
                    blob[in_pos + 1],
                    blob[in_pos + 2],
                    blob[in_pos + 3],
                )
                length = (((b1 & 0x0F) << 12) | (b2 << 4) | (b3 >> 4)) + 0x111
                disp = ((b3 & 0x0F) << 8) | b4
                token_len = 4
            else:
                if in_pos + 2 > blob_len:
                    raise ValueError("truncated LZ11 short token")
                b2 = blob[in_pos + 1]
                length = hi + 1
                disp = ((b1 & 0x0F) << 8) | b2
                token_len = 2
            in_pos += token_len
            distance = disp + 1
            if distance <= 0 or distance > out_pos:
                raise ValueError("invalid LZ11 back-reference")
            actual = min(length, raw_size - out_pos)
            src = out_pos - distance
            if actual <= distance:
                out[out_pos : out_pos + actual] = out[src : src + actual]
            else:
                pattern = bytes(out[src:out_pos])
                if not pattern:
                    raise ValueError("empty LZ11 repeat pattern")
                reps = (actual + len(pattern) - 1) // len(pattern)
                out[out_pos : out_pos + actual] = (pattern * reps)[:actual]
            out_pos += actual
    return bytes(out), in_pos


def try_lz11(blob: bytes) -> Optional[tuple[bytes, int]]:
    try:
        return lz11_decode_standard(blob)
    except (ValueError, IndexError, struct.error):
        return None


def make_lz11_recipe(target_blob: bytes) -> bytes:
    """
    Store target compressor decisions, but deliberately omit every literal
    byte.
    """
    decoded = try_lz11(target_blob)
    if decoded is None:
        raise ValueError("target is not valid standard LZ11")
    raw, consumed = decoded
    if consumed != len(target_blob):
        raise ValueError(
            "target LZ11 member has trailing bytes; refusing to embed a"
            " direct suffix copy"
        )

    flags_out = bytearray()
    refs_out = bytearray()
    in_pos = 4
    out_pos = 0
    raw_size = len(raw)
    while out_pos < raw_size:
        flags = target_blob[in_pos]
        in_pos += 1
        flags_out.append(flags)
        for bit in range(7, -1, -1):
            if out_pos >= raw_size:
                break
            if not (flags & (1 << bit)):
                in_pos += 1
                out_pos += 1
                continue
            b1 = target_blob[in_pos]
            hi = b1 >> 4
            if hi == 0:
                token_len = 3
                b2 = target_blob[in_pos + 1]
                length = (((b1 & 0x0F) << 4) | (b2 >> 4)) + 0x11
            elif hi == 1:
                token_len = 4
                b2, b3 = target_blob[in_pos + 1], target_blob[in_pos + 2]
                length = (((b1 & 0x0F) << 12) | (b2 << 4) | (b3 >> 4)) + 0x111
            else:
                token_len = 2
                length = hi + 1
            refs_out += target_blob[in_pos : in_pos + token_len]
            in_pos += token_len
            out_pos = min(raw_size, out_pos + length)

    recipe = bytearray(b"LZR2")
    recipe += struct.pack(
        "<QQII", raw_size, len(target_blob), len(flags_out), len(refs_out)
    )
    recipe += flags_out
    recipe += refs_out
    return zlib.compress(bytes(recipe), 9)


def rebuild_recipe(raw: bytes, recipe: bytes) -> bytes:
    if len(recipe) < 28 or recipe[:4] != b"LZR2":
        raise ValueError("invalid LZ11 recipe")
    raw_size, target_size, flags_len, refs_len = struct.unpack_from(
        "<QQII", recipe, 4
    )
    pos = 28
    if pos + flags_len + refs_len != len(recipe):
        raise ValueError("invalid LZ11 recipe lengths")
    flags = memoryview(recipe)[pos : pos + flags_len]
    pos += flags_len
    refs = memoryview(recipe)[pos : pos + refs_len]
    if len(raw) != raw_size:
        raise ValueError("raw member size does not match LZ11 recipe")
    packed = bytearray(target_size)
    packed[0:4] = bytes(
        [17, raw_size & 255, raw_size >> 8 & 255, raw_size >> 16 & 255]
    )
    out_pos = 4
    raw_pos = 0
    ref_pos = 0
    for flag in flags:
        if raw_pos >= raw_size:
            break
        packed[out_pos] = flag
        out_pos += 1
        if flag == 0:
            count = min(8, raw_size - raw_pos)
            packed[out_pos : out_pos + count] = raw[raw_pos : raw_pos + count]
            out_pos += count
            raw_pos += count
            continue
        for bit in range(7, -1, -1):
            if raw_pos >= raw_size:
                break
            if not flag & 1 << bit:
                packed[out_pos] = raw[raw_pos]
                out_pos += 1
                raw_pos += 1
                continue
            if ref_pos >= refs_len:
                raise ValueError("truncated LZ11 recipe back-reference")
            b1 = refs[ref_pos]
            hi = b1 >> 4
            if hi == 0:
                token_len = 3
                if ref_pos + token_len > refs_len:
                    raise ValueError("truncated LZ11 recipe token")
                b2 = refs[ref_pos + 1]
                length = ((b1 & 15) << 4 | b2 >> 4) + 17
            elif hi == 1:
                token_len = 4
                if ref_pos + token_len > refs_len:
                    raise ValueError("truncated LZ11 recipe token")
                b2, b3 = (refs[ref_pos + 1], refs[ref_pos + 2])
                length = ((b1 & 15) << 12 | b2 << 4 | b3 >> 4) + 273
            else:
                token_len = 2
                if ref_pos + token_len > refs_len:
                    raise ValueError("truncated LZ11 recipe token")
                length = hi + 1
            packed[out_pos : out_pos + token_len] = refs[
                ref_pos : ref_pos + token_len
            ]
            out_pos += token_len
            ref_pos += token_len
            raw_pos = min(raw_size, raw_pos + length)
    if raw_pos != raw_size or ref_pos != refs_len or out_pos != target_size:
        raise ValueError("LZ11 recipe output mismatch")
    return bytes(packed)


def rebuild_lz11_from_recipe(raw: bytes, compressed_recipe: bytes) -> bytes:
    return rebuild_recipe(raw, zlib.decompress(compressed_recipe))
