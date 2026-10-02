"""Application title IDs, permissions, and packaged metadata."""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
from pathlib import Path

from .config import (
    CRR_RELPATH,
    EXHEADER_EXTDATA_ID_OFFSET,
    EXHEADER_JUMP_ID_OFFSET,
    EXHEADER_PROGRAM_ID_OFFSET,
    EXHEADER_RELPATH,
    EXHEADER_SAVE_SIZE_OFFSET,
    EXHEADER_SD_APPLICATION_FLAG_OFFSET,
    EXHEADER_SIGNED_DESCRIPTOR_OFFSET,
    EXHEADER_SIGNED_PROGRAM_ID_OFFSET,
    EXHEADER_SIZE,
    HARDWARE_IDENTITY_FORMAT,
    ICON_BIN_RELPATH,
    SMDH_ENGLISH_TITLE_INDEX,
    SMDH_LONG_NAME_SIZE,
    SMDH_SHORT_NAME_SIZE,
    SMDH_SIZE,
    SMDH_TITLE_COUNT,
    SMDH_TITLE_ENTRY_SIZE,
    SMDH_TITLES_OFFSET,
    TITLE_ID_MIDDLE_LENGTH,
    TITLE_ID_PREFIX,
    TITLE_ID_SUFFIX,
    TITLE_ID_UNIQUE_MASK,
    XY_GAMES,
    GameIdentity,
)
from .names import make_patchset_name, validate_game_name
from .rsf import makerom_rsf_with_romfs_root


def title_unique_id(title_id: int) -> int:
    return (int(title_id) >> 8) & 0xFFFFF


def title_id_with_unique_id(title_id: int, unique_id: int) -> int:
    if not 0 <= unique_id <= 0xFFFFF:
        raise ValueError("3DS application UniqueId is outside the 20-bit range")
    return (int(title_id) & ~TITLE_ID_UNIQUE_MASK) | (unique_id << 8)


def signed_descriptor_allows_program_id(
    descriptor_id: int, program_id: int
) -> bool:
    """
    In the Access Descriptor, a 0xFF byte permits any value at that byte.
    """
    signed = descriptor_id.to_bytes(8, "little")
    requested = program_id.to_bytes(8, "little")
    return all(a == 0xFF or a == b for a, b in zip(signed, requested))


def parse_title_id(value: str) -> int:
    digits = value.removeprefix("0x").removeprefix("0X")
    if re.fullmatch(r"[0-9a-fA-F]{16}", digits) is None:
        raise ValueError("Use a 16-digit hexadecimal application title ID.")
    title_id = int(digits, 16)
    if title_id & ~TITLE_ID_UNIQUE_MASK != 0x0004000000000000:
        raise ValueError(
            "Use an application title ID beginning with 000400000 "
            "and ending with 00."
        )
    return title_id


def parse_title_id_middle(value: str) -> int:
    """Expand the editable middle digits into a full application title ID."""
    digits = value.strip()
    if (
        re.fullmatch(rf"[0-9a-fA-F]{{{TITLE_ID_MIDDLE_LENGTH}}}", digits)
        is None
    ):
        raise ValueError(
            f"Enter exactly {TITLE_ID_MIDDLE_LENGTH} hexadecimal digits "
            "using 0-9 or A-F."
        )
    return parse_title_id(f"{TITLE_ID_PREFIX}{digits}{TITLE_ID_SUFFIX}")


def read_exheader(root: Path) -> bytes:
    path = root / EXHEADER_RELPATH
    data = path.read_bytes()
    if len(data) != EXHEADER_SIZE:
        raise ValueError(f"ExHeader must be exactly 0x800 bytes: {path.name}")
    return data


def validate_requested_title_id(exheader: bytes, title_id: int) -> None:
    parse_title_id(f"{title_id:016X}")
    original_id = struct.unpack_from(
        "<Q", exheader, EXHEADER_PROGRAM_ID_OFFSET
    )[0]
    if original_id == title_id:
        return
    descriptor_id = struct.unpack_from(
        "<Q", exheader, EXHEADER_SIGNED_PROGRAM_ID_OFFSET
    )[0]
    if not signed_descriptor_allows_program_id(descriptor_id, title_id):
        raise ValueError(
            "The base ROM's signed Access Descriptor does not permit "
            f"title ID {title_id:016X}. Choose a permitted ID."
        )


