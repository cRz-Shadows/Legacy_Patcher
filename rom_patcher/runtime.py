"""Application paths, helper tools, and temporary storage."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import (
    OUTPUT_FREE_MARGIN_BYTES,
    OUTPUT_FREE_MIN_BYTES,
    REQUIRED_FREE_BYTES,
    STALE_WORKDIR_MAX_AGE_SECONDS,
    WORKDIR_PREFIX,
)
from .toolchain import get_platform_tools, tool_cache_root, tools_root


def app_dir():
    """Locate the launcher folder; macOS user paths come from dialogs."""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            for parent in executable.parents:
                if parent.suffix.casefold() == ".app":
                    return parent.parent
        return executable.parent
    return Path(__file__).resolve().parent.parent


def bundle_dir():
    """
    Folder containing bundled resources.

    In a PyInstaller one-file build this is the temporary extraction directory.
    When running from source it is simply the script directory.
    """
    pyinstaller_root = getattr(sys, "_MEIPASS", None)
    if pyinstaller_root:
        return Path(pyinstaller_root).resolve()
    return Path(__file__).resolve().parent.parent


def select_tools_dir(resources_dir: Path) -> Path:
    target = get_platform_tools()
    if getattr(sys, "frozen", False):
        from .tool_downloads import prepare_tools

        cache = tool_cache_root()
        try:
            prepare_tools([target], cache / "tools", cache / "downloads")
        except OSError as error:
            raise RuntimeError(
                "Game tools could not be downloaded. Connect to the internet "
                "and try again."
            ) from error
        return cache / "tools" / target.folder
    selected = tools_root(resources_dir) / target.folder
    if not selected.is_dir():
        raise RuntimeError(
            "Native tools are missing. Run the setup-only build command "
            "in README.md before running from source."
        )
    return selected


def human_bytes(n):
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    x = float(n)
    for u in units:
        if x < 1024.0 or u == units[-1]:
            return "%d B" % int(x) if u == "B" else "%.2f %s" % (x, u)
        x /= 1024.0
    return "%d B" % n


def ensure_free_space(folder, required_bytes):
    usage = shutil.disk_usage(str(folder))
    if usage.free < required_bytes:
        raise RuntimeError(
            "Not enough free disk space.\n\n"
            "Required: %s\n"
            "Available: %s\n\n"
            "Please free up space on this drive and try again."
            % (human_bytes(required_bytes), human_bytes(usage.free))
        )


def ensure_build_free_space(
    temp_folder: Path, output_folder: Path, source_size: int
) -> None:
    """
    Check the work and destination volumes without double-counting one volume.
    """
    temp_folder = Path(temp_folder)
    output_folder = Path(output_folder)
    output_required = max(
        OUTPUT_FREE_MIN_BYTES, int(source_size) + OUTPUT_FREE_MARGIN_BYTES
    )
    try:
        same_volume = (
            os.stat(temp_folder).st_dev == os.stat(output_folder).st_dev
        )
    except OSError:
        same_volume = False
    if same_volume:
        ensure_free_space(
            temp_folder, max(REQUIRED_FREE_BYTES, output_required)
        )
    else:
        ensure_free_space(temp_folder, REQUIRED_FREE_BYTES)
        ensure_free_space(output_folder, output_required)


def safe_rmtree(path):
    try:
        if path.exists():
            shutil.rmtree(str(path))
    except Exception:
        pass


def _hide_path_on_windows(path: Path, *, required: bool = False):
    """
    Mark a path hidden on Windows.

    Temporary output staging must never appear in a normal Explorer view. When
    ``required`` is true, failure to set or verify the hidden attribute stops
    the run instead of leaving a visible staging directory beside the output
    ROM.
    """
    if os.name != "nt":
        return

    FILE_ATTRIBUTE_HIDDEN = 0x00000002
    INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetFileAttributesW.restype = ctypes.c_uint32
        kernel32.SetFileAttributesW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        kernel32.SetFileAttributesW.restype = ctypes.c_bool

        value = kernel32.GetFileAttributesW(str(path))
        if value == INVALID_FILE_ATTRIBUTES:
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.SetFileAttributesW(
            str(path), value | FILE_ATTRIBUTE_HIDDEN
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        verified = kernel32.GetFileAttributesW(str(path))
        if verified == INVALID_FILE_ATTRIBUTES:
            raise ctypes.WinError(ctypes.get_last_error())
        if not verified & FILE_ATTRIBUTE_HIDDEN:
            raise OSError("Windows did not retain the hidden attribute")
    except Exception as exc:
        if required:
            raise RuntimeError(
                "Could not create a hidden output staging directory: %s" % exc
            ) from exc


def _process_is_running(pid: int) -> bool:
    """
    Return True when a work-directory owner process still appears active.
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 4096
            ERROR_ACCESS_DENIED = 5
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                ctypes.c_uint32,
                ctypes.c_bool,
                ctypes.c_uint32,
            ]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def cleanup_stale_workdirs(temp_root: Path):
    """Remove abandoned patcher work folders, but never an active run."""
    now = time.time()
    for path in temp_root.glob(WORKDIR_PREFIX + "*"):
        if not path.is_dir():
            continue
        try:
            age = now - path.stat().st_mtime
            if age < STALE_WORKDIR_MAX_AGE_SECONDS:
                continue
            suffix = path.name[len(WORKDIR_PREFIX) :]
            pid_text = suffix.split("_", 1)[0]
            pid = int(pid_text) if pid_text.isdigit() else -1
            if _process_is_running(pid):
                continue
            safe_rmtree(path)
        except Exception:
            pass


