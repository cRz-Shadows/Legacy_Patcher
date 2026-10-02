"""Exact GARC patch generation and canonical container metadata."""

from __future__ import annotations

import json
import mmap
import os
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import GARC_PATCH_FORMAT, GARC_PATCH_MIN_SOURCE_SIZE
from .delta import build_raw_delta, build_xor_zlib
from .files import sha256_file
from .lz11 import make_lz11_recipe, try_lz11
from .progress import ConsoleProgress


@dataclass(frozen=True)
class GARCRecord:
    key: str
    entry_index: int
    sub_index: int
    start: int
    end: int
    length: int
    abs_start: int
    abs_end: int


@dataclass(frozen=True)
class GARCInfo:
    version: int
    header_size: int
    byte_order: int
    section_count: int
    data_start: int
    file_size: int
    entry_count: int
    file_count: int
    records: tuple[GARCRecord, ...]


def _u16(buf, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def parse_garc(buf) -> GARCInfo:
    if len(buf) < 0x20 or _u32(buf, 0) != 0x47415243:
        raise ValueError("not a GARC file")
    header_size = _u32(buf, 4)
    byte_order = _u16(buf, 8)
    version = _u16(buf, 0x0A)
    section_count = _u32(buf, 0x0C)
    data_start = _u32(buf, 0x10)
    file_size = _u32(buf, 0x14)
    if version not in (0x0400, 0x0600):
        raise ValueError(f"unsupported GARC version 0x{version:04X}")
    if file_size != len(buf):
        raise ValueError("GARC file-size field does not match actual size")
    if section_count != 4:
        raise ValueError("unexpected GARC section count")

    fato_pos = header_size
    if fato_pos + 0x0C > len(buf) or _u32(buf, fato_pos) != 0x4641544F:
        raise ValueError("GARC FATO section missing")
    fato_size = _u32(buf, fato_pos + 4)
    entry_count = _u16(buf, fato_pos + 8)
    if fato_size != 0x0C + entry_count * 4:
        raise ValueError("invalid GARC FATO size")
    fato_offsets = [
        _u32(buf, fato_pos + 0x0C + i * 4) for i in range(entry_count)
    ]

    fatb_pos = fato_pos + fato_size
    if fatb_pos + 0x0C > len(buf) or _u32(buf, fatb_pos) != 0x46415442:
        raise ValueError("GARC FATB section missing")
    fatb_size = _u32(buf, fatb_pos + 4)
    file_count = _u32(buf, fatb_pos + 8)
    fatb_table = fatb_pos + 0x0C
    fatb_end = fatb_pos + fatb_size
    if fatb_end > len(buf):
        raise ValueError("truncated GARC FATB")

    fimb_pos = fatb_end
    if fimb_pos + 0x0C > len(buf) or _u32(buf, fimb_pos) != 0x46494D42:
        raise ValueError("GARC FIMB section missing")
    if data_start != fimb_pos + 0x0C:
        raise ValueError("GARC data offset does not match FIMB")
    fimb_size = _u32(buf, fimb_pos + 8)
    if data_start + fimb_size != len(buf):
        raise ValueError("GARC FIMB size does not match file")

    records: list[GARCRecord] = []
    for entry_index, rel in enumerate(fato_offsets):
        pos = fatb_table + rel
        if pos + 4 > fatb_end:
            raise ValueError("FATO entry points outside FATB")
        vector = _u32(buf, pos)
        pos += 4
        for sub_index in range(32):
            if not (vector & (1 << sub_index)):
                continue
            if pos + 12 > fatb_end:
                raise ValueError("truncated GARC FATB record")
            start, end, length = struct.unpack_from("<III", buf, pos)
            pos += 12
            if (
                start > end
                or length > end - start
                or data_start + end > len(buf)
            ):
                raise ValueError("invalid GARC payload range")
            records.append(
                GARCRecord(
                    key=f"{entry_index}:{sub_index}",
                    entry_index=entry_index,
                    sub_index=sub_index,
                    start=start,
                    end=end,
                    length=length,
                    abs_start=data_start + start,
                    abs_end=data_start + end,
                )
            )
    if len(records) != file_count:
        raise ValueError("GARC FATB file count mismatch")

    return GARCInfo(
        version=version,
        header_size=header_size,
        byte_order=byte_order,
        section_count=section_count,
        data_start=data_start,
        file_size=file_size,
        entry_count=entry_count,
        file_count=file_count,
        records=tuple(records),
    )


def _manifest_records_for_prefix(manifest_or_records) -> list[dict]:
    if isinstance(manifest_or_records, dict):
        return list(manifest_or_records["records"])
    return list(manifest_or_records)


def build_canonical_garc_prefix(
    version: int, entry_count: int, records
) -> bytes:
    """
    Rebuild the standard pk3DS-style GARC header/tables from integers only.
    """
    recs = _manifest_records_for_prefix(records)
    by_entry: list[list[dict]] = [[] for _ in range(entry_count)]
    for rec in recs:
        entry_index = int(rec["entry_index"])
        sub_index = int(rec["sub_index"])
        if not (0 <= entry_index < entry_count and 0 <= sub_index < 32):
            raise ValueError("invalid GARC manifest entry/sub index")
        by_entry[entry_index].append(rec)

    fato_offsets: list[int] = []
    fatb_table_size = 0
    data_cursor = 0
    largest_unpadded = 0
    largest_padded = 0
    table_rows: list[tuple[int, list[tuple[int, int, int]]]] = []

    for entry_index, entry_records in enumerate(by_entry):
        entry_records.sort(key=lambda r: int(r["sub_index"]))
        fato_offsets.append(fatb_table_size)
        fatb_table_size += 4
        vector = 0
        rows: list[tuple[int, int, int]] = []
        for rec in entry_records:
            sub_index = int(rec["sub_index"])
            vector |= 1 << sub_index
            length = int(rec["target_length"])
            padded_length = int(rec["target_padded_length"])
            if length < 0 or padded_length < length:
                raise ValueError("invalid GARC manifest payload length")
            start = data_cursor
            end = start + padded_length
            rows.append((start, end, length))
            data_cursor = end
            largest_unpadded = max(largest_unpadded, length)
            largest_padded = max(largest_padded, padded_length)
            fatb_table_size += 12
        table_rows.append((vector, rows))

    header_size = 0x24 if version == 0x0600 else 0x1C
    fato_size = 0x0C + entry_count * 4
    fatb_size = 0x0C + fatb_table_size

    out = bytearray()
    out += struct.pack("<I", 0x47415243)
    out += struct.pack("<I", header_size)
    out += struct.pack("<H", 0xFEFF)
    out += struct.pack("<H", version)
    out += struct.pack("<I", 4)
    out += b"\x00" * 12
    if version == 0x0600:
        out += b"\x00" * 8

    out += struct.pack("<IIHH", 0x4641544F, fato_size, entry_count, 0xFFFF)
    for offset in fato_offsets:
        out += struct.pack("<I", offset)

    out += struct.pack("<III", 0x46415442, fatb_size, len(recs))
    for vector, rows in table_rows:
        out += struct.pack("<I", vector)
        for start, end, length in rows:
            out += struct.pack("<III", start, end, length)

    out += struct.pack("<III", 0x46494D42, 0x0C, data_cursor)
    data_start = len(out)
    file_size = data_start + data_cursor
    struct.pack_into("<I", out, 0x10, data_start)
    struct.pack_into("<I", out, 0x14, file_size)
    if version == 0x0400:
        struct.pack_into("<I", out, 0x18, largest_unpadded)
    else:
        struct.pack_into("<III", out, 0x18, largest_padded, largest_unpadded, 4)
    return bytes(out)


def verify_canonical_target(buf, info: GARCInfo) -> list[dict]:
    """
    Reject targets whose structure would require embedding target/source prefix
    bytes.
    """
    records: list[dict] = []
    cursor = 0
    for rec in info.records:
        if rec.start != cursor:
            raise ValueError(
                "target GARC has non-canonical data gaps; refusing to embed"
                " those bytes in the patch"
            )
        padded_length = rec.end - rec.start
        padding_start = rec.abs_start + rec.length
        padding = bytes(buf[padding_start : rec.abs_end])
        if padding != b"\xff" * len(padding):
            raise ValueError(
                f"target GARC member {rec.key} has non-FF padding; refusing"
                " to embed it"
            )
        records.append(
            {
                "key": rec.key,
                "entry_index": rec.entry_index,
                "sub_index": rec.sub_index,
                "target_length": rec.length,
                "target_padded_length": padded_length,
            }
        )
        cursor = rec.end

    prefix = build_canonical_garc_prefix(
        info.version, info.entry_count, records
    )
    if prefix != bytes(buf[: info.data_start]):
        raise ValueError(
            "target GARC header/tables are not the canonical pk3DS-style"
            " form; refusing to store a direct prefix copy"
        )
    if info.data_start + cursor != len(buf):
        raise ValueError(
            "target GARC has trailing data outside canonical members"
        )
    return records


def _slice_equal(a, a0: int, b, b0: int, length: int) -> bool:
    pos = 0
    while pos < length:
        n = min(4 * 1024 * 1024, length - pos)
        if a[a0 + pos : a0 + pos + n] != b[b0 + pos : b0 + pos + n]:
            return False
        pos += n
    return True


def _write_stored_blob(z: zipfile.ZipFile, name: str, data: bytes) -> str:
    z.writestr(name, data, compress_type=zipfile.ZIP_STORED)
    return name


def create_garc_patch(
    source_path: Path, target_path: Path, out_patch: Path, relpath: str
) -> dict:
    source_path = Path(source_path)
    target_path = Path(target_path)
    out_patch.parent.mkdir(parents=True, exist_ok=True)
    temp = out_patch.with_suffix(out_patch.suffix + f".tmp.{os.getpid()}")
    if temp.exists():
        temp.unlink()

    print("  Hashing source GARC...")
    hp = ConsoleProgress(source_path.stat().st_size, "source SHA-256")
    source_sha = sha256_file(
        source_path, lambda d, _t: hp.update(d), 0, source_path.stat().st_size
    )
    print("  Hashing target GARC...")
    hp = ConsoleProgress(target_path.stat().st_size, "target SHA-256")
    target_sha = sha256_file(
        target_path, lambda d, _t: hp.update(d), 0, target_path.stat().st_size
    )

    stats = {
        "copy_packed": 0,
        "same_raw": 0,
        "xor_raw": 0,
        "raw_delta": 0,
        "literal_packed": 0,
        "xor_blob_bytes": 0,
        "recipe_blob_bytes": 0,
        "delta_blob_bytes": 0,
        "literal_blob_bytes": 0,
        "changed_raw_bytes": 0,
    }

    fs = ft = None
    ms = mt = None
    try:
        fs = source_path.open("rb")
        ft = target_path.open("rb")
        ms = mmap.mmap(fs.fileno(), 0, access=mmap.ACCESS_READ)
        mt = mmap.mmap(ft.fileno(), 0, access=mmap.ACCESS_READ)
        source_info = parse_garc(ms)
        target_info = parse_garc(mt)
        target_records = verify_canonical_target(mt, target_info)
        source_by_key = {r.key: r for r in source_info.records}
        target_by_key = {r.key: r for r in target_info.records}

        manifest: dict = {
            "format": GARC_PATCH_FORMAT,
            "relpath": relpath,
            "source_size": len(ms),
            "source_sha256": source_sha,
            "target_size": len(mt),
            "target_sha256": target_sha,
            "garc_version": target_info.version,
            "entry_count": target_info.entry_count,
            "records": [],
        }

        member_progress = ConsoleProgress(
            max(1, len(target_records)), "GARC members"
        )
        with zipfile.ZipFile(
            temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as z:
            for index, base_rec in enumerate(target_records):
                tr = target_by_key[base_rec["key"]]
                rec = dict(base_rec)
                sr = source_by_key.get(tr.key)
                detail = tr.key

                if (
                    sr is not None
                    and sr.length == tr.length
                    and _slice_equal(
                        ms, sr.abs_start, mt, tr.abs_start, tr.length
                    )
                ):
                    rec.update(
                        {
                            "mode": "copy_packed",
                            "source_offset": sr.abs_start,
                            "source_length": sr.length,
                        }
                    )
                    stats["copy_packed"] += 1
                    detail += " packed-copy"
                elif sr is not None:
                    source_packed = bytes(
                        ms[sr.abs_start : sr.abs_start + sr.length]
                    )
                    target_packed = bytes(
                        mt[tr.abs_start : tr.abs_start + tr.length]
                    )
                    s_dec = try_lz11(source_packed)
                    t_dec = try_lz11(target_packed)
                    source_raw = (
                        s_dec[0] if s_dec is not None else source_packed
                    )
                    target_raw = (
                        t_dec[0] if t_dec is not None else target_packed
                    )
                    source_encoding = "lz11" if s_dec is not None else "raw"
                    target_encoding = "lz11" if t_dec is not None else "raw"

                    rec.update(
                        {
                            "mode": "rebuild_raw",
                            "source_offset": sr.abs_start,
                            "source_length": sr.length,
                            "source_encoding": source_encoding,
                            "target_encoding": target_encoding,
                            "raw_size": len(target_raw),
                        }
                    )

                    if source_raw == target_raw:
                        rec["raw_mode"] = "same"
                        stats["same_raw"] += 1
                        detail += " same-raw"
                    elif len(source_raw) == len(target_raw):
                        xor_blob = build_xor_zlib(source_raw, target_raw)
                        name = f"members/{index:06d}.xor.z"
                        rec["raw_mode"] = "xor_zlib"
                        rec["raw_blob"] = _write_stored_blob(z, name, xor_blob)
                        stats["xor_raw"] += 1
                        stats["xor_blob_bytes"] += len(xor_blob)
                        stats["changed_raw_bytes"] += len(target_raw)
                        detail += f" xor={len(xor_blob) / (1024 * 1024):.2f}MiB"
                    else:
                        delta_blob = build_raw_delta(source_raw, target_raw)
                        name = f"members/{index:06d}.delta.z"
                        rec["raw_mode"] = "delta"
                        rec["raw_blob"] = _write_stored_blob(
                            z, name, delta_blob
                        )
                        stats["raw_delta"] += 1
                        stats["delta_blob_bytes"] += len(delta_blob)
                        stats["changed_raw_bytes"] += len(target_raw)
                        detail += (
                            f" delta={len(delta_blob) / (1024 * 1024):.2f}MiB"
                        )

                    if target_encoding == "lz11":
                        recipe_blob = make_lz11_recipe(target_packed)
                        rec["recipe_blob"] = _write_stored_blob(
                            z, f"members/{index:06d}.recipe.z", recipe_blob
                        )
                        stats["recipe_blob_bytes"] += len(recipe_blob)
                    stats["literal_blob_bytes"] += 0
                else:

                    target_packed = bytes(
                        mt[tr.abs_start : tr.abs_start + tr.length]
                    )
                    rec.update(
                        {
                            "mode": "literal_packed",
                            "blob": _write_stored_blob(
                                z, f"members/{index:06d}.new", target_packed
                            ),
                        }
                    )
                    stats["literal_packed"] += 1
                    stats["literal_blob_bytes"] += len(target_packed)
                    detail += " NEW-LITERAL"

                manifest["records"].append(rec)
                member_progress.update(index + 1, detail=detail, force=True)

            manifest["stats"] = stats
            z.writestr(
                "manifest.json",
                json.dumps(
                    manifest, separators=(",", ":"), sort_keys=True
                ).encode("utf-8"),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )

        os.replace(temp, out_patch)
        stats["patch_bytes"] = out_patch.stat().st_size
        return stats
    finally:
        for mm in (ms, mt):
            if mm is not None:
                try:
                    mm.close()
                except Exception:
                    pass
        for f in (fs, ft):
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
        try:
            if temp.exists():
                temp.unlink()
        except Exception:
            pass


def read_garc_patch_manifest(patch_path: Path) -> dict:
    with zipfile.ZipFile(patch_path, "r") as z:
        data = json.loads(z.read("manifest.json").decode("utf-8"))
    if data.get("format") != GARC_PATCH_FORMAT:
        raise ValueError(
            "unsupported GARC patch format; regenerate the patch with the"
            " matching v2 maker"
        )
    return data


def is_valid_garc(path: Path, require_canonical_target: bool = False) -> bool:
    """
    Return True only when the file is supported by the exact GARC format.
    """
    handle = None
    mapped = None
    try:
        handle = path.open("rb")
        mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        info = parse_garc(mapped)
        if require_canonical_target:
            verify_canonical_target(mapped, info)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if mapped is not None:
            mapped.close()
        if handle is not None:
            handle.close()


def should_use_garc_patch(source_path: Path, target_path: Path) -> bool:
    """Select exact GARC by original size, never by added target bytes."""
    if source_path.stat().st_size <= GARC_PATCH_MIN_SOURCE_SIZE:
        return False
    return is_valid_garc(source_path) and is_valid_garc(
        target_path, require_canonical_target=True
    )
