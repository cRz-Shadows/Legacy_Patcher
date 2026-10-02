"""Validated pk3DS Game Text repair and persistent metadata."""

from __future__ import annotations

import base64
import datetime as _datetime
import difflib
import hashlib
import os
import shutil
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .runtime import app_dir, bundle_dir

REPLACE_WORKING_GARC = True


MAKE_BACKUP = True


OUTPUT_SUFFIX = ".repaired"


REPORT_NAME = "a074_pk3ds_text_repair_report.txt"


EXPECTED_GARC_VERSION = 0x0400


GARC_PADDING = 4


USE_PERSISTENT_METADATA_STATE = True


METADATA_STATE_SUFFIX = ".text_repair_state"


INFER_NEW_ENTRY_METADATA_FROM_NEIGHBOURS = True


EXPECTED_TOP_LEVEL_ENTRIES = 151


RUN_DEEP_VALIDATION = True


MAX_VALIDATION_EXAMPLES = 250


FAIL_ON_POST_VALIDATION_WARNINGS = True


TEXT_KEY_BASE = 0x7C89


TEXT_KEY_ADVANCE = 0x2983


TEXT_VARIABLE = 0x0010


TEXT_TERMINATOR = 0x0000


TEXT_NULL = 0xBDFF


TEXT_WAIT = 0xBE02


@dataclass(frozen=True)
class ReferenceLine:
    attribute: int
    fixed_tail_zeros: int
    visible_digest: bytes

    visible_words: tuple[int, ...] | None = None


@dataclass
class TextLine:
    attribute: int
    words: list[int]


@dataclass
class GarcSubEntry:
    index: int
    payload: bytes


@dataclass
class GarcArchive:
    version: int
    entries: list[list[GarcSubEntry]]
    padding: int = GARC_PADDING


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    location: str
    message: str


@dataclass
class ValidationResult:
    phase: str
    issues: list[ValidationIssue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    files_checked: int = 0
    lines_checked: int = 0
    variables_checked: int = 0
    largest_line_words: int = 0
    garc_entries_checked: int = 0

    def add(
        self,
        severity: str,
        code: str,
        location: str,
        message: str,
    ) -> None:
        severity = severity.lower()
        key = f"{severity}:{code}"
        self.counts[key] = self.counts.get(key, 0) + 1
        if len(self.issues) < MAX_VALIDATION_EXAMPLES:
            self.issues.append(
                ValidationIssue(severity, code, location, message)
            )

    @property
    def error_count(self) -> int:
        return sum(
            count
            for key, count in self.counts.items()
            if key.startswith("error:")
        )

    @property
    def warning_count(self) -> int:
        return sum(
            count
            for key, count in self.counts.items()
            if key.startswith("warning:")
        )

    @property
    def safe(self) -> bool:
        return self.error_count == 0


def read_u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) // boundary * boundary


def line_key(index: int) -> int:
    return (TEXT_KEY_BASE + index * TEXT_KEY_ADVANCE) & 0xFFFF


def crypt_line(data: bytes, key: int) -> bytes:
    if len(data) % 2:
        raise ValueError("text line byte count is not even")

    result = bytearray(len(data))
    for offset in range(0, len(data), 2):
        value = read_u16(data, offset) ^ key
        struct.pack_into("<H", result, offset, value)
        key = ((key << 3) | (key >> 13)) & 0xFFFF
    return bytes(result)


def words_to_bytes(words: list[int]) -> bytes:
    if not words:
        return b""
    return struct.pack("<" + "H" * len(words), *words)


def visible_digest(words: list[int]) -> bytes:
    return hashlib.blake2b(words_to_bytes(words), digest_size=8).digest()


def load_blueprint() -> list[list[ReferenceLine]]:
    blueprint_path = (
        bundle_dir() / "rom_patcher" / "data" / "game_text_blueprint.b85"
    )
    encoded = "".join(blueprint_path.read_text(encoding="ascii").split())
    raw = zlib.decompress(base64.b85decode(encoded.encode("ascii")))

    if raw[:9] != b"A074META1":
        raise ValueError("embedded repair blueprint has an invalid signature")

    position = 9
    file_count = read_u16(raw, position)
    position += 2

    files: list[list[ReferenceLine]] = []
    for _ in range(file_count):
        line_count = read_u16(raw, position)
        position += 2
        lines: list[ReferenceLine] = []

        for _ in range(line_count):
            attribute, tail_zeros = struct.unpack_from("<HH", raw, position)
            position += 4
            digest = raw[position : position + 8]
            position += 8
            lines.append(ReferenceLine(attribute, tail_zeros, digest))

        files.append(lines)

    if position != len(raw):
        raise ValueError("embedded repair blueprint has trailing data")
    if len(files) != EXPECTED_TOP_LEVEL_ENTRIES:
        raise ValueError(
            f"embedded blueprint has {len(files)} files, expected "
            f"{EXPECTED_TOP_LEVEL_ENTRIES}"
        )
    return files


STATE_MAGIC = b"A074STATE1"


def script_directory(report_folder: Path | None = None) -> Path:
    """Keep repair backups, reports, and state outside the game tree."""
    folder = (
        Path(report_folder)
        if report_folder is not None
        else app_dir() / "reports" / "text_repair"
    )
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def metadata_state_path(
    source_path: Path, report_folder: Path | None = None
) -> Path:
    """Keep independent metadata histories for each selected game tree."""
    project_key = hashlib.sha256(
        str(source_path.resolve().parent).encode("utf-8")
    ).hexdigest()[:16]
    return (
        script_directory(report_folder)
        / f"{project_key}_{source_path.name}{METADATA_STATE_SUFFIX}"
    )


def legacy_metadata_state_path(source_path: Path) -> Path:
    """
    Return the location used by older versions of this repair script.

    The current version reads this path as a fallback, but all newly generated
    state is written in the repair reports folder.
    """
    return source_path.with_name(source_path.name + METADATA_STATE_SUFFIX)


def reference_snapshot_from_garc(
    garc_data: bytes,
) -> list[list[ReferenceLine]]:
    """Capture current visible lines and repaired metadata for later runs."""
    archive = unpack_garc(garc_data)
    if len(archive.entries) != EXPECTED_TOP_LEVEL_ENTRIES:
        raise ValueError(
            f"cannot create metadata state from {len(archive.entries)} files"
        )

    snapshot: list[list[ReferenceLine]] = []
    for file_index, entry in enumerate(archive.entries):
        if len(entry) != 1 or entry[0].index != 0:
            raise ValueError(
                f"entry {file_index:03d} is not a single normal subfile"
            )
        lines = parse_text_file(
            entry[0].payload,
            label=f"{file_index:03d}.bin state",
        )
        file_lines: list[ReferenceLine] = []
        for line in lines:
            visible, _ = canonical_visible_words(line.words)
            terminator = find_text_terminator(line.words)
            tail = line.words[terminator + 1 :]
            if any(value != 0 for value in tail):
                raise ValueError(
                    f"{file_index:03d}.bin has a nonzero post-terminator tail"
                )
            file_lines.append(
                ReferenceLine(
                    line.attribute,
                    len(tail),
                    visible_digest(visible),
                    tuple(visible),
                )
            )
        snapshot.append(file_lines)
    return snapshot


def encode_metadata_state(
    reference_files: list[list[ReferenceLine]],
    repaired_garc_sha256: str,
) -> bytes:
    """
    Encode a compact, compressed state.

    Complete visible words are stored only in this sidecar, never substituted
    into the game. They are used exclusively to align old custom entries with
    the next pk3DS rebuild.
    """
    if len(reference_files) != EXPECTED_TOP_LEVEL_ENTRIES:
        raise ValueError("metadata state has the wrong file count")

    raw = bytearray(STATE_MAGIC)
    raw.extend(bytes.fromhex(repaired_garc_sha256))
    raw.extend(struct.pack("<H", len(reference_files)))

    for file_lines in reference_files:
        raw.extend(struct.pack("<I", len(file_lines)))
        for line in file_lines:
            words = line.visible_words
            if words is None:
                raise ValueError("metadata state line is missing visible words")
            raw.extend(
                struct.pack(
                    "<HHI",
                    line.attribute,
                    line.fixed_tail_zeros,
                    len(words),
                )
            )
            raw.extend(words_to_bytes(list(words)))

    return zlib.compress(bytes(raw), level=9)


