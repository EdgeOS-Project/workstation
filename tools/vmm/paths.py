"""Shared filesystem locations for the standalone Workstation repository."""

from __future__ import annotations

import os
from pathlib import Path


WORKSTATION_ROOT = Path(__file__).resolve().parents[2]


def _configured_path(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser().resolve() if value else default.resolve()


KERNEL_ROOT = _configured_path(
    "EDGEOS_KERNEL_DIR", WORKSTATION_ROOT.parent / "kernel"
)
DISTRIBUTION_ROOT = _configured_path(
    "EDGEOS_DISTRIBUTION_DIR", WORKSTATION_ROOT.parent / "distribution"
)
STATE_ROOT = _configured_path(
    "EDGEOS_WORKSTATION_STATE_DIR", WORKSTATION_ROOT / ".edgeos-vms"
)

