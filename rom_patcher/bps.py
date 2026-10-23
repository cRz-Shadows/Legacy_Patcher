"""BPS generation, parsing, and verified application."""

from __future__ import annotations

import hashlib
import mmap
import os
import shutil
import stat
import struct
import subprocess
import sys
import zlib
from pathlib import Path

from .config import BPS_LITERAL_CHUNK, IO_CHUNK
from .delta import iter_source_runs
from .files import crc32_file


def apply_ips_to_file(ips_path: Path, target_path: Path) -> None:
    """Apply IPS records to a temporary copy before replacing the target."""
    if not ips_path.is_file():
        raise FileNotFoundError(f"IPS not found: {ips_path}")
    if not target_path.is_file():
        raise FileNotFoundError(f"Target file for IPS not found: {target_path}")

    data = ips_path.read_bytes()
    if len(data) < 8 or data[:5] != b"PATCH":
        raise ValueError(f"Invalid IPS header: {ips_path.name}")

    tmp = target_path.with_suffix(target_path.suffix + f".tmp.{os.getpid()}")
    try:

        with target_path.open("rb") as fin, tmp.open("wb") as fout:
            while True:
                b = fin.read(IO_CHUNK)
                if not b:
                    break
                fout.write(b)

        with tmp.open("r+b") as f:
            i = 5
            while True:
                if i + 3 > len(data):
                    raise ValueError("IPS truncated before EOF marker")
                if data[i : i + 3] == b"EOF":
                    i += 3
                    break

                offset = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
                i += 3
                if i + 2 > len(data):
                    raise ValueError("IPS truncated at size field")

                size = (data[i] << 8) | data[i + 1]
                i += 2

                f.seek(offset)

                if size == 0:

                    if i + 3 > len(data):
                        raise ValueError("IPS truncated at RLE record")
                    rle_size = (data[i] << 8) | data[i + 1]
                    val = data[i + 2]
                    i += 3
                    if rle_size:
                        f.write(bytes([val]) * rle_size)
                else:
                    if i + size > len(data):
                        raise ValueError("IPS truncated at data record")
                    f.write(data[i : i + size])
                    i += size

        os.replace(tmp, target_path)

    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def bps_write_vli(n: int) -> bytes:
    if n < 0:
        raise ValueError("VLI cannot encode negative numbers")
    out = bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        if n == 0:
            out.append(0x80 | x)
            break
        out.append(x)
        n -= 1
    return bytes(out)


def bps_cmd(action: int, length: int) -> bytes:
    if length <= 0:
        raise ValueError("BPS command length must be > 0")
    if action not in (0, 1, 2, 3):
        raise ValueError("Invalid BPS action")
    return bps_write_vli(((length - 1) << 2) | action)


def bps_write_signed(n: int) -> bytes:
    mag = -n if n < 0 else n
    v = (mag << 1) | (1 if n < 0 else 0)
    return bps_write_vli(v)


class CRCWriter:
    def __init__(self, f):
        self.f = f
        self.crc = 0

    def write(self, data: bytes) -> None:
        self.f.write(data)
        self.crc = zlib.crc32(data, self.crc) & 0xFFFFFFFF


