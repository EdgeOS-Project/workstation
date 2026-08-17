#!/usr/bin/env python3
"""Launch EdgeOS Workstation with an explicit Qt window-system backend."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


GUI = Path(__file__).resolve().with_name("edgeos_vm_gui.py")


def available_platform_plugins() -> set[str]:
    if sys.platform == "darwin":
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        for prefix in (Path("/opt/homebrew/opt/pyqt"), Path("/usr/local/opt/pyqt")):
            site_packages = prefix / "lib" / version / "site-packages"
            if site_packages.is_dir() and str(site_packages) not in sys.path:
                sys.path.insert(0, str(site_packages))
    try:
        from PyQt6.QtCore import QLibraryInfo
    except ImportError:
        try:
            from PySide6.QtCore import QLibraryInfo
        except ImportError:
            return set()
    plugin_root = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)) / "platforms"
    names: set[str] = set()
    for plugin in plugin_root.glob("*q*.dylib") if plugin_root.is_dir() else []:
        name = plugin.stem.removeprefix("libq").removesuffix("Plugin").lower()
        names.add(name)
    for plugin in plugin_root.glob("libq*.so") if plugin_root.is_dir() else []:
        names.add(plugin.stem.removeprefix("libq"))
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=["auto", "cocoa", "xcb", "offscreen"], default="auto")
    parser.add_argument("--list-platforms", action="store_true")
    args = parser.parse_args()
    platforms = available_platform_plugins()
    if args.list_platforms:
        print("\n".join(sorted(platforms)))
        return 0
    platform = args.platform
    if platform == "auto":
        platform = "cocoa" if sys.platform == "darwin" else "xcb"
    if platforms and platform not in platforms:
        available = ", ".join(sorted(platforms)) or "none"
        print(
            f"Qt platform plugin {platform!r} is unavailable in this Python/Qt build. "
            f"Available plugins: {available}.",
            file=sys.stderr,
        )
        if sys.platform == "darwin" and platform == "xcb":
            print("An X11 launch requires an xcb-enabled Qt build plus XQuartz; Homebrew's native macOS PyQt build normally provides Cocoa only.", file=sys.stderr)
        return 2
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = platform
    return subprocess.call([sys.executable, str(GUI)], env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
