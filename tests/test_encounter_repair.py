"""Check encounter-location repair and preservation of unrelated data."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from rom_patcher import encounter_repair
from rom_patcher.text_repair import GarcArchive, GarcSubEntry, pack_garc


MAP_PATH = Path("romfs/a/0/1/2")
DEX_PATH = Path("romfs/a/2/0/2")


def write_garc(root, relative_path, entries):
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pack_garc(GarcArchive(0x0400, entries)))


def write_encounters(root, slots):
    empty_zone = struct.pack("<2sH5I", b"ZO", 4, 24, 24, 24, 24, 24)
    entries = [[GarcSubEntry(0, empty_zone)] for _ in range(360)]
    master = bytearray(360 * 56)
    for index, group in enumerate(encounter_repair.TOWN_MAP_GROUPS):
        struct.pack_into("<H", master, index * 56 + 26, group)
    encounters = bytearray(16 + 94 * 4)
    for slot, species in slots.items():
        struct.pack_into("<HBB", encounters, 16 + slot * 4, species, 5, 10)
    # Zone 359 belongs to group zero, which uses distribution column two.
    zone = struct.pack(
        "<2sH5I", b"ZO", 4, 24, 24, 24, 24, 24 + len(encounters)
    )
    entries[359] = [GarcSubEntry(0, zone + encounters)]
    entries.append([GarcSubEntry(0, bytes(master))])
    write_garc(root, MAP_PATH, entries)


class EncounterRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / "base"
        self.updated = Path(temporary.name) / "updated"
        write_encounters(self.base, {0: 25})
        write_encounters(self.updated, {0: 1 | (3 << 11), 60: 1})
        table = bytearray(722 * 58 * 4)
        for species, column, value in (
            (25, 2, 0x0801), (1, 2, 0x1000),
            (0, 2, 0x12345678), (1, 0, 0x55), (1, 1, 0x66),
        ):
            struct.pack_into("<I", table, (species * 58 + column) * 4, value)
        write_garc(self.base, DEX_PATH, [
            [GarcSubEntry(0, bytes(table))], [GarcSubEntry(0, b"keep")],
        ])

    def test_repair_updates_locations_and_preserves_other_data(self):
        inputs = [
            self.base / MAP_PATH,
            self.base / DEX_PATH,
            self.updated / MAP_PATH,
        ]
        originals = [path.read_bytes() for path in inputs]
        changed = encounter_repair.repair_encounter_locations(
            self.updated, self.base
        )
        self.assertEqual(changed, 2)
        archive = (self.updated / DEX_PATH).read_bytes()
        table, _compressed = encounter_repair.read_distribution(archive)
        for species, column, expected in (
            (1, 2, 0x1021), (25, 2, 0x0800),
            (0, 2, 0x12345678), (1, 0, 0x55), (1, 1, 0x66),
        ):
            offset = (species * 58 + column) * 4
            self.assertEqual(
                struct.unpack_from("<I", table, offset)[0], expected
            )
        self.assertEqual(archive[-4:], b"keep")
        self.assertEqual([path.read_bytes() for path in inputs], originals)

    def test_repeated_repair_keeps_identical_output(self):
        encounter_repair.repair_encounter_locations(self.updated, self.base)
        before = (self.updated / DEX_PATH).read_bytes()
        self.assertEqual(
            encounter_repair.repair_encounter_locations(
                self.updated, self.base
            ),
            0,
        )
        self.assertEqual((self.updated / DEX_PATH).read_bytes(), before)

    def test_invalid_encounters_stop_before_writing(self):
        (self.updated / MAP_PATH).write_bytes(b"invalid GARC")
        with self.assertRaises(ValueError):
            encounter_repair.repair_encounter_locations(
                self.updated, self.base
            )
        self.assertFalse((self.updated / DEX_PATH).exists())


if __name__ == "__main__":
    unittest.main()
