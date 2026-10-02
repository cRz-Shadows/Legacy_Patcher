"""Optional native LZ11 acceleration with portable fallbacks."""

from __future__ import annotations

import ctypes
import os
import struct
import threading
import zlib
from pathlib import Path

from .lz11 import lz11_decode_standard as decode_standard
from .lz11 import rebuild_recipe
from .runtime import app_dir, bundle_dir
from .toolchain import get_platform_tools, tools_root

NATIVE_LZ11_MIN_BYTES = 0
_NATIVE_LZ11_API_VERSION = 1
_NATIVE_LZ11_LIBRARY = None
_NATIVE_LZ11_ATTEMPTED = False
_NATIVE_LZ11_PATH = None
_NATIVE_LZ11_ERROR = None
_NATIVE_LZ11_LOCK = threading.Lock()


def _native_lz11_candidates() -> list[Path]:
    try:
        target = get_platform_tools()
    except RuntimeError:
        return []
    roots = list(dict.fromkeys((bundle_dir(), app_dir())))
    return [
        tools_root(root) / target.folder / target.native_library
        for root in roots
    ]


def _native_lz11_self_test(lib) -> None:
    packed = b"\x11\x08\x00\x00\x00ABCDEFGH"
    decoded = bytearray(8)
    decoded_buf = (ctypes.c_ubyte * len(decoded)).from_buffer(decoded)
    consumed = ctypes.c_size_t(0)
    rc = lib.legacy_lz11_decode(
        ctypes.c_char_p(packed),
        len(packed),
        decoded_buf,
        len(decoded),
        ctypes.byref(consumed),
    )
    if (
        rc != 0
        or consumed.value != len(packed)
        or bytes(decoded) != b"ABCDEFGH"
    ):
        raise RuntimeError("native LZ11 decode self-test failed")
    recipe = b"LZR2" + struct.pack("<QQII", 8, len(packed), 1, 0) + b"\x00"
    rebuilt = bytearray(len(packed))
    rebuilt_buf = (ctypes.c_ubyte * len(rebuilt)).from_buffer(rebuilt)
    rc = lib.legacy_lz11_rebuild_lzr2(
        ctypes.c_char_p(b"ABCDEFGH"),
        8,
        ctypes.c_char_p(recipe),
        len(recipe),
        rebuilt_buf,
        len(rebuilt),
    )
    if rc != 0 or bytes(rebuilt) != packed:
        raise RuntimeError("native LZ11 recipe self-test failed")


def _get_native_lz11_library():
    global _NATIVE_LZ11_LIBRARY, _NATIVE_LZ11_ATTEMPTED
    global _NATIVE_LZ11_PATH, _NATIVE_LZ11_ERROR
    if _NATIVE_LZ11_ATTEMPTED:
        return _NATIVE_LZ11_LIBRARY
    with _NATIVE_LZ11_LOCK:
        if _NATIVE_LZ11_ATTEMPTED:
            return _NATIVE_LZ11_LIBRARY
        _NATIVE_LZ11_ATTEMPTED = True
        if os.environ.get("LEGACY_LZ11_DISABLE_NATIVE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            _NATIVE_LZ11_ERROR = "disabled by LEGACY_LZ11_DISABLE_NATIVE"
            return None
        errors = []
        for candidate in _native_lz11_candidates():
            if not candidate.is_file():
                continue
            try:
                lib = load_native_library(candidate)
                _NATIVE_LZ11_LIBRARY = lib
                _NATIVE_LZ11_PATH = candidate
                _NATIVE_LZ11_ERROR = None
                return lib
            except Exception as exc:
                errors.append("%s: %s" % (candidate, exc))
        _NATIVE_LZ11_ERROR = "; ".join(errors) if errors else "helper not found"
        return None


def load_native_library(path: Path):
    """Load the LZ11 library and validate its exported interface."""
    lib = ctypes.CDLL(str(path))
    lib.legacy_lz11_api_version.argtypes = []
    lib.legacy_lz11_api_version.restype = ctypes.c_uint32
    lib.legacy_lz11_decode.argtypes = (
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    )
    lib.legacy_lz11_decode.restype = ctypes.c_int
    lib.legacy_lz11_rebuild_lzr2.argtypes = (
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
    )
    lib.legacy_lz11_rebuild_lzr2.restype = ctypes.c_int
    if lib.legacy_lz11_api_version() != _NATIVE_LZ11_API_VERSION:
        raise RuntimeError("unsupported helper API version")
    _native_lz11_self_test(lib)
    return lib


def lz11_decode_standard(blob: bytes) -> tuple[bytes, int]:
    if len(blob) < 4 or blob[0] != 17:
        raise ValueError("not standard LZ11")
    raw_size = blob[1] | blob[2] << 8 | blob[3] << 16
    if raw_size <= 0:
        raise ValueError("unsupported/invalid LZ11 size")
    lib = (
        _get_native_lz11_library()
        if raw_size >= NATIVE_LZ11_MIN_BYTES
        else None
    )
    if lib is None:
        return decode_standard(blob)
    out = bytearray(raw_size)
    out_buf = (ctypes.c_ubyte * raw_size).from_buffer(out)
    consumed = ctypes.c_size_t(0)
    rc = lib.legacy_lz11_decode(
        ctypes.c_char_p(blob),
        len(blob),
        out_buf,
        raw_size,
        ctypes.byref(consumed),
    )
    if rc == -3:
        raise ValueError("truncated LZ11 input")
    if rc == -4:
        raise ValueError("invalid LZ11 back-reference")
    if rc != 0:
        raise ValueError("invalid LZ11 stream (native error %d)" % rc)
    return (bytes(out), int(consumed.value))


def rebuild_lz11_from_recipe(raw: bytes, compressed_recipe: bytes) -> bytes:
    recipe = zlib.decompress(compressed_recipe)
    if len(recipe) < 28 or recipe[:4] != b"LZR2":
        raise ValueError("invalid LZ11 recipe")
    raw_size, target_size, flags_len, refs_len = struct.unpack_from(
        "<QQII", recipe, 4
    )
    if 28 + flags_len + refs_len != len(recipe):
        raise ValueError("invalid LZ11 recipe lengths")
    if len(raw) != raw_size:
        raise ValueError("raw member size does not match LZ11 recipe")
    lib = (
        _get_native_lz11_library()
        if raw_size >= NATIVE_LZ11_MIN_BYTES
        else None
    )
    if lib is None:
        return rebuild_recipe(raw, recipe)
    packed = bytearray(target_size)
    packed_buf = (ctypes.c_ubyte * target_size).from_buffer(packed)
    rc = lib.legacy_lz11_rebuild_lzr2(
        ctypes.c_char_p(raw),
        len(raw),
        ctypes.c_char_p(recipe),
        len(recipe),
        packed_buf,
        target_size,
    )
    if rc == -3:
        raise ValueError("truncated LZ11 recipe token")
    if rc != 0:
        raise ValueError("LZ11 recipe output mismatch (native error %d)" % rc)
    return bytes(packed)
