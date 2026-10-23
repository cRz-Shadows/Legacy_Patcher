"""Repair Pokémon X/Y Pokédex locations from staged wild encounters."""

from __future__ import annotations

import struct
from pathlib import Path

from .garc import parse_garc
from .lz11 import try_lz11
from .text_repair import GarcArchive, GarcSubEntry, pack_garc


ENCOUNTER_RELPATH = Path("romfs/a/0/1/2")
DISTRIBUTION_RELPATH = Path("romfs/a/2/0/2")
ZONE_COUNT = 360
ZONE_RECORD_SIZE = 0x38
TOWN_MAP_GROUP_OFFSET = 0x1A
SPECIES_COUNT = 722
DISTRIBUTION_COLUMNS = 58
METHOD_MASK = 0x07FF
SPECIES_ID_MASK = 0x07FF

METHOD_RANGES = (
    (0, 12, 0x0001),
    (12, 24, 0x0002),
    (24, 36, 0x0004),
    (36, 48, 0x0008),
    (48, 60, 0x0010),
    (60, 65, 0x0020),
    (65, 70, 0x0040),
    (70, 73, 0x0080),
    (73, 76, 0x0100),
    (76, 79, 0x0200),
    (79, 94, 0x0400),
)

# DllZukan uses columns 2..57 in this order, with Azure Bay before Route 22.
TOWN_MAP_GROUPS = (
    0, 8, 12, 21, 29, 38, 45, 53, 63, 65, 67, 157,
    172, 188, 200, 211, 222, 233, 243, 258, 259, 260, 261, 262,
    263, 264, 266, 267, 268, 269, 270, 272, 273, 275, 276, 278,
    279, 281, 282, 283, 285, 357, 286, 287, 303, 305, 309, 313,
    318, 320, 334, 335, 343, 349, 350, 351,
)


def _decode_member(packed: bytes) -> tuple[bytes, bool]:
    decoded = try_lz11(packed)
    if decoded is not None:
        content, consumed = decoded
        if all(value in (0, 0xFF) for value in packed[consumed:]):
            return content, True
    return packed, False


def read_encounters(path: Path) -> dict[int, dict[int, int]]:
    blob = path.read_bytes()
    info = parse_garc(blob)
    members = {
        record.entry_index: blob[
            record.abs_start : record.abs_start + record.length
        ]
        for record in info.records
        if record.sub_index == 0
    }
    missing = sorted(set(range(ZONE_COUNT + 1)) - members.keys())
    if missing:
        raise ValueError(f"{path}: missing encounter members {missing[:20]}")
    master, _compressed = _decode_member(members[ZONE_COUNT])
    if len(master) < ZONE_COUNT * ZONE_RECORD_SIZE:
        raise ValueError(f"{path}: ZoneData member {ZONE_COUNT} is truncated")

    groups: dict[int, dict[int, int]] = {}
    for zone_index in range(ZONE_COUNT):
        group = struct.unpack_from(
            "<H", master, zone_index * ZONE_RECORD_SIZE + TOWN_MAP_GROUP_OFFSET
        )[0]
        if group not in TOWN_MAP_GROUPS:
            raise ValueError(f"{path}: unknown TownMapGroup {group}")
        flags = groups.setdefault(group, {})
        zone, _compressed = _decode_member(members[zone_index])
        label = f"{path}: map {zone_index:03d}"
        if len(zone) < 24 or zone[:4] != b"ZO\x04\x00":
            raise ValueError(f"{label}: expected a four-section ZO map")
        offsets = struct.unpack_from("<5I", zone, 4)
        if (
            offsets[0] < 24
            or offsets[-1] != len(zone)
            or any(start > end for start, end in zip(offsets, offsets[1:]))
        ):
            raise ValueError(f"{label}: invalid map section bounds")
        start, end = offsets[3:]
        if start == end:
            continue
        # Locations need each species field, not the final slot's level bytes.
        required_size = 16 + (METHOD_RANGES[-1][1] - 1) * 4 + 2
        if end - start < required_size:
            raise ValueError(
                f"{label}: encounter species fields are truncated"
            )
        for first, stop, flag in METHOD_RANGES:
            for slot in range(first, stop):
                species = struct.unpack_from(
                    "<H", zone, start + 16 + slot * 4
                )[0] & SPECIES_ID_MASK
                if species >= SPECIES_COUNT:
                    raise ValueError(f"{label}: invalid species ID {species}")
                if species:
                    flags[species] = flags.get(species, 0) | flag

    missing_groups = sorted(set(TOWN_MAP_GROUPS) - groups.keys())
    if missing_groups:
        raise ValueError(f"{path}: missing TownMapGroups {missing_groups}")
    return groups


