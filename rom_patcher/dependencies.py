"""Archive dependency loading for source and bundled applications."""

from __future__ import annotations

import sys
import types

if sys.platform == "darwin":
    # py7zr uses this estimate to size archive buffers without a native wheel.
    memory_module = types.ModuleType("psutil")

    def virtual_memory():
        return types.SimpleNamespace(available=8 * 1024 * 1024 * 1024)

    memory_module.virtual_memory = virtual_memory
    sys.modules["psutil"] = memory_module

try:
    import py7zr
except ImportError:
    py7zr = None


def require_py7zr():
    if py7zr is not None:
        return py7zr
    raise RuntimeError(
        "The archive dependency is missing. "
        "Run the build script to install the project requirements."
    )