def decode_metadata_state(
    state_data: bytes,
) -> tuple[list[list[ReferenceLine]], str]:
    raw = zlib.decompress(state_data)
    if not raw.startswith(STATE_MAGIC):
        raise ValueError("metadata state signature is invalid")

    position = len(STATE_MAGIC)
    if position + 34 > len(raw):
        raise ValueError("metadata state is truncated")

    prior_hash = raw[position : position + 32].hex()
    position += 32
    file_count = read_u16(raw, position)
    position += 2

    if file_count != EXPECTED_TOP_LEVEL_ENTRIES:
        raise ValueError(
            f"metadata state has {file_count} files, expected "
            f"{EXPECTED_TOP_LEVEL_ENTRIES}"
        )

    files: list[list[ReferenceLine]] = []
    for _ in range(file_count):
        if position + 4 > len(raw):
            raise ValueError("metadata state is truncated before a file")
        line_count = read_u32(raw, position)
        position += 4
        lines: list[ReferenceLine] = []

        for _ in range(line_count):
            if position + 8 > len(raw):
                raise ValueError("metadata state is truncated before a line")
            attribute, tail_zeros, word_count = struct.unpack_from(
                "<HHI", raw, position
            )
            position += 8
            byte_count = word_count * 2
            if position + byte_count > len(raw):
                raise ValueError("metadata state line data is truncated")
            words = (
                tuple(
                    struct.unpack_from(
                        "<" + "H" * word_count,
                        raw,
                        position,
                    )
                )
                if word_count
                else ()
            )
            position += byte_count
            lines.append(
                ReferenceLine(
                    attribute,
                    tail_zeros,
                    visible_digest(list(words)),
                    words,
                )
            )
        files.append(lines)

    if position != len(raw):
        raise ValueError("metadata state contains trailing data")
    return files, prior_hash


def load_metadata_state(
    state_path: Path,
) -> tuple[list[list[ReferenceLine]] | None, str | None, str | None]:
    if not USE_PERSISTENT_METADATA_STATE or not state_path.is_file():
        return None, None, None
    try:
        files, prior_hash = decode_metadata_state(state_path.read_bytes())
        return files, prior_hash, None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def write_metadata_state(
    state_path: Path,
    reference_files: list[list[ReferenceLine]],
    repaired_garc_sha256: str,
) -> None:
    if not USE_PERSISTENT_METADATA_STATE:
        return

    payload = encode_metadata_state(reference_files, repaired_garc_sha256)
    temporary = state_path.with_name(state_path.name + ".writing")
    temporary.write_bytes(payload)
    if temporary.read_bytes() != payload:
        temporary.unlink(missing_ok=True)
        raise IOError("metadata state failed its write verification")
    os.replace(temporary, state_path)


def find_text_terminator(words: list[int]) -> int:
    """
    Find the real text terminator.

    Zero can appear as an argument inside a text variable, so a simple
    words.index(0) is unsafe. pk3DS also writes TEXT_NULL and TEXT_WAIT with a
    count of 1 even though each has one argument, so those controls are handled
    explicitly.
    """
    index = 0
    while index < len(words):
        value = words[index]

        if value == TEXT_TERMINATOR:
            return index

        if value != TEXT_VARIABLE:
            index += 1
            continue

        if index + 2 >= len(words):
            raise ValueError("truncated text variable")

        count = words[index + 1]
        variable = words[index + 2]

        if variable in (TEXT_NULL, TEXT_WAIT):
            if index + 3 >= len(words):
                raise ValueError("truncated special text variable")
            index += 4
            continue

        if count < 1:
            raise ValueError(f"invalid text variable count {count}")

        step = 2 + count
        if index + step > len(words):
            raise ValueError("text variable extends past the line")
        index += step

    raise ValueError("text line has no terminator")


def canonical_visible_words(words: list[int]) -> tuple[list[int], int]:
    """
    Return the editor-visible portion and repair malformed special counts.

    The returned count is the number of TEXT_NULL/TEXT_WAIT controls changed
    from pk3DS's incorrect count 1 to the correct count 2.
    """
    terminator = find_text_terminator(words)
    result = list(words[: terminator + 1])
    repaired_controls = 0

    index = 0
    while index < terminator:
        value = result[index]

        if value != TEXT_VARIABLE:
            index += 1
            continue

        count = result[index + 1]
        variable = result[index + 2]

        if variable in (TEXT_NULL, TEXT_WAIT):
            if count != 2:
                result[index + 1] = 2
                repaired_controls += 1
            index += 4
            continue

        index += 2 + count

    return result, repaired_controls