def create_bps_patch(
    source_file: Path, target_file: Path, out_bps: Path, relpath_for_meta: str
) -> None:
    """
    Create BPS patch transforming source_file -> target_file, writing to
    out_bps. Metadata contains just relpath (no manifest file).
    """
    source_size = source_file.stat().st_size
    target_size = target_file.stat().st_size

    source_crc = crc32_file(source_file)
    target_crc = crc32_file(target_file)

    metadata = f"relpath={relpath_for_meta}\n".encode("utf-8")

    tmp = out_bps.with_suffix(out_bps.suffix + f".tmp.{os.getpid()}")
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass

    fs = ft = fo = None
    source_map = target_map = None
    source_view = target_view = None

    try:
        out_bps.parent.mkdir(parents=True, exist_ok=True)
        fs = open(source_file, "rb")
        ft = open(target_file, "rb")
        fo = open(tmp, "wb")

        if source_size:
            source_map = mmap.mmap(fs.fileno(), 0, access=mmap.ACCESS_READ)
        if target_size:
            target_map = mmap.mmap(ft.fileno(), 0, access=mmap.ACCESS_READ)
        source_view = memoryview(source_map if source_size else b"")
        target_view = memoryview(target_map if target_size else b"")

        w = CRCWriter(fo)

        w.write(b"BPS1")
        w.write(bps_write_vli(source_size))
        w.write(bps_write_vli(target_size))
        w.write(bps_write_vli(len(metadata)))
        w.write(metadata)

        source_rel = 0

        def write_source_read(n: int) -> None:
            w.write(bps_cmd(0, n))

        def write_target_read(start: int, end: int) -> None:
            for chunk in range(start, end, BPS_LITERAL_CHUNK):
                buf = bytes(
                    target_view[chunk : min(end, chunk + BPS_LITERAL_CHUNK)]
                )
                w.write(bps_cmd(1, len(buf)))
                w.write(buf)

        def write_source_copy(src_pos: int, n: int) -> None:
            nonlocal source_rel
            w.write(bps_cmd(2, n))
            delta = src_pos - source_rel
            w.write(bps_write_signed(delta))
            source_rel = src_pos + n

        written = 0
        for t_pos, s_pos, length in iter_source_runs(source_view, target_view):
            write_target_read(written, t_pos)
            if s_pos == t_pos:
                write_source_read(length)
            else:
                write_source_copy(s_pos, length)
            written = t_pos + length
        write_target_read(written, target_size)

        w.write(struct.pack("<I", source_crc))
        w.write(struct.pack("<I", target_crc))

        patch_crc = w.crc
        fo.write(struct.pack("<I", patch_crc))

        fo.flush()
        os.fsync(fo.fileno())
        fo.close()
        fo = None

        os.replace(tmp, out_bps)

    finally:
        try:
            if source_view is not None:
                source_view.release()
        except Exception:
            pass
        try:
            if target_view is not None:
                target_view.release()
        except Exception:
            pass
        try:
            if source_map is not None:
                source_map.close()
        except Exception:
            pass
        try:
            if target_map is not None:
                target_map.close()
        except Exception:
            pass
        try:
            if fs is not None:
                fs.close()
        except Exception:
            pass
        try:
            if ft is not None:
                ft.close()
        except Exception:
            pass
        try:
            if fo is not None:
                fo.close()
        except Exception:
            pass
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def crc32_file_progress(path, progress_cb, done_base, total):
    c = 0
    done_local = 0
    size = path.stat().st_size
    with open(str(path), "rb") as f:
        while True:
            b = f.read(IO_CHUNK)
            if not b:
                break
            c = zlib.crc32(b, c) & 4294967295
            done_local += len(b)
            if progress_cb:
                progress_cb(done_base + done_local, total)
    if size == 0 and progress_cb:
        progress_cb(done_base, total)
    return c


def bps_read_vli_mem(buf, cur_ref):
    cur = cur_ref[0]
    data = 0
    shift = 1
    while True:
        if cur >= len(buf):
            raise IOError("Unexpected EOF reading VLI")
        x = buf[cur]
        cur += 1
        data += (x & 127) * shift
        if x & 128:
            break
        shift <<= 7
        data += shift
    cur_ref[0] = cur
    return data


