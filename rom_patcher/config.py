"""Shared format, title, build, and resource settings."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GameIdentity:
    game_name: str
    source_unique_id: int
    target_unique_id: int
    display_name: str
    patchset_dir_name: str
    title_id_flags: int = 0

    @property
    def changes_title_id(self) -> bool:
        return self.source_unique_id != self.target_unique_id

    @property
    def source_title_id(self) -> int:
        base = TITLE_ID_APPLICATION | self.title_id_flags
        return base | (self.source_unique_id << 8)

    @property
    def application_title_id(self) -> int:
        base = TITLE_ID_APPLICATION | self.title_id_flags
        return base | (self.target_unique_id << 8)


BASE_MANIFEST_NAME = "_base_manifest.json"

BASE_MANIFEST_FORMAT = "XY-BASE-MANIFEST-1"

HARDWARE_IDENTITY_NAME = "_legacy_hardware_identity.json"

HARDWARE_IDENTITY_FORMAT = "XY-LEGACY-HARDWARE-CCI-6"

BASE_MANIFEST_EXCLUDE_RELPATHS = {"romfs.bin"}

HASH_ALGO_COMPARE = "sha256"

IGNORE_CASE = False

IO_CHUNK = 8 * 1024 * 1024

BPS_LITERAL_CHUNK = 256 * 1024

DELTA_EXTEND_CHUNK = 1024 * 1024

DELTA_INDEX_MIN_BLOCK = 4

DELTA_INDEX_MAX_ENTRIES = 2 * 1024 * 1024

CODE_IPS_RELPATH = "exefs/code.ips"

CODE_BIN_RELPATH = "exefs/code.bin"

EXHEADER_RELPATH = "exheader.bin"

ICON_BIN_RELPATH = "exefs/icon.bin"

CRR_RELPATH = "romfs/.crr/static.crr"

EXHEADER_SIZE = 0x800

EXHEADER_SD_APPLICATION_FLAG_OFFSET = 0xD

EXHEADER_SAVE_SIZE_OFFSET = 0x1C0

EXHEADER_EXTDATA_ID_OFFSET = 0x230

EXHEADER_STORAGE_FLAGS_OFFSET = 0x24F

EXHEADER_SIGNED_DESCRIPTOR_OFFSET = 0x400

EXHEADER_SIGNED_PROGRAM_ID_OFFSET = 0x600

EXHEADER_ACTIVE_PERMISSIONS_OFFSET = 0x208

REMOVE_FROM_ROOT = ("logo_x00.png", "ncch.bin")

OS_METADATA_NAMES = {".ds_store", "desktop.ini", "thumbs.db"}

OS_METADATA_PREFIX = "._"

# These match ntpath.isreserved(), which needs Python 3.13 or newer.
WINDOWS_DEVICE_NAMES = {
    "AUX",
    "CON",
    "CONIN$",
    "CONOUT$",
    "NUL",
    "PRN",
    *(f"{port}{digit}" for port in ("COM", "LPT") for digit in "123456789¹²³"),
}

TITLE_ID_UNIQUE_MASK = 0x000000000FFFFF00
TITLE_ID_APPLICATION = 0x0004000000000000
TITLE_ID_KTR_BIT = 0x20000000
TITLE_ID_VARIATION_MASK = 0xFF
TITLE_ID_EXTENDED_BITS = TITLE_ID_KTR_BIT | TITLE_ID_VARIATION_MASK

TITLE_ID_PREFIX = "000400000"

TITLE_ID_SUFFIX = "00"

TITLE_ID_MIDDLE_LENGTH = 5

LEGACY_TITLE_ID_EXAMPLES = (("X Legacy", "E4C31"), ("Y Legacy", "E4C32"))

EXHEADER_JUMP_ID_OFFSET = 0x1C8

EXHEADER_PROGRAM_ID_OFFSET = 0x200

SMDH_SIZE = 0x36C0

SMDH_TITLE_COUNT = 16

SMDH_TITLE_ENTRY_SIZE = 0x200

SMDH_TITLES_OFFSET = 0x8

SMDH_ENGLISH_TITLE_INDEX = 1

SMDH_SHORT_NAME_SIZE = 0x80

SMDH_LONG_NAME_SIZE = 0x100

GAME_NAME_MAX_LENGTH = SMDH_SHORT_NAME_SIZE // 2 - 1

GARC_PATCH_MIN_SOURCE_SIZE = 64 * 1024 * 1024

SEVENZIP_LEVEL = 9

SEVENZIP_DICTIONARY_MB = 265

SEVENZIP_WORD_SIZE = 64

PATCH_ARCHIVE_MARKER = ".patch_archive_root"

XOR_CHUNK = 256 * 1024

GARC_PATCH_FORMAT = "XY-GARC-PATCH-2"

GARC_PATCH_EXT = ".garcpatch"

ROM_EXTS = {".3ds", ".cci"}

WORKDIR_PREFIX = "GamePatcher_"

STALE_WORKDIR_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

CTRTOOL_NAME = "ctrtool"

MAKEROM_NAME = "makerom"

PATCHES_ROOT_DIR = "patches"

PATCH_ARCHIVE_NAMES = ("exefs.7z", "romfs.7z")

PATCHSET_ZIP_EXT = ".zip"

BASE_VALIDATION_IGNORE_RELPATHS = {"romfs.bin"}

GARC_PATCH_WORKERS = 0

GARC_PATCH_MAX_AUTO_WORKERS = 6

GARC_PATCH_MEMORY_BUDGET_MB = 2048

ROMFS_HASH_WORKERS = 0

ROMFS_HASH_MAX_AUTO_WORKERS = 2

ROMFS_HASH_PARALLEL_MIN_BYTES = 256 * 1024 * 1024

ROMFS_HASH_BATCH_BYTES = 8 * 1024 * 1024

REQUIRED_FREE_BYTES = 7 * 1024 * 1024 * 1024

OUTPUT_FREE_MIN_BYTES = 1024 * 1024 * 1024

OUTPUT_FREE_MARGIN_BYTES = 512 * 1024 * 1024


ROMFS_BLOCK_SIZE = 4096

ROMFS_UNUSED_ENTRY = 4294967295

ROMFS_IVFC_HEADER_SIZE = 92

ROMFS_IVFC_HEADER_ALIGNED_SIZE = 96

CCI_HEADER_READ_SIZE = 5376

ORIGINAL_CARDINFO_RANGES = ((4096, 4156), (5120, 5136))

CACHE_PAGE_SIZE = 0x2000

ROMFS_INFO_HEADER_SIZE = 0x28

IVFC_HEADER_SIZE = 0x5C

MODEL_GARC_PATH = "a/0/0/7"


XY_GAMES = {0x0055D: "X", 0x0055E: "Y"}

TOOLS_DIR = "tools"