def read_distribution(blob: bytes) -> tuple[bytes, bool]:
    info = parse_garc(blob)
    for record in info.records:
        if record.entry_index == 0 and record.sub_index == 0:
            content, compressed = _decode_member(
                blob[record.abs_start : record.abs_start + record.length]
            )
            expected_size = SPECIES_COUNT * DISTRIBUTION_COLUMNS * 4
            if len(content) != expected_size:
                raise ValueError(
                    "Pokédex member 0 must contain "
                    f"{SPECIES_COUNT} rows and {DISTRIBUTION_COLUMNS} columns"
                )
            return content, compressed
    raise ValueError("Pokédex GARC member 0 is missing")


def _select_archive(staged: Path, base: Path, relative_path: Path) -> Path:
    updated = staged / relative_path
    return updated if updated.exists() else base / relative_path


def _replace_distribution(
    blob: bytes, content: bytes, compressed: bool
) -> bytes:
    packed = content
    if compressed:
        # Literal-only LZ11 preserves the format without a new dependency.
        stream = bytearray(struct.pack("<I", (len(content) << 8) | 0x11))
        for offset in range(0, len(content), 8):
            stream.append(0)
            stream.extend(content[offset : offset + 8])
        packed = bytes(stream)

    info = parse_garc(blob)
    target = next(
        record
        for record in info.records
        if record.entry_index == 0 and record.sub_index == 0
    )
    if any(
        record != target
        and record.abs_start < target.abs_start + target.length
        and target.abs_start < record.abs_start + record.length
        for record in info.records
    ):
        raise ValueError("Pokédex member 0 overlaps another member")
    if len(packed) == target.length:
        output = bytearray(blob)
        output[target.abs_start : target.abs_start + target.length] = packed
        return bytes(output)

    entries = [[] for _ in range(info.entry_count)]
    for record in info.records:
        payload = (
            packed
            if record == target
            else blob[record.abs_start : record.abs_start + record.length]
        )
        entries[record.entry_index].append(
            GarcSubEntry(record.sub_index, payload)
        )
    padding = (
        struct.unpack_from("<I", blob, 0x20)[0]
        if info.version == 0x0600
        else 4
    )
    if not 0 < padding <= 0x10000:
        raise ValueError("invalid Pokédex GARC alignment")
    return pack_garc(GarcArchive(info.version, entries, padding))


def repair_encounter_locations(staged: Path, base: Path) -> int:
    """Repair the staged Pokédex GARC, using base files for partial inputs.

    Reject a mismatched clean encounter/table pair before writing. Preserve
    row zero, internal columns, metadata bits and other archive members.
    """
    destination = staged / DISTRIBUTION_RELPATH
    if destination.resolve() == (base / DISTRIBUTION_RELPATH).resolve():
        raise ValueError("Pokédex repair must not overwrite the clean base")
    print("Repairing Pokédex encounter locations...", flush=True)
    vanilla = read_encounters(base / ENCOUNTER_RELPATH)
    clean_table, _compressed = read_distribution(
        (base / DISTRIBUTION_RELPATH).read_bytes()
    )
    for column, group in enumerate(TOWN_MAP_GROUPS, start=2):
        for species in range(1, SPECIES_COUNT):
            offset = (species * DISTRIBUTION_COLUMNS + column) * 4
            value = struct.unpack_from("<I", clean_table, offset)[0]
            if value & METHOD_MASK != vanilla[group].get(species, 0):
                raise ValueError(
                    "Clean encounter and Pokédex GARCs do not match at "
                    f"species {species}, TownMapGroup {group}. "
                    "Use files from the same untouched X or Y ROM."
                )

    modified = read_encounters(
        _select_archive(staged, base, ENCOUNTER_RELPATH)
    )
    current = _select_archive(staged, base, DISTRIBUTION_RELPATH).read_bytes()
    template, compressed = read_distribution(current)
    rebuilt = bytearray(template)
    changed_cells = 0
    for column, group in enumerate(TOWN_MAP_GROUPS, start=2):
        for species in range(1, SPECIES_COUNT):
            offset = (species * DISTRIBUTION_COLUMNS + column) * 4
            old_value = struct.unpack_from("<I", template, offset)[0]
            new_value = (old_value & ~METHOD_MASK) | modified[group].get(
                species, 0
            )
            if old_value != new_value:
                struct.pack_into("<I", rebuilt, offset, new_value)
                changed_cells += 1

    repaired = current
    if changed_cells:
        content = bytes(rebuilt)
        repaired = _replace_distribution(current, content, compressed)
        if read_distribution(repaired)[0] != content:
            raise RuntimeError("Repaired Pokédex GARC failed verification")
    if not destination.is_file() or repaired != current:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(repaired)
    print(f"Pokédex encounter locations: {changed_cells} cells repaired.")
    return changed_cells
