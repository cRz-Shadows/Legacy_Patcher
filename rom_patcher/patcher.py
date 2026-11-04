"""Cross-platform 3DS patcher interface."""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .archives import (
    find_patchsets_from_selection,
    match_patchsets_by_code,
    materialize_patchset,
)
from .config import (
    CODE_BIN_RELPATH,
    CTRTOOL_NAME,
    MAKEROM_NAME,
    PATCH_ARCHIVE_NAMES,
    PATCHES_ROOT_DIR,
    ROM_EXTS,
)
from .dependencies import require_py7zr
from .identity import read_game_name
from .inputs import (
    SelectionCancelled,
    choose_all_locations,
    select_output_folder,
    select_rom,
    selection_folder,
)
from .names import choose_non_overwriting_output, make_output_base
from .patches import (
    apply_patches_dir,
    build_patch_plan,
    prepare_base_validation,
    read_base_manifest,
)
from .rom_builder import capture_original_cci_card_info, run_makerom
from .runtime import (
    bundle_dir,
    cci_has_romfs,
    create_hidden_workdir,
    ensure_build_free_space,
    extract_code_probe,
    extract_from_rom,
    prepare_workdir,
    resolve_tool,
    safe_rmtree,
    select_tools_dir,
    unpack_romfs_bin,
)


def friendly_failure_suggestions():
    return (
        "\n\nSuggestions:\n- Your ROM may be the wrong version or region.\n-"
        " Your ROM may already be modified and no longer match the base"
        " game.\n- Your ROM dump or decryption may be incomplete.\n- Make"
        " sure the ROM is decrypted and otherwise unmodified.\n"
    )


