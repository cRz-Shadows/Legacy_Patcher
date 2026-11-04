# Legacy Patcher

This is a program for creating and patching 3DS roms for the legal distribution of patch files for mods that do not contain original code from the game. Apply patches to decrypted `.3ds` or `.cci` ROMs, or create patches from updated game files.

Whole-ROM BPS patches can become large and slow when changes affect compressed data or the ROM's layout. Legacy Patcher keeps patches smaller by extracting the ROM, patching its files, and rebuilding it. For large GARC archives, `.garcpatch` patches the individual files inside the archive.

The Patch maker includes Pokémon X/Y Game Text repair for mods where pk3ds has been used.

[Apply patches](#apply-patches) · [Make patches](#make-patches) · [Build](#build)

**First use:** either program needs internet to download verified
[Project_CTR tools](https://github.com/3DSGuy/Project_CTR/releases).
Later runs reuse them from your system's cache.

## Apply patches

1. Put your ROM and patch folders beside `legacy_patcher`, then run it. Choose any missing inputs, or select one ROM if several exist. On **macOS:** you will have to choose the file location every time due to app translocation.
2. Wait for the finished ROM. Windows/Linux save it beside the program. MacOS asks you to select a folder for the output ROM.

Patch folders can have any name, with an optional `patches` wrapper. Outputs use `<original filename>_patched.3ds`, or the stored game title if renamed. Existing outputs receive numbered filenames.

## Make patches

1. Run `legacy_patch_maker` and choose your updated game folder. It needs one `.rsf` file and the changed game files.
2. Choose the clean base ROM if the program cannot find one beside itself.
3. Choose whether to change the title ID or game name. Both default to **No**. The prompt shows the base game's ID. Choosing **Yes** lets you enter your own value.

Windows/Linux save patches beside the maker. macOS always asks you to choose the game folder, base ROM, and output folder. Rebuilding the same patch-set name replaces that set.

- **Title ID:** identifies the game and selects its save data and emulator custom texture folders. A different ID keeps these separate from the base game. Enter only the five middle hexadecimal digits shown in the dialog. The base game's permissions must allow the ID.
- **Game name:** changes the label shown on the 3DS HOME Menu and in emulator game lists, and becomes the output ROM filename. Use up to 63 characters; unsupported characters are rejected and Windows filename characters become underscores.
- **Game files:** partial updated folders can include new files under `romfs/`. The patch maker stores them as `.bps` patches in `romfs.7z`. New files cannot be empty.

| Game | Enter | Full Title ID |
| --- | --- | --- |
| X Legacy | `E4C31` | `000400000E4C3100` |
| Y Legacy | `E4C32` | `000400000E4C3200` |

**Do not use an ID already used by another game.** Matching IDs can share saves and emulator textures. Use these Legacy examples only for their respective games.

Choosing **No** restores the base game's ID or name, even if your updated files already changed it. For X/Y, text repair updates the selected Game Text file and keeps backups and state in `reports/text_repair`. Keep those reports between editing sessions.

## Build

Build on the operating system you want to release for. Use 64-bit Python with Tk and a C compiler. Run these commands from the project root.

### Windows, x86_64

Install Python and the C++ compiler with
[WinGet](https://learn.microsoft.com/en-us/windows/package-manager/winget/):

```powershell
winget install --exact --id Python.Python.3.12
winget install --exact --id Microsoft.VisualStudio.2022.BuildTools `
  --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

Reopen your terminal, then build:

```powershell
python scripts/build.py
```

### Ubuntu, x86_64, including WSL 2

```sh
sudo apt update
sudo apt install python3 python3-tk python3-venv build-essential
python3 scripts/build.py
```

WSL produces Linux programs. File pickers require
[WSLg](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gui-apps).

### macOS, Intel or Apple silicon

Run:

```sh
xcode-select --install
curl -fLO https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg
sudo installer -pkg python-3.12.10-macos11.pkg -target /
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 scripts/build.py
```

Skip `xcode-select --install` if the compiler tools are already installed, and skip the two install commands if Python 3.12 from python.org is already installed. The build needs the python.org Python because it makes universal2 programs that run natively on Intel and Apple silicon; Homebrew's Python cannot.

### Build output

The script handles the Python environment, dependencies, tool downloads, compilation of `scripts/native/legacy_lz11.c`, and both executables. All platforms use **CTRTool 1.3.0** and **MakeROM 0.19.0**.

Your compiled programs appear in `dist/<system>`, such as `dist/macos`.
