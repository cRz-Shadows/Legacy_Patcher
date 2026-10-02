"""Download the pinned Project_CTR releases and verify their exact bytes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .files import sha256_file
from .toolchain import PlatformTools


def open_url(url: str):
    import certifi

    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    request = urllib.request.Request(
        url, headers={"User-Agent": "3DSPatcherBuild"}
    )
    return urllib.request.urlopen(request, timeout=30, context=context)


def download_archive(
    url: str, destination: Path, expected_sha256: str, offline: bool
) -> None:
    if destination.is_file() and sha256_file(destination) == expected_sha256:
        return
    if offline:
        raise RuntimeError(
            f"No verified cached download for {destination.name}. "
            "Run setup once with internet access before using --offline."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, suffix=".download", delete=False
    ) as temporary:
        staged = Path(temporary.name)
    try:
        print(f"Downloading {destination.name}...", flush=True)
        with (
            open_url(url) as response,
            staged.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        if sha256_file(staged) != expected_sha256:
            raise RuntimeError(
                f"Download checksum does not match: {destination.name}"
            )
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


def prepare_tool(
    entry: dict,
    destination: Path,
    cache: Path,
    repository: str,
    offline: bool = False,
) -> Path:
    output = destination / Path(entry["member"]).name
    if output.is_file() and sha256_file(output) == entry["sha256"]:
        output.chmod(output.stat().st_mode | 0o111)
        return output

    archive = cache / entry["archive"]
    url = (
        f"{repository}/releases/download/{entry['release']}/{entry['archive']}"
    )
    download_archive(url, archive, entry["archive_sha256"], offline)
    with zipfile.ZipFile(archive) as bundle:
        data = bundle.read(entry["member"])
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise RuntimeError(
            f"Executable checksum does not match: {entry['name']}"
        )

    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination, suffix=".new", delete=False
    ) as temporary:
        staged = Path(temporary.name)
        temporary.write(data)
    try:
        staged.chmod(staged.stat().st_mode | 0o111)
        os.replace(staged, output)
    finally:
        staged.unlink(missing_ok=True)
    return output


def prepare_tools(
    targets: list[PlatformTools],
    destination: Path,
    cache: Path,
    offline: bool = False,
) -> None:
    manifest = json.loads(
        Path(__file__).with_name("toolchain.json").read_text(encoding="utf-8")
    )
    for target in targets:
        for entry in manifest["platforms"][target.folder]:
            prepare_tool(
                entry,
                destination / target.folder,
                cache,
                manifest["repository"],
                offline,
            )