def _repair_patch_file_access(path: Path):
    """
    Best-effort repair for restrictive modes, ACLs, and flags from archives.
    """
    path = Path(path)
    if sys.platform == "darwin":
        commands = (
            ("chflags", "nouchg"),
            ("chmod", "-N"),
        )
        for tool, option in commands:
            executable = shutil.which(tool)
            if executable is None:
                continue
            try:
                subprocess.run(
                    [executable, option, str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                pass
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IRUSR | stat.S_IWUSR)


def _open_patch_file(path: Path):
    path = Path(path)
    try:
        return path.open("rb")
    except PermissionError:
        _repair_patch_file_access(path)
        return path.open("rb")


def _read_bps_metadata_file(patch_path: Path) -> dict:
    """
    Read the small BPS header/footer. The complete patch CRC is checked on
    apply.
    """
    patch_path = Path(patch_path)
    with _open_patch_file(patch_path) as patch_file:
        patch_file.seek(0, os.SEEK_END)
        patch_size = patch_file.tell()
        if patch_size < 16:
            raise ValueError("BPS patch is too small: %s" % patch_path)
        patch_file.seek(0)
        header = patch_file.read(min(64, patch_size))
        if len(header) < 7 or header[:4] != b"BPS1":
            raise ValueError("Not a BPS1 patch: %s" % patch_path)
        cur = [4]
        source_size = bps_read_vli_mem(header, cur)
        target_size = bps_read_vli_mem(header, cur)
        bps_read_vli_mem(header, cur)
        patch_file.seek(-12, os.SEEK_END)
        footer = patch_file.read(12)
        if len(footer) != 12:
            raise ValueError("BPS patch footer is truncated: %s" % patch_path)
        source_crc, target_crc, patch_crc = struct.unpack("<III", footer)
    return {
        "source_size": source_size,
        "target_size": target_size,
        "source_crc": source_crc,
        "target_crc": target_crc,
        "patch_crc": patch_crc,
        "patch_size": patch_size,
    }


def _sha256_crc32_file_progress(
    path: Path, progress_cb, done_base: int, total: int
) -> tuple[str, int]:
    sha = hashlib.sha256()
    crc = 0
    done = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(IO_CHUNK)
            if not chunk:
                break
            sha.update(chunk)
            crc = zlib.crc32(chunk, crc) & 4294967295
            done += len(chunk)
            if progress_cb:
                progress_cb(done_base + done, total)
    return (sha.hexdigest(), crc)


def bps_read_signed_mem(buf, cur_ref):
    v = bps_read_vli_mem(buf, cur_ref)
    mag = v >> 1
    neg = v & 1
    return -mag if neg else mag


def apply_bps_patch_inplace(
    bps_bytes,
    file_path,
    progress_cb=None,
    progress_base=0,
    progress_total=1,
    verify_source_crc=True,
    expected_metadata=None,
):
    mv = memoryview(bps_bytes)
    try:
        if len(mv) < 4 + 12 or mv[:4].tobytes() != b"BPS1":
            raise ValueError("Not a BPS1 patch")
        patch_crc_expected = struct.unpack_from("<I", mv, len(mv) - 4)[0]
        calc_patch_crc = zlib.crc32(mv[: len(mv) - 4]) & 4294967295
        if calc_patch_crc != patch_crc_expected:
            raise RuntimeError("Patch CRC32 mismatch (corrupt patch file)")
        cur = [4]
        source_size = bps_read_vli_mem(mv, cur)
        target_size = bps_read_vli_mem(mv, cur)
        meta_size = bps_read_vli_mem(mv, cur)
        cur[0] += meta_size
        footer_pos = len(mv) - 12
        if cur[0] > footer_pos:
            raise RuntimeError("Patch metadata extends beyond BPS payload")
        src_crc_expected = struct.unpack_from("<I", mv, len(mv) - 12)[0]
        tgt_crc_expected = struct.unpack_from("<I", mv, len(mv) - 8)[0]
        if expected_metadata is not None:
            checks = (
                (
                    source_size,
                    int(expected_metadata["source_size"]),
                    "source size",
                ),
                (
                    target_size,
                    int(expected_metadata["target_size"]),
                    "target size",
                ),
                (
                    src_crc_expected,
                    int(expected_metadata["source_crc"]),
                    "source CRC",
                ),
                (
                    tgt_crc_expected,
                    int(expected_metadata["target_crc"]),
                    "target CRC",
                ),
                (
                    patch_crc_expected,
                    int(expected_metadata["patch_crc"]),
                    "patch CRC",
                ),
            )
            for actual, expected, label in checks:
                if actual != expected:
                    raise RuntimeError(
                        "BPS %s changed after patch planning" % label
                    )
        if file_path.stat().st_size != source_size:
            raise RuntimeError(
                "Source size mismatch for file: %s" % file_path.as_posix()
            )
        if verify_source_crc:
            c = 0
            with open(str(file_path), "rb") as source_check:
                while True:
                    b = source_check.read(IO_CHUNK)
                    if not b:
                        break
                    c = zlib.crc32(b, c) & 4294967295
            if c != src_crc_expected:
                raise RuntimeError(
                    "Source CRC mismatch for file: %s" % file_path.as_posix()
                )
        tmp = file_path.with_suffix(file_path.suffix + ".tmp.%d" % os.getpid())
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        source_rel = 0
        target_rel = 0
        out_pos = 0
        target_crc = 0

        def report():
            if progress_cb:
                progress_cb(progress_base + out_pos, progress_total)

        def write_target(output, data):
            nonlocal out_pos, target_crc
            output.write(data)
            target_crc = zlib.crc32(data, target_crc) & 4294967295
            out_pos += len(data)
            report()

        try:
            with (
                open(str(file_path), "rb") as fs,
                open(str(tmp), "wb+") as fo,
            ):
                while cur[0] < footer_pos and out_pos < target_size:
                    header = bps_read_vli_mem(mv, cur)
                    action = header & 3
                    length = (header >> 2) + 1
                    if out_pos + length > target_size:
                        raise IOError("BPS action exceeds declared target size")
                    if action == 0:
                        fs.seek(out_pos)
                        remaining = length
                        while remaining:
                            chunk = fs.read(min(IO_CHUNK, remaining))
                            if not chunk:
                                raise IOError("Unexpected EOF in SourceRead")
                            write_target(fo, chunk)
                            remaining -= len(chunk)
                    elif action == 1:
                        end = cur[0] + length
                        if end > footer_pos:
                            raise IOError("Patch truncated in TargetRead")
                        write_target(fo, mv[cur[0] : end])
                        cur[0] = end
                    elif action == 2:
                        source_rel += bps_read_signed_mem(mv, cur)
                        fs.seek(source_rel)
                        remaining = length
                        while remaining:
                            chunk = fs.read(min(IO_CHUNK, remaining))
                            if not chunk:
                                raise IOError("Unexpected EOF in SourceCopy")
                            write_target(fo, chunk)
                            remaining -= len(chunk)
                        source_rel += length
                    elif action == 3:
                        target_rel += bps_read_signed_mem(mv, cur)
                        copy_from = target_rel
                        remaining = length
                        while remaining:
                            fo.flush()
                            fo.seek(copy_from)
                            chunk = fo.read(min(IO_CHUNK, remaining))
                            if not chunk:
                                raise IOError(
                                    "Unexpected EOF in TargetCopy readback"
                                )
                            fo.seek(0, os.SEEK_END)
                            write_target(fo, chunk)
                            remaining -= len(chunk)
                            copy_from += len(chunk)
                        target_rel += length
                    else:
                        raise RuntimeError("Invalid BPS action")
                if out_pos != target_size:
                    raise RuntimeError(
                        "BPS patch produced the wrong target size"
                    )
                if cur[0] != footer_pos:
                    raise RuntimeError(
                        "BPS patch contains unused or truncated action data"
                    )
                fo.flush()
                os.fsync(fo.fileno())
            if target_crc != tgt_crc_expected:
                raise RuntimeError(
                    "Target CRC mismatch after patch for file: %s"
                    % file_path.as_posix()
                )
            os.replace(str(tmp), str(file_path))
            return target_size
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
    finally:
        mv.release()
