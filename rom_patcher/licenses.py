"""View and save the license materials included in each executable."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from .runtime import bundle_dir

LICENSE_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.txt", "SOURCES.zip")


def license_folder() -> Path:
    resources = bundle_dir()
    if getattr(sys, "frozen", False):
        return resources / "licenses"
    return resources / "build" / "licenses"


def export_licenses(destination: Path) -> Path:
    materials = license_folder()
    for name in LICENSE_FILES:
        if not (materials / name).is_file():
            raise FileNotFoundError(f"License material is missing: {name}")
    output = Path(
        tempfile.mkdtemp(prefix="Legacy_Patcher_Licenses_", dir=destination)
    )
    try:
        for name in LICENSE_FILES:
            shutil.copy2(materials / name, output / name)
    except OSError:
        shutil.rmtree(output)
        raise
    return output


def show_licenses(parent=None) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    window = tk.Toplevel(parent) if parent is not None else tk.Tk()
    window.title("Legacy Patcher Licenses")
    window.geometry("760x560")
    text = ScrolledText(window, wrap="word", width=90, height=28)
    text.pack(fill="both", expand=True, padx=12, pady=12)
    try:
        materials = license_folder()
        content = "\n\n".join(
            (materials / name).read_text(encoding="utf-8")
            for name in LICENSE_FILES[:2]
        )
    except OSError:
        content = (
            "License materials are included in compiled programs. "
            "Build both programs to prepare them for source use."
        )
    text.insert("1.0", content)
    text.configure(state="disabled")

    def save():
        selected = filedialog.askdirectory(
            title="Choose a folder for licenses and library sources",
            mustexist=True,
            parent=window,
        )
        if not selected:
            return
        try:
            output = export_licenses(Path(selected))
        except OSError as error:
            messagebox.showerror(
                "Could not save licenses", str(error), parent=window
            )
            return
        messagebox.showinfo(
            "Licenses saved",
            f"Licenses and library source archives:\n\n{output}",
            parent=window,
        )

    buttons = ttk.Frame(window)
    buttons.pack(fill="x", padx=12, pady=(0, 12))
    ttk.Button(buttons, text="Save licenses and source...", command=save).pack(
        side="left"
    )
    ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")
    if parent is None:
        window.mainloop()
