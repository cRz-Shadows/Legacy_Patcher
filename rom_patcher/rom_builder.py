"""Verified CCI staging and original CardInfo restoration."""

from __future__ import annotations

import os
from pathlib import Path

from .config import CCI_HEADER_READ_SIZE, ORIGINAL_CARDINFO_RANGES
from .romfs import build_deterministic_romfs
from .rsf import write_romfs_rsf
from .runtime import create_hidden_output_staging_dir, run_cmd, safe_rmtree


def capture_original_cci_card_info(source_rom):
    """Capture immutable seed-linked CardInfo fields before any build work."""
    source_rom = Path(source_rom)
    with source_rom.open("rb") as source:
        source_header = source.read(CCI_HEADER_READ_SIZE)
    if (
        len(source_header) < CCI_HEADER_READ_SIZE
        or source_header[256:260] != b"NCSD"
    ):
        raise RuntimeError("Input ROM is not a valid CCI/3DS image")
    return tuple(
        (
            (offset, source_header[offset:end])
            for offset, end in ORIGINAL_CARDINFO_RANGES
        )
    )


def verify_original_cci_card_info(original_fields, output_rom):
    output_rom = Path(output_rom)
    with output_rom.open("rb") as output:
        output_header = output.read(CCI_HEADER_READ_SIZE)
    if (
        len(output_header) < CCI_HEADER_READ_SIZE
        or output_header[256:260] != b"NCSD"
    ):
        raise RuntimeError("makerom output is not a valid CCI/3DS image")
    for offset, expected in original_fields:
        actual = output_header[offset : offset + len(expected)]
        if actual != expected:
            raise RuntimeError(
                "Final ROM header data differs from the input at offset 0x%X"
                % offset
            )


def apply_original_cci_card_info(original_fields, output_rom, durable=True):
    """Write captured CardInfo fields and verify them on disk."""
    output_rom = Path(output_rom)
    with output_rom.open("r+b", buffering=0) as output:
        output_header = output.read(CCI_HEADER_READ_SIZE)
        if (
            len(output_header) < CCI_HEADER_READ_SIZE
            or output_header[256:260] != b"NCSD"
        ):
            raise RuntimeError("makerom output is not a valid CCI/3DS image")
        for offset, data in original_fields:
            output.seek(offset)
            output.write(data)
        output.flush()
        if durable:
            os.fsync(output.fileno())
    verify_original_cci_card_info(original_fields, output_rom)


def run_makerom(
    makerom,
    workdir,
    rsf,
    out_rom,
    original_card_info,
    validation_state=None,
    validation_progress_cb=None,
):
    exheader = workdir / "exheader.bin"
    code = workdir / "exefs" / "code.bin"
    icon = workdir / "exefs" / "icon.bin"
    banner = workdir / "exefs" / "banner.bin"
    romfs_dir = workdir / "romfs"
    for required_file in (exheader, code, rsf):
        if not required_file.is_file():
            raise RuntimeError(
                "Missing required build input: %s" % required_file
            )
    out_rom = Path(out_rom)
    if out_rom.exists():
        raise RuntimeError(
            "Refusing to overwrite an existing output ROM: %s" % out_rom
        )
    out_rom.parent.mkdir(parents=True, exist_ok=True)
    build_rsf = workdir / "_makerom_build.rsf"
    deterministic_romfs = workdir / "_deterministic_romfs.bin"
    has_romfs = romfs_dir.is_dir()
    write_romfs_rsf(rsf, build_rsf, include_romfs=has_romfs)
    deferred_validation = (
        validation_state.get("deferred_romfs", {})
        if validation_state is not None
        else {}
    )
    if deferred_validation and not has_romfs:
        raise RuntimeError("Missing required build input: %s" % romfs_dir)
    if has_romfs:
        build_deterministic_romfs(
            romfs_dir,
            deterministic_romfs,
            deferred_validation=deferred_validation,
            validation_progress_cb=validation_progress_cb,
        )
    staging_dir = create_hidden_output_staging_dir(out_rom.parent)
    staged_output = staging_dir / "_verified_makerom_output.3ds"
    command = [
        str(makerom),
        "-f",
        "cci",
        "-o",
        str(staged_output),
        "-v",
        "-target",
        "p",
        "-ignoresign",
        "-devcci",
        "-rsf",
        str(build_rsf),
        "-exheader",
        str(exheader),
        "-code",
        str(code),
    ]
    if has_romfs:
        command.extend(("-romfs", str(deterministic_romfs)))
    for option, path in (
        ("-icon", icon),
        ("-banner", banner),
        ("-logo", workdir / "logo.bin"),
        ("-plainrgn", workdir / "plain.bin"),
    ):
        if path.is_file():
            command.extend((option, str(path)))
    try:
        run_cmd(command, cwd=workdir)
        try:
            deterministic_romfs.unlink()
        except OSError:
            pass
        apply_original_cci_card_info(
            original_card_info, staged_output, durable=False
        )
        with staged_output.open("r+b", buffering=0) as output:
            output.flush()
            os.fsync(output.fileno())
        if out_rom.exists():
            raise RuntimeError(
                "Refusing to overwrite an existing output ROM: %s" % out_rom
            )
        os.replace(staged_output, out_rom)
        verify_original_cci_card_info(original_card_info, out_rom)
    finally:
        safe_rmtree(staging_dir)