class PatcherWindow(object):

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Legacy Patcher")
        self.root.resizable(False, False)
        self.label = ttk.Label(self.root, text="Starting...")
        self.label.pack(padx=20, pady=(16, 8))
        self.progress = ttk.Progressbar(
            self.root, length=420, mode="determinate", maximum=1000
        )
        self.progress.pack(padx=20, pady=(0, 16))
        self._msg = "Starting..."
        self._mode = "determinate"
        self._done = 0
        self._total = 1
        self._final_message = None
        self._final_is_error = False
        self._patch_selection = None
        self._selected_roms = None
        self._output_folder = None
        self.root.after(50, self._tick)
        self.root.after(100, self._start_work)

    def _start_work(self):
        try:
            (
                self._patch_selection,
                self._selected_roms,
                self._output_folder,
            ) = self._resolve_inputs()
        except SelectionCancelled:
            self.root.destroy()
            return
        except Exception as exc:
            self._final_message = "Something went wrong.\n\n%s" % (
                str(exc).strip() if str(exc) else "Unknown error"
            )
            self._final_is_error = True
            return
        threading.Thread(target=self._work, daemon=True).start()

    def _resolve_inputs(self):
        folder = selection_folder()
        patch_selection = None
        if not choose_all_locations():
            for candidate in (folder / PATCHES_ROOT_DIR, folder):
                if candidate.is_dir() and find_patchsets_from_selection(
                    candidate
                ):
                    patch_selection = candidate
                    break
        if patch_selection is None:
            patch_selection = self._choose_patch_folder(folder)
        rom = select_rom(folder, parent=self.root)
        output_folder = select_output_folder(folder, parent=self.root)
        return patch_selection, [rom], output_folder

    def _choose_patch_folder(
        self,
        initial_folder,
        title="Select patches, wrapper, or one patch-set folder",
    ):
        initial_folder = Path(initial_folder)
        while True:
            selected = filedialog.askdirectory(
                title=title,
                initialdir=str(initial_folder),
                mustexist=True,
                parent=self.root,
            )
            if not selected:
                raise SelectionCancelled()
            candidate = Path(selected).resolve()
            try:
                patchsets = find_patchsets_from_selection(candidate)
            except Exception:
                patchsets = []
            if patchsets:
                return candidate
            messagebox.showerror(
                "No Valid Patch Sets Found",
                "Select the main patches folder, a wrapper folder containing "
                "patch-set folders, or one individual patch-set folder.",
                parent=self.root,
            )
            initial_folder = candidate

    def set_stage(self, text, mode="determinate"):
        self._msg = text
        self._mode = mode
        self._done = 0
        self._total = 1

    def set_progress(self, done, total):
        self._done = done
        self._total = max(1, total)

    def _tick(self):
        if self._final_message is not None:
            if self._final_is_error:
                messagebox.showerror("Result", self._final_message.strip())
            else:
                messagebox.showinfo("Result", self._final_message.strip())
            self.root.destroy()
            return
        self.label.configure(text=self._msg)
        if self._mode == "indeterminate":
            if str(self.progress["mode"]) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
        else:
            if str(self.progress["mode"]) != "determinate":
                try:
                    self.progress.stop()
                except Exception:
                    pass
                self.progress.configure(mode="determinate")
            value = int(float(self._done) / float(self._total) * 1000.0)
            if value < 0:
                value = 0
            if value > 1000:
                value = 1000
            self.progress["value"] = value
        self.root.after(50, self._tick)

    def _work(self):
        output_folder = Path(self._output_folder)
        successes = []
        failures = []
        workdir = None
        try:
            self.set_stage("Preparing game tools...", mode="indeterminate")
            tools_dir = select_tools_dir(bundle_dir())
            workdir = create_hidden_workdir()
            if not tools_dir.is_dir():
                raise RuntimeError(
                    "Bundled tools folder is missing or damaged:\n\n%s"
                    % tools_dir.as_posix()
                )
            ctrtool = resolve_tool(tools_dir, CTRTOOL_NAME)
            makerom = resolve_tool(tools_dir, MAKEROM_NAME)
            patchsets = find_patchsets_from_selection(self._patch_selection)
            if not patchsets:
                raise RuntimeError(
                    "No valid patch sets were found. Each set needs an RSF, "
                    "_base_manifest.json, and archived or loose patch files."
                )
            needs_py7zr = any(
                (patchset_dir / archive_name).is_file()
                for patchset_dir, _rsf_rel in patchsets
                for archive_name in PATCH_ARCHIVE_NAMES
            )
            if needs_py7zr:
                require_py7zr()
            if self._selected_roms is None:
                roms = sorted(
                    (
                        path
                        for path in output_folder.iterdir()
                        if path.is_file() and path.suffix.casefold() in ROM_EXTS
                    ),
                    key=lambda path: path.name.casefold(),
                )
            else:
                roms = list(self._selected_roms)
            if not roms:
                raise RuntimeError("No .3ds or .cci ROM was selected or found.")

            def extract_clean_rom(rom_path, destination, label):
                safe_rmtree(destination)
                destination.mkdir(parents=True, exist_ok=True)
                self.set_stage(label, mode="indeterminate")
                romfs_bin = extract_from_rom(ctrtool, rom_path, destination)
                if not romfs_bin.is_file():
                    if cci_has_romfs(rom_path):
                        raise RuntimeError(
                            "Extraction failed because romfs.bin was not created."
                        )
                    return
                self.set_stage("Unpacking RomFS...", mode="indeterminate")
                unpack_romfs_bin(ctrtool, romfs_bin, destination / "romfs")
                try:
                    romfs_bin.unlink()
                except OSError:
                    pass

            def probe_patchsets(rom_path, probe_dir):
                self.set_stage(
                    "Checking ROM version from code.bin...",
                    mode="indeterminate",
                )
                try:
                    code_path = extract_code_probe(ctrtool, rom_path, probe_dir)
                    return match_patchsets_by_code(patchsets, code_path)
                except Exception:
                    return None
                finally:
                    safe_rmtree(probe_dir)

            for rom_index, rom in enumerate(roms, 1):
                prepare_workdir(workdir)
                build_dir = workdir / "build"
                probe_dir = workdir / "probe"
                patch_stage = workdir / "patchset"
                try:
                    self.set_stage(
                        "Checking free disk space...", mode="indeterminate"
                    )
                    ensure_build_free_space(
                        workdir.parent, output_folder, rom.stat().st_size
                    )
                    original_card_info = capture_original_cci_card_info(rom)
                    probe_result = probe_patchsets(rom, probe_dir)
                    fully_extracted = False
                    if probe_result is None:
                        extract_clean_rom(
                            rom,
                            build_dir,
                            "Extracting ROM (%d/%d): %s"
                            % (rom_index, len(roms), rom.name),
                        )
                        fully_extracted = True
                        self.set_stage(
                            "Matching patch sets from code.bin...",
                            mode="indeterminate",
                        )
                        matching_patchsets, rejected = match_patchsets_by_code(
                            patchsets, build_dir / Path(CODE_BIN_RELPATH)
                        )
                    else:
                        matching_patchsets, rejected = probe_result
                    if not matching_patchsets:
                        reason = "No patch set has a matching base code.bin."
                        if rejected:
                            details = ["%s: %s" % item for item in rejected[:8]]
                            reason += "\n  - " + "\n  - ".join(details)
                            if len(rejected) > 8:
                                reason += "\n  - Additional results omitted"
                        failures.append((rom.name, reason))
                        continue
                    if not fully_extracted:
                        extract_clean_rom(
                            rom,
                            build_dir,
                            "Extracting ROM (%d/%d): %s"
                            % (rom_index, len(roms), rom.name),
                        )
                        self.set_stage(
                            "Confirming extracted code.bin...",
                            mode="indeterminate",
                        )
                        confirmed, rejected = match_patchsets_by_code(
                            patchsets, build_dir / Path(CODE_BIN_RELPATH)
                        )
                        expected_paths = [
                            str(item[0].resolve())
                            for item in matching_patchsets
                        ]
                        confirmed_paths = [
                            str(item[0].resolve()) for item in confirmed
                        ]
                        if confirmed_paths != expected_paths:
                            raise RuntimeError(
                                "ExeFS probing and complete extraction"
                                " produced different code.bin results"
                            )
                except Exception as exc:
                    failures.append(
                        (
                            rom.name,
                            (
                                str(exc).splitlines()[0]
                                if str(exc)
                                else "Extraction failed"
                            ),
                        )
                    )
                    continue
                base_game_name = read_game_name(build_dir)
                for match_index, (patchset_dir, rsf_rel) in enumerate(
                    matching_patchsets
                ):
                    out_rom = None
                    try:
                        if match_index > 0:
                            extract_clean_rom(
                                rom,
                                build_dir,
                                "Re-extracting %s for patch set %d/%d..."
                                % (
                                    rom.name,
                                    match_index + 1,
                                    len(matching_patchsets),
                                ),
                            )
                            confirmed, _rejected = match_patchsets_by_code(
                                patchsets, build_dir / Path(CODE_BIN_RELPATH)
                            )
                            if (patchset_dir, rsf_rel) not in confirmed:
                                raise RuntimeError(
                                    "Re-extracted code.bin no longer matches"
                                    " the patch set"
                                )
                        self.set_stage(
                            "Extracting matched patch set %d/%d: %s"
                            % (
                                match_index + 1,
                                len(matching_patchsets),
                                patchset_dir.name,
                            ),
                            mode="indeterminate",
                        )
                        materialize_patchset(patchset_dir, patch_stage)
                        staged_rsf = patch_stage / rsf_rel
                        if not staged_rsf.is_file():
                            raise RuntimeError(
                                "Patch-set RSF was not extracted"
                            )
                        patch_plan = build_patch_plan(patch_stage, build_dir)
                        self.set_stage(
                            "Validating required base files...",
                            mode="determinate",
                        )
                        validation_state = prepare_base_validation(
                            patch_stage,
                            build_dir,
                            self.set_progress,
                            patch_plan,
                        )
                        self.set_stage("Patching files...", mode="determinate")
                        apply_patches_dir(
                            patch_stage,
                            build_dir,
                            self.set_progress,
                            validation_state,
                        )
                        manifest = read_base_manifest(patch_stage)
                        display_name = (
                            manifest.get("display_name") if manifest else None
                        )
                        patched_game_name = read_game_name(build_dir)
                        if patched_game_name is not None and (
                            display_name is not None
                            or patched_game_name != base_game_name
                        ):
                            display_name = patched_game_name
                        output_base = make_output_base(
                            rom,
                            display_name,
                        )
                        out_rom = choose_non_overwriting_output(
                            output_folder, output_base, ".3ds"
                        )
                        self.set_stage(
                            "Building, validating, and publishing output"
                            " ROM...",
                            mode="indeterminate",
                        )
                        run_makerom(
                            makerom,
                            build_dir,
                            staged_rsf,
                            out_rom,
                            original_card_info,
                            validation_state,
                            self.set_progress,
                        )
                        successes.append(
                            (rom.name, patchset_dir.name, out_rom.name)
                        )
                    except Exception as exc:
                        if out_rom is not None and out_rom.exists():
                            try:
                                out_rom.unlink()
                            except OSError:
                                pass
                        detail = (
                            str(exc).splitlines()[0] if str(exc) else "error"
                        )
                        failures.append(
                            (
                                "%s + %s" % (rom.name, patchset_dir.name),
                                "Build failed: %s" % detail,
                            )
                        )
                    finally:
                        self.set_stage(
                            "Cleaning patch staging...", mode="indeterminate"
                        )
                        safe_rmtree(patch_stage)
                self.set_stage(
                    "Cleaning extracted work files...", mode="indeterminate"
                )
                safe_rmtree(workdir)
            if successes and (not failures):
                message = "Done!\n\nBuilt ROM(s):\n"
                for rom_name, patchset_name, output_name in successes:
                    message += "- %s + %s -> %s\n" % (
                        rom_name,
                        patchset_name,
                        output_name,
                    )
                self._final_message = message
                self._final_is_error = False
                return
            if successes and failures:
                message = "Completed with some issues.\n\nSucceeded:\n"
                for rom_name, patchset_name, output_name in successes:
                    message += "- %s + %s -> %s\n" % (
                        rom_name,
                        patchset_name,
                        output_name,
                    )
                message += "\nFailed:\n"
                for rom_name, reason in failures:
                    message += "- %s\n  %s\n" % (rom_name, reason)
                self._final_message = message + friendly_failure_suggestions()
                self._final_is_error = False
                return
            message = "No ROM could be built.\n\n"
            if failures:
                message += "What happened:\n"
                for rom_name, reason in failures:
                    message += "- %s\n  %s\n" % (rom_name, reason)
            self._final_message = message + friendly_failure_suggestions()
            self._final_is_error = True
        except Exception as exc:
            message = "Something went wrong.\n\n%s" % (
                str(exc).strip() if str(exc) else "Unknown error"
            )
            self._final_message = message + friendly_failure_suggestions()
            self._final_is_error = True
        finally:
            if workdir is not None:
                safe_rmtree(workdir)

    def run(self):
        self.root.mainloop()


def main():
    PatcherWindow().run()
