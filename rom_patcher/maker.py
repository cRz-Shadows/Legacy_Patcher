"""Create compatible patch sets from updated 3DS game files."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .archives import compress_patch_subtree, publish_patchset
from .bps import apply_ips_to_file, create_bps_patch
from .config import (
    BASE_MANIFEST_NAME,
    CODE_BIN_RELPATH,
    CODE_IPS_RELPATH,
    EXHEADER_RELPATH,
    GARC_PATCH_EXT,
    GARC_PATCH_MIN_SOURCE_SIZE,
    HARDWARE_IDENTITY_NAME,
    HASH_ALGO_COMPARE,
    IGNORE_CASE,
    REMOVE_FROM_ROOT,
    XY_GAMES,
    GameIdentity,
)
from .encounter_repair import repair_encounter_locations
from .files import build_base_manifest, build_index, files_differ, safe_rename
from .garc import create_garc_patch, should_use_garc_patch
from .identity import (
    apply_identity_to_temp_tree,
    detect_game_identity,
    hardware_identity_manifest,
    read_exheader,
    restore_base_identity,
    write_makerom_rsf,
)
from .rsf import write_romfs_rsf
from .runtime import app_dir


def normalize_patch_format(
    root_a_temp: Path,
) -> Path | None:
    """Normalize extracted filenames and locate the single build RSF."""

    exefs_dir = root_a_temp / "exefs"
    if exefs_dir.is_dir():
        icon_plain = exefs_dir / "icon"
        icon_bin = exefs_dir / "icon.bin"
        if icon_plain.is_file():
            safe_rename(icon_plain, icon_bin)

        dot_code = exefs_dir / ".code.bin"
        code_bin = exefs_dir / "code.bin"
        if dot_code.is_file():
            safe_rename(dot_code, code_bin)

    for name in REMOVE_FROM_ROOT:
        p = root_a_temp / name
        if p.is_file():
            try:
                p.unlink()
            except Exception:
                pass

    candidates = sorted(
        (path for path in root_a_temp.glob("*.rsf") if path.is_file()),
        key=lambda path: path.name.casefold(),
    )
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise ValueError(
            f"Keep one root-level RSF file in the game folder: {names}"
        )
    return candidates[0] if candidates else None


@dataclass(frozen=True)
class PreparedGame:
    rsf: Path
    identity: GameIdentity
    base_exheader: bytes
    source_exheader: bytes


def prepare_updated_tree(
    staged_updated: Path,
    clean_base: Path,
    *,
    title_id: int | None = None,
    display_name: str | None = None,
) -> PreparedGame:
    rsf = normalize_patch_format(staged_updated)
    if rsf is None:
        raise ValueError(
            "The updated game folder needs one root-level RSF file."
        )
    ips_path = staged_updated / CODE_IPS_RELPATH
    if ips_path.is_file():
        apply_ips_to_file(ips_path, staged_updated / CODE_BIN_RELPATH)
        ips_path.unlink()
    identity = detect_game_identity(
        clean_base,
        rsf,
        title_id=title_id,
        display_name=display_name,
    )
    base_exheader = read_exheader(clean_base)
    source_exheader = (
        read_exheader(staged_updated)
        if (staged_updated / EXHEADER_RELPATH).is_file()
        else base_exheader
    )
    restore_base_identity(staged_updated, clean_base, rsf)
    apply_identity_to_temp_tree(staged_updated, clean_base, identity)
    if identity.source_unique_id in XY_GAMES:
        repair_encounter_locations(staged_updated, clean_base)
    print(f"Application Title ID: {identity.application_title_id:016X}")
    return PreparedGame(rsf, identity, base_exheader, source_exheader)


def write_prepared_rsf(game: PreparedGame, destination: Path) -> None:
    if game.identity.changes_title_id:
        write_makerom_rsf(
            game.rsf,
            destination,
            game.identity,
            game.base_exheader,
            game.identity.application_title_id,
            game.source_exheader,
        )
    else:
        write_romfs_rsf(game.rsf, destination)


def create_patchset(
    updated_root: Path,
    base_root: Path,
    output_folder: Path | None = None,
    *,
    title_id: int | None = None,
    display_name: str | None = None,
) -> int:
    updated_root = Path(updated_root).resolve()
    clean_base = Path(base_root).resolve()
    destination = Path(output_folder) if output_folder else app_dir()

    if not updated_root.is_dir():
        raise SystemExit(f"Not a directory: {updated_root}")
    if not clean_base.is_dir():
        raise SystemExit(f"Not a directory: {clean_base}")

    source_workspace = Path(tempfile.mkdtemp(prefix="patch_a_norm_"))
    staged_updated = source_workspace / "updated"
    rsf_path: Path | None = None

    staged_patches: Path | None = None

    try:

        shutil.copytree(updated_root, staged_updated, dirs_exist_ok=True)

        game = prepare_updated_tree(
            staged_updated,
            clean_base,
            title_id=title_id,
            display_name=display_name,
        )
        identity = game.identity
        rsf_path = game.rsf
        output_patchset = destination / identity.patchset_dir_name

        updated_index = build_index(staged_updated, IGNORE_CASE)
        base_index = build_index(clean_base, IGNORE_CASE)

        base_manifest = build_base_manifest(clean_base)
        if display_name is not None:
            base_manifest["display_name"] = identity.display_name

        ignore_relpaths = {
            CODE_IPS_RELPATH.lower(),
            rsf_path.relative_to(staged_updated).as_posix().lower(),
        }

        diffs: list[tuple[str, Path, Path]] = []

        for key, updated_path in updated_index.items():
            rel = updated_path.relative_to(staged_updated).as_posix()
            relative_lower = rel.lower()

            if relative_lower in ignore_relpaths:
                continue

            if relative_lower == CODE_IPS_RELPATH.lower():
                continue

            if key not in base_index:

                raise SystemExit(
                    "ERROR: Updated folder contains a file not present in"
                    f" base:\n  {rel}\nAdded files are not supported by this"
                    " patch-set format."
                )

            base_path = base_index[key]
            if files_differ(updated_path, base_path, HASH_ALGO_COMPARE):
                diffs.append((rel, base_path, updated_path))

        if not diffs:
            print("No differing files found. Nothing to do.")
            return 0

        staged_patches = Path(tempfile.mkdtemp(prefix="game_patches_"))
        (staged_patches / "exefs").mkdir()
        (staged_patches / "romfs").mkdir()
        patch_count = 0
        garc_count = 0
        bps_count = 0

        for rel, base_file, updated_file in diffs:
            use_garc = should_use_garc_patch(base_file, updated_file)
            if use_garc:
                out_patch = staged_patches / (rel + GARC_PATCH_EXT)
                out_patch.parent.mkdir(parents=True, exist_ok=True)
                print("Exact GARC patch:", rel)
                stats = create_garc_patch(
                    base_file, updated_file, out_patch, rel
                )
                print(
                    "  modes: "
                    f"packed-copy={stats['copy_packed']}, "
                    f"same-raw/re-encode={stats['same_raw']}, "
                    f"xor-changes={stats['xor_raw']}, "
                    f"variable-raw-delta={stats['raw_delta']}, "
                    f"new-packed={stats['literal_packed']}"
                )
                print(
                    "  stored components: "
                    f"XOR={stats['xor_blob_bytes'] / (1024 * 1024):.2f} MiB, "
                    f"LZ recipes={stats['recipe_blob_bytes'] / 1048576:.2f}"
                    " MiB, "
                    "variable"
                    f" deltas={stats['delta_blob_bytes'] / (1024 * 1024):.2f}"
                    " MiB, "
                    f"new literals={stats['literal_blob_bytes'] / 1048576:.2f}"
                    " MiB"
                )
                print(
                    "  final .garcpatch:"
                    f" {stats['patch_bytes'] / (1024 * 1024):.2f} MiB"
                )
                garc_count += 1
            else:
                if base_file.stat().st_size > GARC_PATCH_MIN_SOURCE_SIZE:
                    print("Large non-GARC file, using BPS:", rel)
                out_bps = staged_patches / (rel + ".bps")
                out_bps.parent.mkdir(parents=True, exist_ok=True)
                print("BPS:", rel)
                create_bps_patch(base_file, updated_file, out_bps, rel)
                bps_count += 1
            patch_count += 1

        manifest_path = staged_patches / BASE_MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(base_manifest, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        print(
            f"Added full base-file manifest: {len(base_manifest['files'])}"
            " files"
        )

        if identity.changes_title_id:
            hardware_manifest_path = staged_patches / HARDWARE_IDENTITY_NAME
            hardware_manifest_path.write_text(
                json.dumps(
                    hardware_identity_manifest(
                        identity, game.base_exheader, game.source_exheader
                    ),
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        write_prepared_rsf(game, staged_patches / rsf_path.name)

        for subtree in ("exefs", "romfs"):
            print(f"Compressing {subtree}.7z with py7zr ultra LZMA2...")
            archive = compress_patch_subtree(staged_patches, subtree)
            print(
                f"  {archive.name}:"
                f" {archive.stat().st_size / (1024 * 1024):.2f} MiB"
            )

        publish_patchset(staged_patches, output_patchset)
        staged_patches = None

        print("\nWrote patch set:", output_patchset)
        if identity.display_name:
            print("Display name:", identity.display_name)
        print("Application Title ID: 0x%016X" % identity.application_title_id)
        print(
            f"Patches: {patch_count} ({garc_count} exact GARC, {bps_count}"
            " BPS)"
        )
        print("Archives: exefs.7z, romfs.7z")
        print(
            "Exact GARC patches are verified against the final target SHA-256"
            " during application."
        )
        return 0

    finally:

        if staged_patches is not None:
            try:
                if staged_patches.exists():
                    shutil.rmtree(staged_patches)
            except Exception:
                pass

        try:
            shutil.rmtree(source_workspace)
        except Exception:
            pass