def create_hidden_workdir() -> Path:
    """Create the multi-gigabyte work area under the user's temp directory."""
    temp_root = Path(tempfile.gettempdir()).resolve()
    cleanup_stale_workdirs(temp_root)
    path = Path(
        tempfile.mkdtemp(
            prefix="%s%d_" % (WORKDIR_PREFIX, os.getpid()), dir=str(temp_root)
        )
    ).resolve()
    _hide_path_on_windows(path)
    return path


def create_hidden_output_staging_dir(output_parent: Path) -> Path:
    """
    Create a same-volume staging directory hidden from normal file views.
    """
    path = Path(
        tempfile.mkdtemp(
            prefix=".GameOutput_%d_" % os.getpid(),
            dir=str(output_parent),
        )
    ).resolve()
    try:
        _hide_path_on_windows(path, required=True)
        return path
    except Exception:
        safe_rmtree(path)
        raise


def prepare_workdir(path):
    safe_rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    _hide_path_on_windows(path)


def run_cmd(cmd, cwd):
    kwargs = {"cwd": str(cwd), "capture_output": True, "text": True}
    restore_dll_dir = None
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        if getattr(sys, "frozen", False):
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
                kernel32.SetDllDirectoryW.restype = ctypes.c_bool
                restore_dll_dir = str(bundle_dir())
                kernel32.SetDllDirectoryW(None)
            except Exception:
                restore_dll_dir = None
    try:
        try:
            p = subprocess.run(cmd, **kwargs)
        finally:
            if restore_dll_dir is not None:
                try:
                    ctypes.WinDLL(
                        "kernel32", use_last_error=True
                    ).SetDllDirectoryW(restore_dll_dir)
                except Exception:
                    pass
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Required helper tool could not be launched: %s" % exc
        )
    except PermissionError as exc:
        advice = ""
        if sys.platform == "darwin":
            advice = (
                "\n\nmacOS blocked the bundled helper. Check the app's"
                " Gatekeeper approval and code signature."
            )
        raise RuntimeError(
            "Required helper tool is not executable: %s%s" % (exc, advice)
        )
    if p.returncode != 0:
        out = ""
        if p.stdout and p.stdout.strip():
            out += "\n[stdout]\n" + p.stdout
        if p.stderr and p.stderr.strip():
            out += "\n[stderr]\n" + p.stderr
        raise RuntimeError("Command failed (exit %d).%s" % (p.returncode, out))
    return p


def resolve_tool(tools_dir: Path, base_name: str) -> Path:
    names = (
        (base_name + ".exe", base_name + ".EXE", base_name)
        if os.name == "nt"
        else (base_name, base_name + ".exe", base_name + ".EXE")
    )
    for name in names:
        candidate = tools_dir / name
        if not candidate.is_file():
            continue
        if os.name != "nt":
            try:
                mode = candidate.stat().st_mode
                if not mode & 73:
                    candidate.chmod(mode | 73)
            except OSError:
                pass
            if not os.access(candidate, os.X_OK):
                raise RuntimeError(
                    "Bundled helper is not executable:\n\n%s" % candidate
                )
        return candidate
    expected = ", ".join(names)
    raise RuntimeError(
        "Missing required helper '%s'.\n\nExpected one of %s in:\n%s"
        % (base_name, expected, tools_dir)
    )


def extract_from_rom(ctrtool, rom, out_root):
    exefs_dir = out_root / "exefs"
    exefs_dir.mkdir(parents=True, exist_ok=True)
    romfs_bin = out_root / "romfs.bin"
    cmd = [
        str(ctrtool),
        "--exheader=%s" % (out_root / "exheader.bin"),
        "--exefsdir=%s" % exefs_dir,
        "--romfs=%s" % romfs_bin,
        "--logo=%s" % (out_root / "logo.bin"),
        "--plainrgn=%s" % (out_root / "plain.bin"),
        str(rom),
    ]
    run_cmd(cmd, cwd=out_root)
    return romfs_bin


def unpack_romfs_bin(ctrtool, romfs_bin, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(ctrtool), "--romfsdir=%s" % out_dir, str(romfs_bin)]
    run_cmd(cmd, cwd=out_dir)


def extract_code_probe(ctrtool, rom, out_root):
    """
    Extract only ExeFS so unsupported ROMs can be rejected before RomFS work.
    """
    safe_rmtree(out_root)
    exefs_dir = Path(out_root) / "exefs"
    exefs_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(ctrtool), "--exefsdir=%s" % exefs_dir, str(rom)]
    run_cmd(cmd, cwd=out_root)
    for name in ("code.bin", ".code.bin", "code"):
        candidate = exefs_dir / name
        if candidate.is_file():
            return candidate
    raise RuntimeError("ExeFS probe did not produce code.bin")


def _manifest_records_for_prefix(manifest_or_records) -> list[dict]:
    if isinstance(manifest_or_records, dict):
        return list(manifest_or_records["records"])
    return list(manifest_or_records)
