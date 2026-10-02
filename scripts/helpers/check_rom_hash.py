"""Print ROM checksums or compare a ROM with an expected SHA-256 digest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rom_patcher.files import crc32_file, file_hash
from rom_patcher.inputs import SelectionCancelled, select_rom, selection_folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom", type=Path)
    parser.add_argument("--expected", metavar="SHA256")
    arguments = parser.parse_args()
    if arguments.expected:
        try:
            expected = bytes.fromhex(arguments.expected)
        except ValueError:
            parser.error("--expected must contain 64 hexadecimal characters")
        if len(expected) != 32:
            parser.error("--expected must contain 64 hexadecimal characters")
    try:
        rom = arguments.rom or select_rom(selection_folder())
        digest = file_hash(rom, "sha256")
        print(f"File: {rom.name}")
        print(f"Bytes: {rom.stat().st_size}")
        print(f"SHA-256: {digest}")
        print(f"SHA-1: {file_hash(rom, 'sha1')}")
        print(f"CRC32: {crc32_file(rom):08x}")
        if arguments.expected:
            if digest != arguments.expected.casefold():
                print("The SHA-256 does not match the expected digest.")
                return 1
            print("The SHA-256 matches the expected digest.")
        return 0
    except SelectionCancelled:
        return 0
    except OSError as error:
        print(f"Checksum failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
