#!/usr/bin/env python3
"""Build a local macOS application bundle for EdgeOS Workstation."""

from __future__ import annotations

import argparse
import plistlib
import shutil
import stat
from pathlib import Path

from paths import WORKSTATION_ROOT


REPO_ROOT = WORKSTATION_ROOT


def build_bundle(destination: Path) -> Path:
    bundle = destination / "EdgeOS Workstation.app"
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    if bundle.exists():
        shutil.rmtree(bundle)
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleDevelopmentRegion": "en",
                "CFBundleDisplayName": "EdgeOS Workstation",
                "CFBundleExecutable": "edgeos-workstation",
                "CFBundleIdentifier": "org.edgeos.workstation",
                "CFBundleInfoDictionaryVersion": "6.0",
                "CFBundleName": "EdgeOS Workstation",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "0.4.0",
                "CFBundleVersion": "4",
                "LSMinimumSystemVersion": "13.0",
                "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
            },
            stream,
            sort_keys=True,
        )

    repository_link = resources / "edgeos"
    repository_link.symlink_to(REPO_ROOT)
    launcher = macos / "edgeos-workstation"
    launcher.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "REPO=$(CDPATH= cd -- \"$(dirname \"$0\")/../Resources/edgeos\" && pwd)\n"
        "for PYTHON in /opt/homebrew/opt/python@3.14/bin/python3.14 /opt/homebrew/bin/python3 python3; do\n"
        "    if command -v \"$PYTHON\" >/dev/null 2>&1; then\n"
        "        exec \"$PYTHON\" \"$REPO/tools/vmm/edgeos_vm_gui.py\"\n"
        "    fi\n"
        "done\n"
        "osascript -e 'display alert \"Python 3 is required to run EdgeOS Workstation.\"'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "dist")
    args = parser.parse_args()
    bundle = build_bundle(args.output.expanduser().resolve())
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
