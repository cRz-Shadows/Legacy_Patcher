"""Makerom RomFS section handling."""

from __future__ import annotations

import re


def makerom_rsf_with_romfs_root(text: str, root_path: str = "romfs") -> str:
    """Return an RSF that makes makerom build RomFS from ``root_path``.

    Existing RomFs settings are retained. Only RootPath is inserted or
    replaced. A block-style RomFs section is required if the section already
    exists; this is the format produced by standard makerom RSF files.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    romfs_index = None

    for index, line in enumerate(lines):
        if re.match(r"^RomFs\s*:\s*(?:#.*)?$", line):
            romfs_index = index
            break
        if re.match(r"^RomFs\s*:", line):
            raise RuntimeError(
                "The RSF uses an inline RomFs mapping. Convert it to a block "
                "section before building the patch set."
            )

    if romfs_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(("RomFs:", "  RootPath: %s" % root_path))
        return newline.join(lines) + newline

    section_end = len(lines)
    for index in range(romfs_index + 1, len(lines)):
        line = lines[index]
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            section_end = index
            break

    root_index = None
    indentation = "  "
    indentation_found = False
    for index in range(romfs_index + 1, section_end):
        line = lines[index]
        match = re.match(r"^(\s+)RootPath\s*:", line)
        if match:
            root_index = index
            indentation = match.group(1)
            break
        content = re.match(r"^(\s+)[^#\s]", line)
        if content and not indentation_found:
            indentation = content.group(1)
            indentation_found = True

    root_line = "%sRootPath: %s" % (indentation, root_path)
    if root_index is None:
        lines.insert(romfs_index + 1, root_line)
    else:
        lines[root_index] = root_line
    return newline.join(lines) + newline


def write_romfs_rsf(source, destination, *, include_romfs: bool = True):
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError("RSF must be UTF-8 text: %s" % source) from exc
    root_path = "romfs" if include_romfs else '""'
    with destination.open("w", encoding="utf-8", newline="") as output:
        output.write(makerom_rsf_with_romfs_root(text, root_path))
