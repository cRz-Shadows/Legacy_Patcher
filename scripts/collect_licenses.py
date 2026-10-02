"""Copy notices for bundled components and preserve library rebuild sources."""

from __future__ import annotations

import json
import shutil
import sys
import sysconfig
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyInstaller.archive.readers import CArchiveReader
from rom_patcher.licenses import LICENSE_FILES
from rom_patcher.tool_downloads import download_archive, open_url
from scripts.paths import BUILD_ROOT, PROJECT_ROOT, SCRIPTS_ROOT, source_files


@dataclass
class BundleContents:
    files: set[Path] = field(default_factory=set)
    modules: set[str] = field(default_factory=set)
    binaries: set[str] = field(default_factory=set)


def verify_bundled_licenses(
    executable: str, license_data: list[tuple[str, str, str]]
) -> None:
    archive = CArchiveReader(executable)
    archive.open_embedded_archive("PYZ.pyz")
    stored_names = {name.replace("\\", "/"): name for name in archive.toc}
    for filename, source, kind in license_data:
        stored_name = stored_names.get(filename)
        if stored_name is None:
            raise ValueError(
                f"Bundled license material is missing: {filename}."
            )
        if archive.extract(stored_name) != Path(source).read_bytes():
            raise ValueError(
                f"Bundled license material does not match {filename}."
            )


def read_recorded_files(records, files: set[Path]) -> None:
    if not isinstance(records, (tuple, list)):
        return
    if (
        len(records) == 3
        and isinstance(records[1], str)
        and isinstance(records[2], str)
        and records[2] in {"PYMODULE", "PYSOURCE", "BINARY", "DATA"}
    ):
        path = Path(records[1])
        if path.is_absolute():
            files.add(path.resolve())
        return
    for record in records:
        read_recorded_files(record, files)


def read_analysis_contents(analyses) -> BundleContents:
    contents = BundleContents()
    for analysis in analyses:
        contents.modules.update(record[0] for record in analysis.pure)
        contents.binaries.update(record[0] for record in analysis.binaries)
        for records in (
            analysis.pure,
            analysis.binaries,
            analysis.datas,
            analysis.scripts,
        ):
            read_recorded_files(records, contents.files)
    return contents


def find_bundled_distributions(files: set[Path]) -> list[metadata.Distribution]:
    distributions = list(metadata.distributions())
    vendor_folders = {
        str(Path(distribution.locate_file(relative)).parent.parent)
        for distribution in distributions
        for relative in distribution.files or ()
        if "_vendor" in relative.parts and relative.name == "METADATA"
    }
    distributions.extend(metadata.distributions(path=sorted(vendor_folders)))
    included = []
    for distribution in distributions:
        name = distribution.metadata["Name"].casefold()
        # PyInstaller's bundle exception waives its own license notices.
        if name in {"pyinstaller", "pyinstaller-hooks-contrib"}:
            continue
        owned_files = {
            Path(distribution.locate_file(relative)).resolve()
            for relative in distribution.files or ()
        }
        if owned_files & files:
            included.append(distribution)
    return sorted(
        included,
        key=lambda distribution: (
            distribution.metadata["Name"].casefold(),
            distribution.version,
        ),
    )


def find_license_files(
    distribution: metadata.Distribution, bundled_files: set[Path]
) -> list[Path]:
    licenses = []
    for relative in distribution.files or ():
        name = relative.name.casefold()
        if "_vendor" in relative.parts:
            continue
        if not any(word in name for word in ("license", "copying", "notice")):
            continue
        if relative.suffix.casefold() in {".py", ".pyc"}:
            continue
        path = Path(distribution.locate_file(relative)).resolve()
        if not path.is_file():
            continue
        if any(part.endswith(".dist-info") for part in relative.parts):
            licenses.append(path)
        elif any(path.parent in source.parents for source in bundled_files):
            licenses.append(path)
    if not licenses:
        raise ValueError(
            f"No license files found for {distribution.metadata['Name']}."
        )
    return sorted(set(licenses))