def detect_game_identity(
    clean_base_root: Path,
    rsf: Path,
    *,
    title_id: int | None = None,
    display_name: str | None = None,
) -> GameIdentity:
    data = read_exheader(clean_base_root)
    program_id = struct.unpack_from("<Q", data, EXHEADER_PROGRAM_ID_OFFSET)[0]
    parse_title_id(f"{program_id:016X}")
    target_id = program_id if title_id is None else title_id
    parse_title_id(f"{target_id:016X}")
    source_unique_id = title_unique_id(program_id)
    game_name = XY_GAMES.get(source_unique_id, rsf.stem)
    name = validate_game_name(display_name) if display_name is not None else ""
    patch_name = make_patchset_name(
        game_name,
        name,
        title_unique_id(target_id),
        changes_title_id=target_id != program_id,
    )
    identity = GameIdentity(
        game_name=game_name,
        source_unique_id=source_unique_id,
        target_unique_id=title_unique_id(target_id),
        display_name=name,
        patchset_dir_name=patch_name,
    )
    if identity.changes_title_id:
        validate_requested_title_id(data, target_id)
    return identity


def restore_base_identity(root: Path, base: Path, rsf: Path) -> None:
    """Make explicit choices independent of earlier edits to game metadata."""
    original = read_exheader(base)
    exheader = ensure_temp_patch_input(root, base, EXHEADER_RELPATH)
    data = bytearray(read_exheader(root))
    for offset in (EXHEADER_PROGRAM_ID_OFFSET, EXHEADER_JUMP_ID_OFFSET):
        data[offset : offset + 8] = original[offset : offset + 8]
    descriptor = EXHEADER_SIGNED_DESCRIPTOR_OFFSET
    data[descriptor:] = original[descriptor:]
    flag = EXHEADER_SD_APPLICATION_FLAG_OFFSET
    data[flag] = (data[flag] & ~0x02) | (original[flag] & 0x02)
    exheader.write_bytes(data)

    original_id = struct.unpack_from(
        "<Q", original, EXHEADER_PROGRAM_ID_OFFSET
    )[0]
    text = rsf.read_text(encoding="utf-8-sig")
    unique_id = _rsf_scalar(text, "TitleInfo", "UniqueId")
    if unique_id is None:
        raise ValueError("The RSF needs TitleInfo.UniqueId.")
    current_id = int(unique_id.rstrip("Ll"), 0)
    if not 0 <= current_id <= 0xFFFFF:
        raise ValueError("The RSF UniqueId is outside the 20-bit range.")
    base_unique_id = title_unique_id(original_id)
    if current_id != base_unique_id:
        text = _replace_rsf_numeric_value(
            text,
            "TitleInfo",
            "UniqueId",
            {current_id},
            f"0x{base_unique_id:08x}",
        )
    jump = _rsf_scalar(text, "SystemControlInfo", "JumpId")
    base_jump = struct.unpack_from("<Q", original, EXHEADER_JUMP_ID_OFFSET)[0]
    if jump is not None and int(jump.rstrip("Ll"), 0) != base_jump:
        text = _set_rsf_scalar(
            text, "SystemControlInfo", "JumpId", f"0x{base_jump:016x}L"
        )
    sd_application = bool(original[flag] & 0x02)
    text = _set_rsf_option(
        text, "UseOnSD", "true" if sd_application else "false"
    )
    with rsf.open("w", encoding="utf-8", newline="") as output:
        output.write(text)

    crr = root / CRR_RELPATH
    if crr.is_file() and (base / CRR_RELPATH).is_file():
        content = bytearray(crr.read_bytes())
        if len(content) < 0x360 or content[:4] != b"CRR0":
            raise ValueError("The updated static.crr is not a CRR0 file.")
        struct.pack_into("<I", content, 0x340, base_unique_id << 8)
        crr.write_bytes(content)

    icon = root / ICON_BIN_RELPATH
    base_icon = base / ICON_BIN_RELPATH
    if icon.is_file() and base_icon.is_file():
        content = bytearray(read_smdh(icon))
        original_icon = read_smdh(base_icon)
        title_size = SMDH_SHORT_NAME_SIZE + SMDH_LONG_NAME_SIZE
        for index in range(SMDH_TITLE_COUNT):
            offset = SMDH_TITLES_OFFSET + index * SMDH_TITLE_ENTRY_SIZE
            content[offset : offset + title_size] = original_icon[
                offset : offset + title_size
            ]
        icon.write_bytes(content)


