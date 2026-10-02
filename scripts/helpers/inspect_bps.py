"""Show command counts and literal sizes in a BPS patch."""

from __future__ import annotations

import argparse
import mmap
import struct
import sys
from pathlib import Path
from tkinter import filedialog

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rom_patcher.bps import bps_read_signed_mem, bps_read_vli_mem
from rom_patcher.inputs import choose_all_locations, selection_folder


def inspect_patch(path: Path) -> None:
    with path.open("rb") as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as payload:
            if len(payload) < 16 or payload[:4] != b"BPS1":
                raise ValueError("Select a BPS1 patch.")
            cursor = [4]
            source_size = bps_read_vli_mem(payload, cursor)
            target_size = bps_read_vli_mem(payload, cursor)
            metadata_size = bps_read_vli_mem(payload, cursor)
            cursor[0] += metadata_size
            commands = [0] * 4
            sizes = [0] * 4
            produced = 0
            while produced < target_size:
                instruction = bps_read_vli_mem(payload, cursor)
                action = instruction & 3
                length = (instruction >> 2) + 1
                commands[action] += 1
                sizes[action] += length
                if action == 1:
                    cursor[0] += length
                elif action in (2, 3):
                    bps_read_signed_mem(payload, cursor)
                if (
                    cursor[0] > len(payload) - 12
                    or produced + length > target_size
                ):
                    raise ValueError("The patch has an invalid command range.")
                produced += length
            if cursor[0] != len(payload) - 12:
                raise ValueError("The patch has trailing command data.")
            source_crc, target_crc, patch_crc = struct.unpack_from(
                "<III", payload, len(payload) - 12
            )
    print(f"File: {path.name}")
    print(f"Source bytes: {source_size}")
    print(f"Target bytes: {target_size}")
    labels = ("SourceRead", "TargetRead", "SourceCopy", "TargetCopy")
    for label, count, size in zip(labels, commands, sizes):
        print(f"{label}: {count} commands, {size} bytes")
    print(f"Source CRC32: {source_crc:08x}")
    print(f"Target CRC32: {target_crc:08x}")
    print(f"Patch CRC32: {patch_crc:08x}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("patch", nargs="?", type=Path)
    arguments = parser.parse_args()
    path = arguments.patch
    if path is None:
        folder = selection_folder()
        candidates = (
            [] if choose_all_locations() else sorted(folder.glob("*.bps"))
        )
        if len(candidates) == 1:
            path = candidates[0]
        else:
            selected = filedialog.askopenfilename(
                title="Select a BPS patch",
                initialdir=str(folder),
                filetypes=(("BPS patch", "*.bps"),),
            )
            if not selected:
                return 0
            path = Path(selected)
    try:
        inspect_patch(path)
    except (OSError, ValueError, IndexError, struct.error) as error:
        print(f"Patch inspection failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