def parse_text_file(data: bytes, *, label: str) -> list[TextLine]:
    if len(data) < 20:
        raise ValueError(f"{label}: text file is too small")

    section_count, line_count = struct.unpack_from("<HH", data, 0)
    total_length, initial_key, section_offset = struct.unpack_from(
        "<III", data, 4
    )

    if section_count != 1:
        raise ValueError(f"{label}: expected one text section")
    if initial_key != 0:
        raise ValueError(f"{label}: unexpected initial text key")
    if section_offset != 0x10:
        raise ValueError(
            f"{label}: unexpected section offset 0x{section_offset:X}"
        )
    if section_offset + total_length != len(data):
        raise ValueError(f"{label}: total length does not match file size")

    section_length = read_u32(data, section_offset)
    if section_length != total_length:
        raise ValueError(f"{label}: section and total lengths differ")

    table_end = section_offset + 4 + line_count * 8
    if table_end > len(data):
        raise ValueError(f"{label}: line table extends past the file")

    lines: list[TextLine] = []
    for index in range(line_count):
        record_offset = section_offset + 4 + index * 8
        relative_offset, length_words, attribute = struct.unpack_from(
            "<IHH", data, record_offset
        )
        absolute_offset = section_offset + relative_offset
        absolute_end = absolute_offset + length_words * 2

        if absolute_offset < table_end or absolute_end > len(data):
            raise ValueError(
                f"{label}: line {index} points outside its valid data area"
            )

        encrypted = data[absolute_offset:absolute_end]
        decrypted = crypt_line(encrypted, line_key(index))
        words = list(
            struct.unpack("<" + "H" * (len(decrypted) // 2), decrypted)
        )
        find_text_terminator(words)
        lines.append(TextLine(attribute, words))

    return lines


def _line_similarity(
    reference: ReferenceLine,
    current_words: list[int],
) -> float:
    if reference.visible_words is None:
        return 0.0
    if reference.visible_digest == visible_digest(current_words):
        return 1.0
    return difflib.SequenceMatcher(
        a=reference.visible_words,
        b=tuple(current_words),
        autojunk=False,
    ).ratio()


def _proportional_mapping(
    ref_start: int,
    ref_end: int,
    cur_start: int,
    cur_end: int,
) -> dict[int, int]:
    """
    Conservative fallback used only when the embedded compact blueprint lacks
    complete words. It distributes the smaller side across the larger side.
    """
    ref_count = ref_end - ref_start
    cur_count = cur_end - cur_start
    result: dict[int, int] = {}
    if ref_count <= 0 or cur_count <= 0:
        return result

    if ref_count == cur_count:
        return {
            cur_start + offset: ref_start + offset
            for offset in range(ref_count)
        }

    if ref_count == 1:
        result[cur_start + (cur_count - 1) // 2] = ref_start
        return result
    if cur_count == 1:
        result[cur_start] = ref_start + (ref_count - 1) // 2
        return result

    if cur_count > ref_count:
        for ref_offset in range(ref_count):
            cur_offset = round(ref_offset * (cur_count - 1) / (ref_count - 1))
            result[cur_start + cur_offset] = ref_start + ref_offset
    else:
        for cur_offset in range(cur_count):
            ref_offset = round(cur_offset * (ref_count - 1) / (cur_count - 1))
            result[cur_start + cur_offset] = ref_start + ref_offset
    return result


def _align_replace_block(
    current_visible: list[list[int]],
    reference_lines: list[ReferenceLine],
    ref_start: int,
    ref_end: int,
    cur_start: int,
    cur_end: int,
) -> dict[int, int]:
    """
    Align an edited block while allowing inserted and removed entries.

    A substitution is cheaper than a delete plus insert, so an edited existing
    entry remains mapped. The most textually similar current line wins when a
    new entry was inserted beside an edited old entry.
    """
    ref_count = ref_end - ref_start
    cur_count = cur_end - cur_start
    if ref_count <= 0 or cur_count <= 0:
        return {}

    if ref_count == cur_count:
        return {
            cur_start + offset: ref_start + offset
            for offset in range(ref_count)
        }

    if any(
        reference_lines[index].visible_words is None
        for index in range(ref_start, ref_end)
    ):
        return _proportional_mapping(
            ref_start,
            ref_end,
            cur_start,
            cur_end,
        )

    if ref_count * cur_count > 250_000:
        return _proportional_mapping(
            ref_start,
            ref_end,
            cur_start,
            cur_end,
        )

    gap_cost = 1.0
    dp = [[0.0] * (cur_count + 1) for _ in range(ref_count + 1)]
    action = [[""] * (cur_count + 1) for _ in range(ref_count + 1)]

    for i in range(1, ref_count + 1):
        dp[i][0] = i * gap_cost
        action[i][0] = "delete"
    for j in range(1, cur_count + 1):
        dp[0][j] = j * gap_cost
        action[0][j] = "insert"

    for i in range(1, ref_count + 1):
        reference = reference_lines[ref_start + i - 1]
        for j in range(1, cur_count + 1):
            current = current_visible[cur_start + j - 1]
            similarity = _line_similarity(reference, current)

            substitution_cost = 0.70 + 1.10 * (1.0 - similarity)
            relative_ref = (i - 0.5) / ref_count
            relative_cur = (j - 0.5) / cur_count
            substitution_cost += 0.12 * abs(relative_ref - relative_cur)

            candidates = (
                (
                    dp[i - 1][j - 1] + substitution_cost,
                    "match",
                ),
                (dp[i - 1][j] + gap_cost, "delete"),
                (dp[i][j - 1] + gap_cost, "insert"),
            )
            best_cost, best_action = min(
                candidates,
                key=lambda item: (
                    item[0],
                    (
                        0
                        if item[1] == "match"
                        else 1 if item[1] == "insert" else 2
                    ),
                ),
            )
            dp[i][j] = best_cost
            action[i][j] = best_action

    mapping: dict[int, int] = {}
    i = ref_count
    j = cur_count
    while i or j:
        step = action[i][j]
        if step == "match":
            mapping[cur_start + j - 1] = ref_start + i - 1
            i -= 1
            j -= 1
        elif step == "delete":
            i -= 1
        elif step == "insert":
            j -= 1
        else:
            raise ValueError("internal text alignment failure")

    return mapping


def build_reference_mapping(
    current_lines: list[TextLine],
    reference_lines: list[ReferenceLine],
) -> dict[int, int]:
    """
    Map current entry indexes to structural reference indexes.

    Exact unchanged lines form anchors. Edited blocks are then aligned while
    permitting additions and removals. With a persistent state, complete prior
    visible words allow an edited old entry to be distinguished from a newly
    inserted entry.
    """
    current_visible = [
        canonical_visible_words(line.words)[0] for line in current_lines
    ]
    current_hashes = [visible_digest(words) for words in current_visible]
    reference_hashes = [line.visible_digest for line in reference_lines]

    matcher = difflib.SequenceMatcher(
        a=reference_hashes,
        b=current_hashes,
        autojunk=False,
    )

    mapping: dict[int, int] = {}
    for tag, ref_start, ref_end, cur_start, cur_end in matcher.get_opcodes():
        if tag == "equal":
            for delta in range(ref_end - ref_start):
                mapping[cur_start + delta] = ref_start + delta
            continue

        if tag == "replace":
            mapping.update(
                _align_replace_block(
                    current_visible,
                    reference_lines,
                    ref_start,
                    ref_end,
                    cur_start,
                    cur_end,
                )
            )

    return mapping


def infer_new_entry_metadata(
    current_index: int,
    current_line: TextLine,
    mapping: dict[int, int],
    reference_lines: list[ReferenceLine],
) -> tuple[int, int, bool]:
    """
    Return metadata for a newly inserted entry.

    Explicit nonzero metadata already present in the current file is preserved.
    Otherwise, identical metadata on both mapped neighbours, or a uniform
    reference file, is safe to inherit. Normal dialogue remains 0/0.
    """
    if current_line.attribute:
        return current_line.attribute, 0, False
    if not INFER_NEW_ENTRY_METADATA_FROM_NEIGHBOURS:
        return 0, 0, False

    previous = None
    following = None

    for index in range(current_index - 1, -1, -1):
        if index in mapping:
            previous = reference_lines[mapping[index]]
            break

    max_current = max(mapping, default=-1)
    for index in range(current_index + 1, max_current + 2):
        if index in mapping:
            following = reference_lines[mapping[index]]
            break

    if previous is not None and following is not None:
        previous_meta = (
            previous.attribute,
            previous.fixed_tail_zeros,
        )
        following_meta = (
            following.attribute,
            following.fixed_tail_zeros,
        )
        if previous_meta == following_meta:
            return previous_meta[0], previous_meta[1], True

    unique_meta = {
        (line.attribute, line.fixed_tail_zeros) for line in reference_lines
    }
    if len(unique_meta) == 1:
        attribute, tail_zeros = next(iter(unique_meta))
        return attribute, tail_zeros, True

    return 0, 0, False


def rebuild_text_file(
    original_data: bytes,
    reference_lines: list[ReferenceLine],
    *,
    label: str,
) -> tuple[bytes, dict[str, int]]:
    current_lines = parse_text_file(original_data, label=label)
    mapping = build_reference_mapping(current_lines, reference_lines)

    line_count = len(current_lines)
    section_table_size = 4 + line_count * 8
    section = bytearray(section_table_size)
    cursor = section_table_size
    table_records: list[tuple[int, int, int]] = []

    stats = {
        "lines": line_count,
        "mapped_lines": len(mapping),
        "unmapped_lines": line_count - len(mapping),
        "new_entries_inferred": 0,
        "attributes_restored": 0,
        "special_controls_repaired": 0,
        "line_padding_repaired": 0,
        "fixed_width_lines_restored": 0,
    }

    expected_visible: list[list[int]] = []

    for current_index, current_line in enumerate(current_lines):
        visible_words, repaired_controls = canonical_visible_words(
            current_line.words
        )
        expected_visible.append(visible_words)
        stats["special_controls_repaired"] += repaired_controls

        if current_index in mapping:
            reference = reference_lines[mapping[current_index]]
            attribute = reference.attribute
            tail_zeros = reference.fixed_tail_zeros
        else:

            attribute, tail_zeros, inferred = infer_new_entry_metadata(
                current_index,
                current_line,
                mapping,
                reference_lines,
            )
            if inferred:
                stats["new_entries_inferred"] += 1

        if attribute != current_line.attribute:
            stats["attributes_restored"] += 1
        current_terminator = find_text_terminator(current_line.words)
        current_tail = current_line.words[current_terminator + 1 :]
        expected_tail = [0] * tail_zeros
        if current_tail != expected_tail:
            stats["line_padding_repaired"] += 1
            if tail_zeros:
                stats["fixed_width_lines_restored"] += 1

        plaintext_words = visible_words + expected_tail
        encrypted = crypt_line(
            words_to_bytes(plaintext_words),
            line_key(current_index),
        )

        aligned_cursor = align(cursor, 4)
        if aligned_cursor > len(section):
            section.extend(b"\x00" * (aligned_cursor - len(section)))
        cursor = aligned_cursor

        relative_offset = cursor
        section.extend(encrypted)
        cursor += len(encrypted)

        table_records.append((relative_offset, len(plaintext_words), attribute))

    final_size = align(cursor, 4)
    if final_size > len(section):
        section.extend(b"\x00" * (final_size - len(section)))

    struct.pack_into("<I", section, 0, len(section))
    for index, record in enumerate(table_records):
        struct.pack_into("<IHH", section, 4 + index * 8, *record)

    header = struct.pack(
        "<HHIII",
        1,
        line_count,
        len(section),
        0,
        0x10,
    )
    rebuilt = header + section

    rebuilt_lines = parse_text_file(rebuilt, label=f"{label} rebuilt")
    if len(rebuilt_lines) != len(expected_visible):
        raise ValueError(f"{label}: rebuilt line count changed")

    for index, rebuilt_line in enumerate(rebuilt_lines):
        rebuilt_visible, _ = canonical_visible_words(rebuilt_line.words)
        if rebuilt_visible != expected_visible[index]:
            raise ValueError(
                f"{label}: visible dialogue changed at entry {index}"
            )

    return rebuilt, stats


def unpack_garc(data: bytes) -> GarcArchive:
    if len(data) < 0x20 or read_u32(data, 0) != 0x47415243:
        raise ValueError("target is not a GARC archive")

    header_size = read_u32(data, 4)
    version = read_u16(data, 0x0A)
    section_count = read_u32(data, 0x0C)
    data_offset = read_u32(data, 0x10)

    if version != EXPECTED_GARC_VERSION:
        raise ValueError(
            f"expected GARC version 0x{EXPECTED_GARC_VERSION:04X}, "
            f"found 0x{version:04X}"
        )
    if section_count != 4:
        raise ValueError(f"unexpected GARC section count {section_count}")

    position = header_size
    if data[position : position + 4] != b"OTAF":
        raise ValueError("GARC is missing FATO/OTAF")

    fato_header_size = read_u32(data, position + 4)
    entry_count = read_u16(data, position + 8)
    position += fato_header_size

    if data[position : position + 4] != b"BTAF":
        raise ValueError("GARC is missing FATB/BTAF")

    declared_file_count = read_u32(data, position + 8)
    position += 0x0C

    entries: list[list[GarcSubEntry]] = []
    actual_file_count = 0

    for _entry_index in range(entry_count):
        vector = read_u32(data, position)
        position += 4
        subentries: list[GarcSubEntry] = []

        for bit in range(32):
            if not (vector & (1 << bit)):
                continue

            start, _end, length = struct.unpack_from("<III", data, position)
            position += 0x0C

            payload_start = data_offset + start
            payload_end = payload_start + length
            if payload_end > len(data):
                raise ValueError("GARC subfile extends past the archive")

            subentries.append(
                GarcSubEntry(bit, data[payload_start:payload_end])
            )
            actual_file_count += 1

        entries.append(subentries)

    if actual_file_count != declared_file_count:
        raise ValueError("GARC file count does not match its FATB declaration")

    return GarcArchive(version, entries, GARC_PADDING)


def pack_garc(archive: GarcArchive) -> bytes:
    version = archive.version
    padding = archive.padding
    entries = archive.entries

    if version not in (0x0400, 0x0600):
        raise ValueError(f"unsupported GARC version 0x{version:04X}")
    if padding <= 0:
        raise ValueError("GARC padding must be positive")

    entry_count = len(entries)
    file_count = sum(len(entry) for entry in entries)

    fato_offsets: list[int] = []
    fatb_records: list[tuple[int, list[tuple[int, int, int, bytes]]]] = []

    fatb_table_offset = 0
    data_cursor = 0
    largest_unpadded = 0
    largest_padded = 0

    for subentries in entries:
        vector = 0
        records: list[tuple[int, int, int, bytes]] = []

        fato_offsets.append(fatb_table_offset)
        fatb_table_offset += 4

        for subentry in sorted(subentries, key=lambda item: item.index):
            if not 0 <= subentry.index < 32:
                raise ValueError("GARC subentry index is outside 0..31")

            vector |= 1 << subentry.index
            payload = subentry.payload
            padded_length = align(len(payload), padding)
            start = data_cursor
            end = start + padded_length

            records.append((start, end, len(payload), payload))
            data_cursor = end
            largest_unpadded = max(largest_unpadded, len(payload))
            largest_padded = max(largest_padded, padded_length)
            fatb_table_offset += 12

        fatb_records.append((vector, records))

    garc_header_size = 0x24 if version == 0x0600 else 0x1C
    fato_header_size = 0x0C + entry_count * 4
    fatb_header_size = 0x0C + fatb_table_offset

    output = bytearray()
    output += struct.pack("<I", 0x47415243)
    output += struct.pack("<I", garc_header_size)
    output += struct.pack("<H", 0xFEFF)
    output += struct.pack("<H", version)
    output += struct.pack("<I", 4)
    output += b"\x00" * 12

    if version == 0x0600:
        output += b"\x00" * 8

    output += struct.pack("<I", 0x4641544F)
    output += struct.pack("<I", fato_header_size)
    output += struct.pack("<H", entry_count)
    output += struct.pack("<H", 0xFFFF)
    for offset in fato_offsets:
        output += struct.pack("<I", offset)

    output += struct.pack("<I", 0x46415442)
    output += struct.pack("<I", fatb_header_size)
    output += struct.pack("<I", file_count)

    for vector, records in fatb_records:
        output += struct.pack("<I", vector)
        for start, end, length, _payload in records:
            output += struct.pack("<III", start, end, length)

    output += struct.pack("<I", 0x46494D42)
    output += struct.pack("<I", 0x0C)
    fimb_size_offset = len(output)
    output += b"\x00" * 4

    data_offset = len(output)

    for _vector, records in fatb_records:
        for start, end, length, payload in records:
            if len(output) - data_offset != start:
                raise ValueError("internal GARC data offset mismatch")

            output += payload
            output += b"\xff" * (end - start - length)

    fimb_data_size = len(output) - data_offset

    struct.pack_into("<I", output, 0x10, data_offset)
    struct.pack_into("<I", output, 0x14, len(output))

    if version == 0x0400:
        struct.pack_into("<I", output, 0x18, largest_unpadded)
    else:
        struct.pack_into("<I", output, 0x18, largest_padded)
        struct.pack_into("<I", output, 0x1C, largest_unpadded)
        struct.pack_into("<I", output, 0x20, padding)

    struct.pack_into("<I", output, fimb_size_offset, fimb_data_size)
    return bytes(output)


def timestamped_backup_path(
    path: Path, report_folder: Path | None = None
) -> Path:
    """
    Create backup names in the repair reports folder, not beside the GARC.
    """
    stamp = _datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return (
        script_directory(report_folder)
        / f"{path.name}.before_text_repair_{stamp}.bak"
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validation_severity(strict: bool) -> str:
    return "error" if strict else "warning"


def validate_garc_container(
    data: bytes,
    result: ValidationResult,
    *,
    strict: bool,
) -> GarcArchive | None:
    """
    Validate the complete GARC container, not only its extracted payloads.
    """
    label = "a/0/7/4"

    try:
        archive = unpack_garc(data)
    except Exception as exc:
        result.add(
            "error",
            "garc_unpack_failed",
            label,
            f"{type(exc).__name__}: {exc}",
        )
        return None

    result.garc_entries_checked = len(archive.entries)

    if len(data) < 0x1C:
        result.add(
            "error", "garc_header_truncated", label, "header is truncated"
        )
        return archive

    header_size = read_u32(data, 4)
    byte_order = read_u16(data, 8)
    version = read_u16(data, 0x0A)
    section_count = read_u32(data, 0x0C)
    data_offset = read_u32(data, 0x10)
    declared_size = read_u32(data, 0x14)
    declared_largest = read_u32(data, 0x18)

    if header_size != 0x1C:
        result.add(
            "error",
            "garc_header_size",
            label,
            f"header size is 0x{header_size:X}, expected 0x1C",
        )
    if byte_order != 0xFEFF:
        result.add(
            "error",
            "garc_byte_order",
            label,
            f"byte-order marker is 0x{byte_order:04X}",
        )
    if version != EXPECTED_GARC_VERSION:
        result.add(
            "error",
            "garc_version",
            label,
            f"version is 0x{version:04X}, expected"
            f" 0x{EXPECTED_GARC_VERSION:04X}",
        )
    if section_count != 4:
        result.add(
            "error",
            "garc_section_count",
            label,
            f"section count is {section_count}, expected 4",
        )
    if declared_size != len(data):
        result.add(
            "error",
            "garc_size_mismatch",
            label,
            f"header declares {declared_size} bytes, file has {len(data)}",
        )
    if data_offset > len(data):
        result.add(
            "error",
            "garc_data_offset",
            label,
            f"data offset 0x{data_offset:X} is outside the file",
        )
        return archive

    try:
        fato_start = header_size
        if data[fato_start : fato_start + 4] != b"OTAF":
            raise ValueError("missing OTAF")
        fato_size = read_u32(data, fato_start + 4)
        entry_count = read_u16(data, fato_start + 8)
        expected_fato_size = 0x0C + entry_count * 4
        if fato_size != expected_fato_size:
            result.add(
                "error",
                "fato_size",
                label,
                f"OTAF size is {fato_size}, expected {expected_fato_size}",
            )
        fato_end = fato_start + fato_size
        if fato_end > len(data):
            raise ValueError("OTAF extends past the file")
        fato_offsets = [
            read_u32(data, fato_start + 0x0C + index * 4)
            for index in range(entry_count)
        ]

        fatb_start = fato_end
        if data[fatb_start : fatb_start + 4] != b"BTAF":
            raise ValueError("missing BTAF")
        fatb_size = read_u32(data, fatb_start + 4)
        declared_file_count = read_u32(data, fatb_start + 8)
        fatb_end = fatb_start + fatb_size
        if fatb_end > len(data):
            raise ValueError("BTAF extends past the file")

        position = fatb_start + 0x0C
        record_base = position
        actual_file_count = 0
        intervals: list[tuple[int, int, int, int, int]] = []
        largest_payload = 0

        for entry_index in range(entry_count):
            expected_offset = position - record_base
            if entry_index >= len(fato_offsets):
                raise ValueError("OTAF entry table is truncated")
            if fato_offsets[entry_index] != expected_offset:
                result.add(
                    "error",
                    "fato_entry_offset",
                    f"GARC entry {entry_index}",
                    f"OTAF offset is {fato_offsets[entry_index]}, "
                    f"expected {expected_offset}",
                )

            if position + 4 > fatb_end:
                raise ValueError("BTAF vector table is truncated")
            vector = read_u32(data, position)
            position += 4

            for bit in range(32):
                if not (vector & (1 << bit)):
                    continue
                if position + 12 > fatb_end:
                    raise ValueError("BTAF file record is truncated")
                start, end, length = struct.unpack_from("<III", data, position)
                position += 12
                actual_file_count += 1
                largest_payload = max(largest_payload, length)

                location = f"GARC entry {entry_index}, subfile {bit}"
                if start > end:
                    result.add(
                        "error",
                        "garc_record_order",
                        location,
                        f"start {start} is after end {end}",
                    )
                if length > end - start:
                    result.add(
                        "error",
                        "garc_record_length",
                        location,
                        f"payload length {length} exceeds allocation"
                        f" {end - start}",
                    )
                if start % GARC_PADDING:
                    result.add(
                        _validation_severity(strict),
                        "garc_record_alignment",
                        location,
                        f"data start {start} is not {GARC_PADDING}-byte"
                        " aligned",
                    )
                if data_offset + end > len(data):
                    result.add(
                        "error",
                        "garc_record_out_of_bounds",
                        location,
                        "allocated range extends past the archive",
                    )
                    continue

                intervals.append((start, end, length, entry_index, bit))
                padding_start = data_offset + start + length
                padding_end = data_offset + end
                padding = data[padding_start:padding_end]
                if padding and any(value != 0xFF for value in padding):
                    result.add(
                        _validation_severity(strict),
                        "garc_padding_bytes",
                        location,
                        "GARC allocation padding contains bytes other than"
                        " 0xFF",
                    )

        if position != fatb_end:
            result.add(
                "error",
                "fatb_size_mismatch",
                label,
                f"BTAF parser ended at 0x{position:X}, section ends at "
                f"0x{fatb_end:X}",
            )
        if actual_file_count != declared_file_count:
            result.add(
                "error",
                "garc_file_count",
                label,
                f"BTAF declares {declared_file_count} files, found "
                f"{actual_file_count}",
            )

        if data[fatb_end : fatb_end + 4] != b"BMIF":
            raise ValueError("missing BMIF")
        fimb_header_size = read_u32(data, fatb_end + 4)
        fimb_data_size = read_u32(data, fatb_end + 8)
        if fimb_header_size != 0x0C:
            result.add(
                "error",
                "fimb_header_size",
                label,
                f"BMIF header size is {fimb_header_size}, expected 12",
            )
        if data_offset != fatb_end + fimb_header_size:
            result.add(
                "error",
                "garc_data_offset_mismatch",
                label,
                f"data offset is 0x{data_offset:X}, expected "
                f"0x{fatb_end + fimb_header_size:X}",
            )
        if fimb_data_size != len(data) - data_offset:
            result.add(
                "error",
                "fimb_data_size",
                label,
                f"BMIF declares {fimb_data_size} data bytes, found "
                f"{len(data) - data_offset}",
            )
        if declared_largest != largest_payload:
            result.add(
                _validation_severity(strict),
                "garc_largest_file",
                label,
                f"header largest-file value is {declared_largest}, "
                f"actual largest payload is {largest_payload}",
            )

        intervals.sort()
        previous_end = 0
        for start, end, _length, entry_index, bit in intervals:
            if start < previous_end:
                result.add(
                    "error",
                    "garc_file_overlap",
                    f"GARC entry {entry_index}, subfile {bit}",
                    f"allocation starts at {start}, before prior end "
                    f"{previous_end}",
                )
            previous_end = max(previous_end, end)

        if intervals and intervals[-1][1] != len(data) - data_offset:
            result.add(
                _validation_severity(strict),
                "garc_trailing_allocation",
                label,
                f"last allocation ends at {intervals[-1][1]}, data section "
                f"has {len(data) - data_offset} bytes",
            )
    except Exception as exc:
        result.add(
            "error",
            "garc_layout_parse_failed",
            label,
            f"{type(exc).__name__}: {exc}",
        )

    return archive


def _inspect_line_controls(
    words: list[int],
    result: ValidationResult,
    *,
    location: str,
    strict: bool,
) -> int | None:
    """
    Validate control-code boundaries and return the real terminator index.
    """
    if not words:
        result.add(
            "error", "empty_line_record", location, "line has zero words"
        )
        return None

    index = 0
    while index < len(words):
        value = words[index]

        if value == TEXT_TERMINATOR:
            return index

        if value in (TEXT_NULL, TEXT_WAIT):
            result.add(
                _validation_severity(strict),
                "orphan_control_identifier",
                location,
                f"reserved control identifier 0x{value:04X} appears outside "
                "a text variable",
            )
            index += 1
            continue

        if value != TEXT_VARIABLE:
            index += 1
            continue

        if index + 2 >= len(words):
            result.add(
                "error",
                "truncated_text_variable",
                location,
                f"text variable starts at word {index} but lacks its header",
            )
            return None

        count = words[index + 1]
        variable = words[index + 2]
        result.variables_checked += 1

        if variable in (TEXT_NULL, TEXT_WAIT):
            if index + 3 >= len(words):
                result.add(
                    "error",
                    "truncated_special_control",
                    location,
                    f"special control 0x{variable:04X} lacks its argument",
                )
                return None
            if count != 2:
                result.add(
                    _validation_severity(strict),
                    "special_control_count",
                    location,
                    f"special control 0x{variable:04X} has count {count}, "
                    "expected 2",
                )
            index += 4
            continue

        if count < 1:
            result.add(
                "error",
                "invalid_variable_count",
                location,
                f"variable 0x{variable:04X} has invalid count {count}",
            )
            return None

        step = 2 + count
        if index + step > len(words):
            result.add(
                "error",
                "variable_out_of_bounds",
                location,
                f"variable 0x{variable:04X} extends past the line",
            )
            return None
        index += step

    result.add(
        "error", "missing_terminator", location, "line has no terminator"
    )
    return None


def validate_text_payload(
    payload: bytes,
    result: ValidationResult,
    *,
    file_index: int,
    reference_lines: list[ReferenceLine] | None,
    strict: bool,
) -> None:
    """Deep-check one encrypted Game Text subfile."""
    file_label = f"{file_index:03d}.bin"
    result.files_checked += 1

    if len(payload) < 20:
        result.add(
            "error",
            "text_file_too_small",
            file_label,
            "file is under 20 bytes",
        )
        return
    if len(payload) % 4:
        result.add(
            _validation_severity(strict),
            "text_file_alignment",
            file_label,
            f"file size {len(payload)} is not divisible by 4",
        )

    try:
        section_count, line_count = struct.unpack_from("<HH", payload, 0)
        total_length, initial_key, section_offset = struct.unpack_from(
            "<III", payload, 4
        )
    except struct.error as exc:
        result.add("error", "text_header_truncated", file_label, str(exc))
        return

    if section_count != 1:
        result.add(
            "error",
            "text_section_count",
            file_label,
            f"section count is {section_count}, expected 1",
        )
    if initial_key != 0:
        result.add(
            "error",
            "text_initial_key",
            file_label,
            f"initial key is 0x{initial_key:08X}, expected 0",
        )
    if section_offset != 0x10:
        result.add(
            "error",
            "text_section_offset",
            file_label,
            f"section offset is 0x{section_offset:X}, expected 0x10",
        )
    if section_offset > len(payload) - 4:
        result.add(
            "error",
            "text_section_out_of_bounds",
            file_label,
            "section offset is outside the file",
        )
        return
    if section_offset + total_length != len(payload):
        result.add(
            "error",
            "text_total_length",
            file_label,
            f"header total {total_length} does not match file size "
            f"{len(payload)}",
        )

    section_length = read_u32(payload, section_offset)
    if section_length != total_length:
        result.add(
            "error",
            "text_section_length",
            file_label,
            f"section length {section_length} differs from total "
            f"{total_length}",
        )
    if section_length % 4:
        result.add(
            _validation_severity(strict),
            "text_section_alignment",
            file_label,
            f"section size {section_length} is not divisible by 4",
        )

    table_end = section_offset + 4 + line_count * 8
    if table_end > len(payload):
        result.add(
            "error",
            "line_table_out_of_bounds",
            file_label,
            "line table extends past the file",
        )
        return

    parsed_lines: list[TextLine] = []
    intervals: list[tuple[int, int, int]] = []

    for line_index in range(line_count):
        location = f"{file_label} entry {line_index}"
        record_offset = section_offset + 4 + line_index * 8
        try:
            relative_offset, length_words, attribute = struct.unpack_from(
                "<IHH", payload, record_offset
            )
        except struct.error as exc:
            result.add("error", "line_record_truncated", location, str(exc))
            continue

        absolute_offset = section_offset + relative_offset
        absolute_end = absolute_offset + length_words * 2

        if length_words == 0:
            result.add(
                "error", "zero_length_line", location, "line length is zero"
            )
            continue
        if relative_offset % 4:
            result.add(
                _validation_severity(strict),
                "line_offset_alignment",
                location,
                f"relative offset 0x{relative_offset:X} is not 4-byte aligned",
            )
        if absolute_offset < table_end:
            result.add(
                "error",
                "line_points_into_table",
                location,
                f"line starts at 0x{absolute_offset:X}, before data area "
                f"0x{table_end:X}",
            )
            continue
        if absolute_end > len(payload):
            result.add(
                "error",
                "line_out_of_bounds",
                location,
                f"line ends at 0x{absolute_end:X}, file ends at "
                f"0x{len(payload):X}",
            )
            continue

        intervals.append((absolute_offset, absolute_end, line_index))
        encrypted = payload[absolute_offset:absolute_end]
        try:
            decrypted = crypt_line(encrypted, line_key(line_index))
            words = list(struct.unpack("<" + "H" * length_words, decrypted))
        except Exception as exc:
            result.add(
                "error",
                "line_decryption_failed",
                location,
                f"{type(exc).__name__}: {exc}",
            )
            continue

        result.lines_checked += 1
        result.largest_line_words = max(
            result.largest_line_words,
            len(words),
        )

        terminator = _inspect_line_controls(
            words,
            result,
            location=location,
            strict=strict,
        )
        parsed_lines.append(TextLine(attribute, words))

        if terminator is not None:
            tail = words[terminator + 1 :]
            if any(value != 0 for value in tail):
                result.add(
                    _validation_severity(strict),
                    "nonzero_post_terminator_data",
                    location,
                    "decrypted words after the terminator are not all zero",
                )

    intervals.sort()
    previous_end = table_end
    for start, end, line_index in intervals:
        if start < previous_end:
            result.add(
                "error",
                "line_data_overlap",
                f"{file_label} entry {line_index}",
                f"line starts at 0x{start:X}, before prior end "
                f"0x{previous_end:X}",
            )
        if start > previous_end:
            gap = payload[previous_end:start]
            if any(gap):
                result.add(
                    _validation_severity(strict),
                    "nonzero_line_gap",
                    file_label,
                    f"gap 0x{previous_end:X}..0x{start:X} contains "
                    "nonzero bytes",
                )
        previous_end = max(previous_end, end)

    if previous_end < len(payload):
        trailing = payload[previous_end:]
        if any(trailing):
            result.add(
                _validation_severity(strict),
                "nonzero_text_trailing_padding",
                file_label,
                "bytes after the final line are not zero padding",
            )

    if len(parsed_lines) != line_count:
        return
    if reference_lines is None:
        return

    try:
        mapping = build_reference_mapping(parsed_lines, reference_lines)
    except Exception as exc:
        result.add(
            "error",
            "reference_alignment_failed",
            file_label,
            f"{type(exc).__name__}: {exc}",
        )
        return

    for current_index, current_line in enumerate(parsed_lines):
        location = f"{file_label} entry {current_index}"
        try:
            terminator = find_text_terminator(current_line.words)
        except Exception:
            continue
        current_tail = current_line.words[terminator + 1 :]

        if current_index in mapping:
            reference = reference_lines[mapping[current_index]]
            if current_line.attribute != reference.attribute:
                result.add(
                    _validation_severity(strict),
                    "metadata_attribute_mismatch",
                    location,
                    f"attribute is 0x{current_line.attribute:04X}, expected "
                    f"0x{reference.attribute:04X}",
                )
            expected_tail = [0] * reference.fixed_tail_zeros
            if current_tail != expected_tail:
                result.add(
                    _validation_severity(strict),
                    "metadata_tail_mismatch",
                    location,
                    f"post-terminator tail has {len(current_tail)} words, "
                    f"expected {reference.fixed_tail_zeros} zero words",
                )
        elif strict and any(current_tail):
            result.add(
                "error",
                "new_entry_tail_not_neutral",
                location,
                "new entry has nonzero post-terminator data",
            )


def visible_text_snapshot(
    garc_data: bytes,
) -> list[list[tuple[int, ...]]]:
    """
    Return canonical visible words for an archive-wide preservation check.
    """
    archive = unpack_garc(garc_data)
    snapshot: list[list[tuple[int, ...]]] = []
    for file_index, entry in enumerate(archive.entries):
        if len(entry) != 1 or entry[0].index != 0:
            raise ValueError(
                f"entry {file_index:03d} is not a single normal subfile"
            )
        lines = parse_text_file(
            entry[0].payload,
            label=f"{file_index:03d}.bin snapshot",
        )
        snapshot.append(
            [tuple(canonical_visible_words(line.words)[0]) for line in lines]
        )
    return snapshot


def validate_game_text_garc(
    garc_data: bytes,
    *,
    phase: str,
    reference_files: list[list[ReferenceLine]] | None,
    strict: bool,
) -> ValidationResult:
    result = ValidationResult(phase)
    archive = validate_garc_container(
        garc_data,
        result,
        strict=strict,
    )
    if archive is None:
        return result

    if len(archive.entries) != EXPECTED_TOP_LEVEL_ENTRIES:
        result.add(
            "error",
            "top_level_entry_count",
            "a/0/7/4",
            f"archive has {len(archive.entries)} entries, expected "
            f"{EXPECTED_TOP_LEVEL_ENTRIES}",
        )
        return result

    if reference_files is not None and (
        len(reference_files) != EXPECTED_TOP_LEVEL_ENTRIES
    ):
        result.add(
            "error",
            "reference_file_count",
            "metadata reference",
            f"reference has {len(reference_files)} files",
        )
        return result

    for file_index, entry in enumerate(archive.entries):
        if len(entry) != 1 or entry[0].index != 0:
            result.add(
                "error",
                "unexpected_subfile_layout",
                f"GARC entry {file_index}",
                "expected exactly one subfile at index 0",
            )
            continue
        payload = entry[0].payload
        if payload[:1] == b"\x11":
            result.add(
                "error",
                "unexpected_lz11",
                f"{file_index:03d}.bin",
                "Game Text subfile is unexpectedly LZ11-compressed",
            )
            continue
        validate_text_payload(
            payload,
            result,
            file_index=file_index,
            reference_lines=(
                reference_files[file_index]
                if reference_files is not None
                else None
            ),
            strict=strict,
        )

    return result


def assert_validation_safe(
    result: ValidationResult,
    *,
    fail_on_warnings: bool = False,
) -> None:
    if result.error_count:
        raise ValueError(
            f"{result.phase} validation found {result.error_count} fatal "
            "issue(s); the working GARC was not replaced"
        )
    if fail_on_warnings and result.warning_count:
        raise ValueError(
            f"{result.phase} validation found {result.warning_count} "
            "warning(s) in strict warning mode; the working GARC was not "
            "replaced"
        )


def validate_archive_shape(
    archive: GarcArchive,
    blueprint: list[list[ReferenceLine]],
) -> list[int]:
    if len(archive.entries) != EXPECTED_TOP_LEVEL_ENTRIES:
        raise ValueError(
            f"target has {len(archive.entries)} top-level entries, expected "
            f"{EXPECTED_TOP_LEVEL_ENTRIES}"
        )

    differing_counts: list[int] = []

    for file_index, entry in enumerate(archive.entries):
        if len(entry) != 1 or entry[0].index != 0:
            raise ValueError(
                f"entry {file_index:03d} is not a single normal subfile"
            )

        payload = entry[0].payload
        if payload[:1] == b"\x11":
            raise ValueError(
                f"entry {file_index:03d} is unexpectedly LZ11-compressed"
            )

        lines = parse_text_file(payload, label=f"{file_index:03d}.bin")
        if len(lines) != len(blueprint[file_index]):
            differing_counts.append(file_index)

    return differing_counts


def repair_archive(
    original_garc: bytes,
    blueprint: list[list[ReferenceLine]],
    reference_files: list[list[ReferenceLine]] | None = None,
) -> tuple[bytes, dict[str, object]]:
    archive = unpack_garc(original_garc)
    differing_counts = validate_archive_shape(archive, blueprint)

    if reference_files is None:
        reference_files = blueprint
        reference_source = "embedded vanilla blueprint"
    else:
        if len(reference_files) != EXPECTED_TOP_LEVEL_ENTRIES:
            raise ValueError(
                "persistent metadata state has the wrong file count"
            )
        reference_source = "persistent custom-entry metadata state"

    totals = {
        "text_files": 0,
        "lines": 0,
        "mapped_lines": 0,
        "unmapped_lines": 0,
        "new_entries_inferred": 0,
        "attributes_restored": 0,
        "special_controls_repaired": 0,
        "line_padding_repaired": 0,
        "fixed_width_lines_restored": 0,
        "changed_text_subfiles": 0,
        "line_count_differences": differing_counts,
        "reference_source": reference_source,
    }

    for file_index, entry in enumerate(archive.entries):
        original_text = entry[0].payload
        repaired_text, stats = rebuild_text_file(
            original_text,
            reference_files[file_index],
            label=f"{file_index:03d}.bin",
        )

        totals["text_files"] += 1
        for key in (
            "lines",
            "mapped_lines",
            "unmapped_lines",
            "new_entries_inferred",
            "attributes_restored",
            "special_controls_repaired",
            "line_padding_repaired",
            "fixed_width_lines_restored",
        ):
            totals[key] += stats[key]

        if repaired_text != original_text:
            totals["changed_text_subfiles"] += 1
            entry[0].payload = repaired_text

    repaired_garc = pack_garc(archive)

    check_archive = unpack_garc(repaired_garc)
    validate_archive_shape(check_archive, blueprint)

    antidote_targets = {
        96: list(b"Antidote") + [0],
        97: list(b"Antidotes") + [0],
        98: list(b"Antidote") + [0],
    }
    for file_index in (96, 97, 98):
        lines = parse_text_file(
            check_archive.entries[file_index][0].payload,
            label=f"{file_index:03d}.bin verification",
        )
        matches = []
        for entry_index, line in enumerate(lines):
            visible, _ = canonical_visible_words(line.words)
            if visible == antidote_targets[file_index]:
                matches.append((entry_index, line.attribute))
        if not matches or any(attribute != 0x0004 for _, attribute in matches):
            raise ValueError(
                "Antidote grammar attribute was not restored in "
                f"{file_index:03d}.bin"
            )

    return repaired_garc, totals


def write_report(
    report_path: Path,
    source_path: Path,
    output_path: Path,
    backup_path: Path | None,
    state_path: Path,
    state_load_error: str | None,
    prior_state_hash: str | None,
    original_data: bytes,
    repaired_data: bytes,
    totals: dict[str, object],
    pre_validation: ValidationResult | None,
    post_validation: ValidationResult | None,
) -> None:
    count_differences = totals["line_count_differences"]
    count_text = (
        ", ".join(f"{index:03d}" for index in count_differences)
        if count_differences
        else "none"
    )

    def validation_report_lines(
        validation: ValidationResult | None,
    ) -> list[str]:
        if validation is None:
            return ["Validation disabled."]

        output = [
            f"Phase: {validation.phase}",
            f"Fatal errors: {validation.error_count}",
            f"Warnings: {validation.warning_count}",
            f"GARC entries checked: {validation.garc_entries_checked}",
            f"Text subfiles checked: {validation.files_checked}",
            f"Text lines checked: {validation.lines_checked}",
            f"Text variables checked: {validation.variables_checked}",
            f"Largest line: {validation.largest_line_words} words",
        ]

        if validation.counts:
            output.append("Issue counts:")
            for key in sorted(validation.counts):
                output.append(f"  {key}: {validation.counts[key]}")
        else:
            output.append("Issue counts: none")

        if validation.issues:
            output.append(f"Examples, capped at {MAX_VALIDATION_EXAMPLES}:")
            for issue in validation.issues:
                output.append(
                    f"  [{issue.severity.upper()}] {issue.code} | "
                    f"{issue.location} | {issue.message}"
                )
        else:
            output.append("Examples: none")
        return output

    lines = [
        "pk3DS a/0/7/4 text repair",
        "=" * 72,
        f"Source: {source_path}",
        f"Output: {output_path}",
        f"Backup: {backup_path if backup_path else 'disabled'}",
        f"Metadata state: {state_path}",
        f"Metadata reference: {totals['reference_source']}",
        f"Prior state GARC SHA-256: {prior_state_hash or 'none'}",
        f"State load warning: {state_load_error or 'none'}",
        f"Original size: {len(original_data)}",
        f"Repaired size: {len(repaired_data)}",
        f"Original SHA-256: {sha256(original_data)}",
        f"Repaired SHA-256: {sha256(repaired_data)}",
        "",
        f"Text subfiles checked: {totals['text_files']}",
        (
            "Text subfiles rebuilt differently: "
            f"{totals['changed_text_subfiles']}"
        ),
        f"Visible lines preserved: {totals['lines']}",
        f"Reference-mapped lines: {totals['mapped_lines']}",
        f"New or unmapped lines preserved: {totals['unmapped_lines']}",
        (
            "New lines inheriting safe neighbour metadata: "
            f"{totals['new_entries_inferred']}"
        ),
        f"Line attributes restored: {totals['attributes_restored']}",
        (
            "Malformed special controls repaired: "
            f"{totals['special_controls_repaired']}"
        ),
        (
            "Incorrect in-line padding records repaired: "
            f"{totals['line_padding_repaired']}"
        ),
        (
            "Fixed-width internal lines restored: "
            f"{totals['fixed_width_lines_restored']}"
        ),
        f"Files with non-vanilla line counts: {count_text}",
        "",
        "Pre-repair validation:",
        *validation_report_lines(pre_validation),
        "",
        "Post-repair validation:",
        *validation_report_lines(post_validation),
        "",
        "Verification:",
        "  Editor-visible dialogue remained byte-equivalent after decryption.",
        "  Antidote attribute 0x0004 is present in files 096, 097, and 098.",
        "  The rebuilt GARC passed full container and text validation.",
        "  Every text variable was checked for valid boundaries and counts.",
        (
            "  Every line offset, allocation, terminator, tail, and padding"
            " area was checked."
        ),
        (
            "  A new custom-entry metadata state was written in the repair"
            " reports folder."
        ),
    ]

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def repair_file(source_path: Path, report_folder: Path | None = None) -> None:
    source_path = Path(source_path).resolve()
    blueprint = load_blueprint()
    original_data = source_path.read_bytes()

    state_path = metadata_state_path(source_path, report_folder)

    state_load_path = state_path
    legacy_state_path = legacy_metadata_state_path(source_path)
    loaded_legacy_state = False

    if (
        not state_path.is_file()
        and legacy_state_path.is_file()
        and legacy_state_path.resolve() != state_path.resolve()
    ):
        state_load_path = legacy_state_path
        loaded_legacy_state = True

    state_reference, prior_state_hash, state_load_error = load_metadata_state(
        state_load_path
    )

    if loaded_legacy_state and state_reference is not None:
        print(
            "Loaded legacy metadata state from "
            f"{state_load_path}. The refreshed state will be written to "
            f"{state_path}."
        )

    active_reference = (
        state_reference if state_reference is not None else blueprint
    )

    pre_validation: ValidationResult | None = None
    post_validation: ValidationResult | None = None
    original_visible = visible_text_snapshot(original_data)

    if RUN_DEEP_VALIDATION:
        pre_validation = validate_game_text_garc(
            original_data,
            phase="before repair",
            reference_files=active_reference,
            strict=False,
        )
        assert_validation_safe(pre_validation)

    repaired_data, totals = repair_archive(
        original_data,
        blueprint,
        state_reference,
    )

    repaired_visible = visible_text_snapshot(repaired_data)
    if repaired_visible != original_visible:
        raise ValueError(
            "archive-wide validation found an editor-visible text change; "
            "the working GARC was not replaced"
        )

    if RUN_DEEP_VALIDATION:
        post_validation = validate_game_text_garc(
            repaired_data,
            phase="after repair",
            reference_files=active_reference,
            strict=True,
        )
        assert_validation_safe(
            post_validation,
            fail_on_warnings=FAIL_ON_POST_VALIDATION_WARNINGS,
        )

    if REPLACE_WORKING_GARC:
        output_path = source_path
    else:
        output_path = source_path.with_name(source_path.name + OUTPUT_SUFFIX)

    temporary_path = output_path.with_name(output_path.name + ".repairing")
    if temporary_path.exists():
        temporary_path.unlink()

    temporary_path.write_bytes(repaired_data)

    on_disk = temporary_path.read_bytes()
    if on_disk != repaired_data:
        temporary_path.unlink(missing_ok=True)
        raise IOError("temporary repaired GARC failed its write verification")

    backup_path: Path | None = None
    if REPLACE_WORKING_GARC:
        if MAKE_BACKUP:
            backup_path = timestamped_backup_path(source_path, report_folder)
            shutil.copy2(source_path, backup_path)
        os.replace(temporary_path, source_path)
    else:
        os.replace(temporary_path, output_path)

    repaired_snapshot = reference_snapshot_from_garc(repaired_data)
    write_metadata_state(
        state_path,
        repaired_snapshot,
        sha256(repaired_data),
    )

    report_path = script_directory(report_folder) / REPORT_NAME
    write_report(
        report_path,
        source_path,
        output_path,
        backup_path,
        state_path,
        state_load_error,
        prior_state_hash,
        original_data,
        repaired_data,
        totals,
        pre_validation,
        post_validation,
    )

    print("a/0/7/4 repair complete.")
    print(f"Working file: {output_path}")
    if backup_path:
        print(f"Backup: {backup_path}")
    print(f"Metadata state: {state_path}")
    print(f"Reference used: {totals['reference_source']}")
    if state_load_error:
        print(f"State warning: {state_load_error}")
    print(f"Report: {report_path}")
    if pre_validation is not None:
        print(
            "Pre-repair validation: "
            f"{pre_validation.error_count} errors, "
            f"{pre_validation.warning_count} warnings"
        )
    if post_validation is not None:
        print(
            "Post-repair validation: "
            f"{post_validation.error_count} errors, "
            f"{post_validation.warning_count} warnings"
        )
    print(f"Text files changed: {totals['changed_text_subfiles']}")
    print(f"Attributes restored: {totals['attributes_restored']}")
    print(f"Special controls repaired: {totals['special_controls_repaired']}")
    print(
        "Incorrect in-line padding records repaired: "
        f"{totals['line_padding_repaired']}"
    )
    print(
        "Fixed-width internal lines restored: "
        f"{totals['fixed_width_lines_restored']}"
    )
    print(f"Visible dialogue lines preserved: {totals['lines']}")
    print(f"New or unmapped lines: {totals['unmapped_lines']}")