def ensure_temp_patch_input(
    root_a_temp: Path,
    clean_base_root: Path,
    relpath: str,
) -> Path:
    """Seed a missing metadata file in the temporary updated tree."""
    target = root_a_temp / relpath
    if target.is_file():
        return target
    source = clean_base_root / relpath
    if not source.is_file():
        raise RuntimeError("Clean base extraction is missing: %s" % relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def _replace_exheader_title_id(
    data: bytearray,
    offset: int,
    identity: GameIdentity,
    field_name: str,
    *,
    required: bool,
) -> None:
    value = struct.unpack_from("<Q", data, offset)[0]
    if value == 0 and not required:
        return
    unique_id = title_unique_id(value)
    if unique_id not in {
        identity.source_unique_id,
        identity.target_unique_id,
    }:
        raise RuntimeError(
            "%s has unexpected UniqueId 0x%05X in the updated exheader"
            % (field_name, unique_id)
        )
    struct.pack_into(
        "<Q",
        data,
        offset,
        title_id_with_unique_id(value, identity.target_unique_id),
    )


def apply_exheader_identity(
    path: Path, identity: GameIdentity, clean_base_exheader: bytes
) -> None:
    """Update the identity-bearing exheader fields on the temporary copy."""
    data = bytearray(path.read_bytes())
    if len(data) != EXHEADER_SIZE or len(clean_base_exheader) != EXHEADER_SIZE:
        raise RuntimeError(
            "Both exheaders must be exactly 0x800 bytes: %s" % path
        )

    previous_jump = struct.unpack_from("<Q", data, EXHEADER_JUMP_ID_OFFSET)[0]
    if previous_jump and title_unique_id(previous_jump) not in {
        identity.source_unique_id,
        identity.target_unique_id,
    }:
        raise RuntimeError("Updated exheader JumpId refers to another game")
    struct.pack_into(
        "<Q", data, EXHEADER_JUMP_ID_OFFSET, identity.application_title_id
    )

    data[EXHEADER_SD_APPLICATION_FLAG_OFFSET] |= 0x02
    _replace_exheader_title_id(
        data,
        EXHEADER_PROGRAM_ID_OFFSET,
        identity,
        "ProgramId",
        required=True,
    )
    if (
        struct.unpack_from("<Q", data, EXHEADER_PROGRAM_ID_OFFSET)[0]
        != identity.application_title_id
    ):
        raise RuntimeError(
            "Unexpected application category in the updated exheader"
        )

    descriptor_start = EXHEADER_SIGNED_DESCRIPTOR_OFFSET
    if data[descriptor_start:] != clean_base_exheader[descriptor_start:]:
        data[descriptor_start:] = clean_base_exheader[descriptor_start:]
        print(
            "Restored the clean signed Access Descriptor in the temporary"
            " exheader."
        )
    path.write_bytes(data)


def apply_crr_identity(
    path: Path, identity: GameIdentity, clean_base_path: Path
) -> None:
    """
    Keep the CRO manifest's process identity in step with the application.
    """
    data = bytearray(path.read_bytes())
    clean_base = clean_base_path.read_bytes()
    for name, content in (("updated", data), ("clean base", clean_base)):
        if len(content) < 0x360 or content[:4] != b"CRR0":
            raise RuntimeError("%s static.crr is not a CRR0 file" % name)
        if struct.unpack_from("<I", content, 0x344)[0] != len(content):
            raise RuntimeError("%s static.crr has the wrong file size" % name)
    vanilla_id = identity.source_unique_id << 8
    target_id = identity.target_unique_id << 8
    if struct.unpack_from("<I", clean_base, 0x340)[0] != vanilla_id:
        raise RuntimeError("Clean static.crr belongs to a different game")
    previous_id = struct.unpack_from("<I", data, 0x340)[0]
    if previous_id not in (vanilla_id, target_id):
        raise RuntimeError("Updated static.crr belongs to a different game")
    mask, pattern = struct.unpack_from("<II", data, 0x20)
    if target_id & mask != pattern & mask:
        raise RuntimeError(
            "The signed static.crr Unique ID mask excludes the selected ID"
        )
    struct.pack_into("<I", data, 0x340, target_id)
    path.write_bytes(data)
    print("CRR process ID: 0x%08X -> 0x%08X" % (previous_id, target_id))


def exheader_service_names(exheader: bytes) -> list[str]:
    """List the services needed by the original updated application."""
    names = [
        exheader[offset : offset + 8].split(b"\0", 1)[0].decode("ascii")
        for offset in range(0x250, 0x360, 8)
        if any(exheader[offset : offset + 8])
    ]
    if len(names) != len(set(names)):
        raise RuntimeError("Updated ExHeader repeats a service name")
    return names


def hardware_identity_manifest(
    identity: GameIdentity,
    base_exheader: bytes,
    source_exheader: bytes,
) -> dict:
    """
    Declare the title and save-data identity for the completed ROM.
    """
    if (
        len(base_exheader) != EXHEADER_SIZE
        or len(source_exheader) != EXHEADER_SIZE
    ):
        raise RuntimeError("Both source ExHeaders must be 0x800 bytes")
    return {
        "format": HARDWARE_IDENTITY_FORMAT,
        "game": identity.game_name,
        "vanilla_title_id": (
            "0x%016X" % (0x0004000000000000 | identity.source_unique_id << 8)
        ),
        "application_title_id": "0x%016X" % identity.application_title_id,
        "jump_id": "0x%016X" % identity.application_title_id,
        "save_size": struct.unpack_from(
            "<Q", base_exheader, EXHEADER_SAVE_SIZE_OFFSET
        )[0],
        "extdata_id": struct.unpack_from(
            "<Q", base_exheader, EXHEADER_EXTDATA_ID_OFFSET
        )[0],
        "source_access_sha256": (
            hashlib.sha256(source_exheader[0x208:0x400]).hexdigest()
        ),
        "remaster_version": struct.unpack_from("<H", source_exheader, 0xE)[0],
        "source_required_services": exheader_service_names(source_exheader),
        "output_format": "cci",
        "sd_application": True,
    }


def _smdh_fixed_utf16(text: str, field_size: int) -> bytes:
    encoded = text.encode("utf-16le")
    if len(encoded) + 2 > field_size:
        raise ValueError("SMDH title is too long for its fixed-width field")
    return encoded + bytes(field_size - len(encoded))


def read_smdh(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) != SMDH_SIZE or data[:4] != b"SMDH":
        raise RuntimeError(
            "ExeFS icon is not a valid 0x36C0-byte SMDH: %s" % path
        )
    return data


def read_game_name(root: Path) -> str | None:
    path = root / ICON_BIN_RELPATH
    if not path.is_file():
        return None
    data = path.read_bytes()
    if len(data) != SMDH_SIZE or data[:4] != b"SMDH":
        return None
    languages = [SMDH_ENGLISH_TITLE_INDEX]
    languages.extend(
        index
        for index in range(SMDH_TITLE_COUNT)
        if index != SMDH_ENGLISH_TITLE_INDEX
    )
    for index in languages:
        start = SMDH_TITLES_OFFSET + index * SMDH_TITLE_ENTRY_SIZE
        try:
            name = (
                data[start : start + SMDH_SHORT_NAME_SIZE]
                .decode("utf-16le")
                .split("\0", 1)[0]
                .strip()
            )
        except UnicodeDecodeError:
            continue
        if name:
            return name
    return None


def apply_smdh_identity(path: Path, identity: GameIdentity) -> None:
    """Update every populated SMDH language title."""
    data = bytearray(read_smdh(path))

    short_name = _smdh_fixed_utf16(identity.display_name, SMDH_SHORT_NAME_SIZE)
    long_name = _smdh_fixed_utf16(identity.display_name, SMDH_LONG_NAME_SIZE)
    populated = 0
    for index in range(SMDH_TITLE_COUNT):
        entry = SMDH_TITLES_OFFSET + index * SMDH_TITLE_ENTRY_SIZE
        short_slice = slice(entry, entry + SMDH_SHORT_NAME_SIZE)
        long_start = entry + SMDH_SHORT_NAME_SIZE
        long_slice = slice(long_start, long_start + SMDH_LONG_NAME_SIZE)
        if any(data[short_slice]) or any(data[long_slice]):
            data[short_slice] = short_name
            data[long_slice] = long_name
            populated += 1
    if populated == 0:
        raise RuntimeError(
            "ExeFS icon contains no populated SMDH language titles"
        )
    path.write_bytes(data)


def apply_identity_to_temp_tree(
    root_a_temp: Path,
    clean_base_root: Path,
    identity: GameIdentity,
) -> None:
    """Apply identity edits only to the temporary updated tree."""
    exheader = ensure_temp_patch_input(
        root_a_temp,
        clean_base_root,
        EXHEADER_RELPATH,
    )
    if identity.changes_title_id:
        apply_exheader_identity(
            exheader, identity, read_exheader(clean_base_root)
        )
        if (clean_base_root / CRR_RELPATH).is_file():
            crr = ensure_temp_patch_input(
                root_a_temp, clean_base_root, CRR_RELPATH
            )
            apply_crr_identity(crr, identity, clean_base_root / CRR_RELPATH)
    if identity.display_name:
        icon = ensure_temp_patch_input(
            root_a_temp, clean_base_root, ICON_BIN_RELPATH
        )
        apply_smdh_identity(icon, identity)


def _replace_rsf_numeric_value(
    text: str,
    section_name: str,
    key_name: str,
    allowed_current_values: set[int],
    replacement: str,
) -> str:
    """Replace one numeric scalar inside one top-level RSF section."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    section_index = None
    for index, line in enumerate(lines):
        if re.match(r"^%s\s*:\s*(?:#.*)?$" % re.escape(section_name), line):
            section_index = index
            break
    if section_index is None:
        raise RuntimeError("RSF is missing section: %s" % section_name)

    section_end = len(lines)
    for index in range(section_index + 1, len(lines)):
        line = lines[index]
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            section_end = index
            break

    key_pattern = re.compile(
        r"^(\s*%s\s*:\s*)([^\s#]+)(.*)$" % re.escape(key_name)
    )
    for index in range(section_index + 1, section_end):
        match = key_pattern.match(lines[index])
        if not match:
            continue
        try:
            current = int(match.group(2).rstrip("Ll"), 0)
        except ValueError as exc:
            raise RuntimeError(
                "RSF %s.%s is not a numeric value" % (section_name, key_name)
            ) from exc
        if current not in allowed_current_values:
            raise RuntimeError(
                "RSF %s.%s has unexpected value 0x%X"
                % (section_name, key_name, current)
            )
        lines[index] = match.group(1) + replacement + match.group(3)
        return newline.join(lines) + newline
    raise RuntimeError("RSF is missing key: %s.%s" % (section_name, key_name))


def _rsf_section_range(lines: list[str], section_name: str) -> tuple[int, int]:
    matches = [
        i
        for i, line in enumerate(lines)
        if re.match(r"^%s\s*:\s*(?:#.*)?$" % re.escape(section_name), line)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "RSF must have exactly one block section: %s" % section_name
        )
    begin = matches[0]
    end = next(
        (
            i
            for i in range(begin + 1, len(lines))
            if lines[i]
            and not lines[i][0].isspace()
            and not lines[i].lstrip().startswith("#")
        ),
        len(lines),
    )
    return begin, end


def _rsf_scalar(text: str, section_name: str, key_name: str) -> str | None:
    lines = text.splitlines()
    begin, end = _rsf_section_range(lines, section_name)
    pattern = re.compile(r"^\s+%s\s*:\s*([^\s#]+)" % re.escape(key_name))
    values = [
        m.group(1)
        for line in lines[begin + 1 : end]
        if (m := pattern.match(line))
    ]
    if len(values) > 1:
        raise RuntimeError(
            "RSF has duplicate active key: %s.%s" % (section_name, key_name)
        )
    return values[0] if values else None


def _set_rsf_scalar(
    text: str, section_name: str, key_name: str, value: str
) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    begin, end = _rsf_section_range(lines, section_name)
    pattern = re.compile(r"^(\s+%s\s*:\s*)([^\s#]+)(.*)$" % re.escape(key_name))
    matches = [(i, pattern.match(lines[i])) for i in range(begin + 1, end)]
    matches = [(i, m) for i, m in matches if m]
    if len(matches) > 1:
        raise RuntimeError(
            "RSF has duplicate active key: %s.%s" % (section_name, key_name)
        )
    if matches:
        i, match = matches[0]
        lines[i] = match.group(1) + value + match.group(3)
    else:
        indent = next(
            (
                re.match(r"^(\s+)[^\s#]", line).group(1)
                for line in lines[begin + 1 : end]
                if re.match(r"^(\s+)[^\s#]", line)
            ),
            "  ",
        )
        lines.insert(begin + 1, "%s%s: %s" % (indent, key_name, value))
    return newline.join(lines) + newline


def _set_rsf_option(text: str, key_name: str, value: str) -> str:
    """Set an Option value even when the input RSF lacks an Option section."""
    if not re.search(r"^Option\s*:\s*(?:#.*)?$", text, re.MULTILINE):
        newline = "\r\n" if "\r\n" in text else "\n"
        text = text.rstrip("\r\n") + newline + newline + "Option:" + newline
    return _set_rsf_scalar(text, "Option", key_name, value)


def makerom_rsf_with_required_services(
    text: str, required_services: list[str]
) -> str:
    """Add updated-game service permissions to makerom's RSF."""
    names = list(dict.fromkeys(required_services))
    if (
        len(names) != len(required_services)
        or len(names) > 34
        or any(
            re.fullmatch(r"[\x21-\x7e]{1,8}", name) is None for name in names
        )
    ):
        raise RuntimeError(
            "The updated ExHeader has invalid service permissions"
        )

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    begin, end = _rsf_section_range(lines, "AccessControlInfo")
    headers = [
        (index, match.group(1))
        for index in range(begin + 1, end)
        if (
            match := re.match(
                r"^([ \t]+)ServiceAccessControl\s*:\s*(?:#.*)?$",
                lines[index],
            )
        )
    ]
    if len(headers) > 1:
        raise RuntimeError("RSF has duplicate ServiceAccessControl sections")
    if not headers:
        if any(
            re.match(r"^[ \t]+ServiceAccessControl\s*:", line)
            for line in lines[begin + 1 : end]
        ):
            raise RuntimeError("RSF uses an unsupported inline service list")
        indentation = next(
            (
                match.group(1)
                for line in lines[begin + 1 : end]
                if (match := re.match(r"^([ \t]+)[^\s#-]", line))
            ),
            "  ",
        )
        additions = [indentation + "ServiceAccessControl:"]
        additions.extend(indentation + "  - " + name for name in names)
        lines[begin + 1 : begin + 1] = additions
        return newline.join(lines) + newline

    header, indentation = headers[0]
    values: list[str] = []
    insertion = header + 1
    item_indentation = indentation + "  "
    for index in range(header + 1, end):
        line = lines[index]
        key = re.match(r"^([ \t]+)[^\s#-][^:]*\s*:", line)
        if key and len(key.group(1).expandtabs(8)) <= len(
            indentation.expandtabs(8)
        ):
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.fullmatch(r"([ \t]+)-[ \t]+(\S+)[ \t]*(?:#.*)?", line)
        if item is None:
            raise RuntimeError("RSF ServiceAccessControl is not a service list")
        value = item.group(2)
        if value.startswith(("'", '"')):
            if len(value) < 3 or value[-1] != value[0]:
                raise RuntimeError("RSF has a malformed service name")
            value = value[1:-1]
        if re.fullmatch(r"[\x21-\x7e]{1,8}", value) is None:
            raise RuntimeError("RSF has a malformed service name")
        values.append(value)
        insertion = index + 1
        item_indentation = item.group(1)
    if len(values) != len(set(values)):
        raise RuntimeError("RSF lists a service more than once")
    missing = [name for name in names if name not in values]
    if len(values) + len(missing) > 34:
        raise RuntimeError("RSF exceeds the 34-service ExHeader limit")
    lines[insertion:insertion] = [
        item_indentation + "- " + name for name in missing
    ]
    return newline.join(lines) + newline


def _remove_rsf_scalar(text: str, section_name: str, key_name: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    begin, end = _rsf_section_range(lines, section_name)
    pattern = re.compile(r"^\s+%s\s*:" % re.escape(key_name))
    return (
        newline.join(
            line
            for i, line in enumerate(lines)
            if not (begin < i < end and pattern.match(line))
        )
        + newline
    )


def makerom_rsf_with_identity(
    text: str,
    identity: GameIdentity,
    clean_base_exheader: bytes,
    target_jump: int,
) -> str:
    allowed = {identity.source_unique_id, identity.target_unique_id}
    text = _replace_rsf_numeric_value(
        text,
        "TitleInfo",
        "UniqueId",
        allowed,
        "0x%08x" % identity.target_unique_id,
    )
    for key in ("Category", "TargetCategory"):
        category = _rsf_scalar(text, "TitleInfo", key)
        if category is not None and category != "Application":
            raise RuntimeError("RSF TitleInfo.%s must be Application" % key)

    text = _set_rsf_option(text, "UseOnSD", "true")

    base_jump = struct.unpack_from(
        "<Q", clean_base_exheader, EXHEADER_JUMP_ID_OFFSET
    )[0]
    jump = _rsf_scalar(text, "SystemControlInfo", "JumpId")
    if jump is not None:
        previous_jump = int(jump.rstrip("Ll"), 0)
        if previous_jump not in {
            0,
            base_jump,
            target_jump,
        } and title_unique_id(previous_jump) not in {
            identity.source_unique_id,
            identity.target_unique_id,
        }:
            raise RuntimeError(
                "RSF JumpId is unrelated to %s" % identity.game_name
            )
    text = _set_rsf_scalar(
        text, "SystemControlInfo", "JumpId", "0x%016xL" % target_jump
    )
    extended = _rsf_scalar(
        text, "AccessControlInfo", "UseExtendedSaveDataAccessControl"
    )
    if (extended is not None and extended.casefold() == "true") or _rsf_scalar(
        text, "AccessControlInfo", "AccessibleSaveDataIds"
    ) is not None:
        return text
    base_extdata = struct.unpack_from(
        "<Q", clean_base_exheader, EXHEADER_EXTDATA_ID_OFFSET
    )[0]
    extdata = _rsf_scalar(text, "AccessControlInfo", "ExtSaveDataId")
    if extdata is not None and int(extdata.rstrip("Ll"), 0) not in {
        0,
        identity.source_unique_id,
        identity.target_unique_id,
        base_extdata,
    }:
        raise RuntimeError("RSF ExtSaveDataId has an unexpected existing value")
    if base_extdata:
        text = _set_rsf_scalar(
            text, "AccessControlInfo", "UseExtSaveData", "true"
        )
        text = _set_rsf_scalar(
            text,
            "AccessControlInfo",
            "ExtSaveDataId",
            "0x%08x" % base_extdata,
        )
    else:
        text = _set_rsf_scalar(
            text, "AccessControlInfo", "UseExtSaveData", "false"
        )
        text = _remove_rsf_scalar(text, "AccessControlInfo", "ExtSaveDataId")
    return text


def write_makerom_rsf(
    source: Path,
    destination: Path,
    identity: GameIdentity,
    clean_base_exheader: bytes,
    target_jump: int,
    source_exheader: bytes,
) -> None:
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError("RSF must be UTF-8 text: %s" % source) from exc
    text = makerom_rsf_with_romfs_root(text)
    text = makerom_rsf_with_identity(
        text, identity, clean_base_exheader, target_jump
    )
    source_remaster = struct.unpack_from("<H", source_exheader, 0xE)[0]
    text = _set_rsf_scalar(
        text,
        "SystemControlInfo",
        "RemasterVersion",
        str(source_remaster),
    )
    text = makerom_rsf_with_required_services(
        text, exheader_service_names(source_exheader)
    )
    with destination.open("w", encoding="utf-8", newline="") as output:
        output.write(text)