def collect_source(distribution, destination: Path) -> None:
    name = distribution.metadata["Name"]
    version = distribution.version
    cache = BUILD_ROOT / "downloads" / "sources"
    cache.mkdir(parents=True, exist_ok=True)
    metadata_path = cache / f"{name}-{version}.json"
    if metadata_path.is_file():
        source_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    else:
        url = f"https://pypi.org/pypi/{name}/{version}/json"
        with open_url(url) as response:
            source_metadata = json.load(response)
        metadata_path.write_text(json.dumps(source_metadata), encoding="utf-8")
    if source_metadata["info"]["version"] != version:
        raise ValueError(f"Source metadata version does not match {name}.")
    archives = [
        entry
        for entry in source_metadata["urls"]
        if entry["packagetype"] == "sdist"
    ]
    if len(archives) != 1:
        raise ValueError(f"Expected one source archive for {name} {version}.")
    entry = archives[0]
    filename = entry["filename"]
    if Path(filename).name != filename:
        raise ValueError("Source archive has an invalid filename.")
    archive = cache / filename
    download_archive(entry["url"], archive, entry["digests"]["sha256"], False)
    shutil.copy2(archive, destination / filename)


def find_python_license() -> Path:
    candidates = (
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    )
    for path in candidates:
        if path.is_file():
            return path
    version = ".".join(map(str, sys.version_info[:3]))
    cached = BUILD_ROOT / "downloads" / "notices" / f"python-{version}.txt"
    if not cached.is_file():
        url = (
            "https://raw.githubusercontent.com/python/cpython/"
            f"v{version}/LICENSE"
        )
        cached.parent.mkdir(parents=True, exist_ok=True)
        with open_url(url) as response:
            cached.write_bytes(response.read())
    return cached


def collect_runtime_notices(contents: BundleContents) -> list[str]:
    python_license = find_python_license().read_text(encoding="utf-8")
    sections = ["Python\n======\n\n" + python_license]
    manifest = json.loads(
        (SCRIPTS_ROOT / "license_sources.json").read_text(encoding="utf-8")
    )
    for entry in manifest:
        modules = entry.get("modules", [])
        prefixes = entry.get("binaries", [])
        if modules and not any(name in contents.modules for name in modules):
            continue
        if prefixes and not any(
            Path(binary).name.startswith(prefix)
            for binary in contents.binaries
            for prefix in prefixes
        ):
            continue
        cached = BUILD_ROOT / "downloads" / "notices" / entry["filename"]
        download_archive(entry["url"], cached, entry["sha256"], False)
        component = entry["component"]
        sections.append(
            f"{component}\n{'=' * len(component)}\n\n{entry['url']}\n\n"
            + cached.read_text(encoding="utf-8")
        )
    return sections


def collect_licenses(contents: BundleContents) -> list[tuple[str, str, str]]:
    sections = collect_runtime_notices(contents)
    sources = BUILD_ROOT / "sources"
    if sources.exists():
        shutil.rmtree(sources)
    sources.mkdir(parents=True)
    sections.append(
        "Library replacement\n"
        "===================\n\n"
        "Run either program with --licenses to view these notices and save "
        "the bundled SOURCES.zip. "
        "Extract it to obtain the LGPL and MPL source archives and project "
        "source needed to rebuild with modified libraries. Reverse "
        "engineering to debug library modifications is permitted. In "
        "sources/project, run the setup-only command from "
        "README.md, install your modified library in build/.venv, then run "
        "the normal build command. When distributing modified libraries, "
        "replace their source archives with the matching modified sources.\n"
    )
    for distribution in find_bundled_distributions(contents.files):
        name = distribution.metadata["Name"]
        for license_file in find_license_files(distribution, contents.files):
            sections.append(
                f"{name} {distribution.version}: {license_file.name}\n\n"
                + license_file.read_text(encoding="utf-8")
            )
        license_expression = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or ""
        )
        if any(
            license_name in license_expression.casefold()
            for license_name in ("lgpl", "mpl")
        ):
            collect_source(distribution, sources)
    for source in source_files():
        output = sources / "project" / source.relative_to(PROJECT_ROOT)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
    materials = BUILD_ROOT / "licenses"
    materials.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "LICENSE", materials / "LICENSE")
    (materials / "THIRD_PARTY_NOTICES.txt").write_text(
        "\n\n".join(sections), encoding="utf-8"
    )
    with ZipFile(materials / "SOURCES.zip", "w", ZIP_DEFLATED) as archive:
        for source in sorted(sources.rglob("*")):
            if source.is_file():
                archive.write(source, source.relative_to(BUILD_ROOT))
    return [
        (f"licenses/{name}", str(materials / name), "DATA")
        for name in LICENSE_FILES
    ]
