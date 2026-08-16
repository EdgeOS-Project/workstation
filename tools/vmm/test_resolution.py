#!/usr/bin/env python3
"""Regression tests for EdgeOS Workstation display resolution handling."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

import edgeos_vm
from edgeos_vm import VmError, normalize_display_resolution
from vm_schema import CURRENT_CONFIG_VERSION, ConfigError, migrate_config


class ResolutionValidationTests(unittest.TestCase):
    def test_accepts_presets_and_custom_modes(self) -> None:
        self.assertEqual(normalize_display_resolution("2560x1440"), "2560x1440")
        self.assertEqual(normalize_display_resolution("3440X1440"), "3440x1440")
        self.assertEqual(normalize_display_resolution("3840×2160"), "3840x2160")

    def test_rejects_dimensions_outside_safe_limits(self) -> None:
        with self.assertRaisesRegex(VmError, "display width"):
            normalize_display_resolution("8000x4320")
        with self.assertRaisesRegex(VmError, "display height"):
            normalize_display_resolution("1920x5000")

    def test_schema_accepts_maximum_safe_mode(self) -> None:
        config, changed = migrate_config(
            {
                "config_version": CURRENT_CONFIG_VERSION,
                "architecture": "arm64",
                "cpus": 2,
                "memory": "2G",
                "networks": [],
                "storage": [],
                "usb_devices": [],
                "shared_folders": [],
                "display_resolution": "7680x4320",
                "build_jobs": 0,
            }
        )
        self.assertFalse(changed)
        self.assertEqual(config["display_resolution"], "7680x4320")

    def test_arm64_resolution_accepts_legacy_gpu_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "architecture": "arm64",
                "gpu": "virtio-gpu-gl-pci",
                "display_resolution": "800x600",
                "uefi_path": "missing.img",
                "_dir": directory,
            }
            with (
                mock.patch.object(edgeos_vm, "load_vm", return_value=config),
                mock.patch.object(edgeos_vm, "save_vm") as save_vm,
                mock.patch.object(edgeos_vm, "running_pid", return_value=None),
            ):
                edgeos_vm.command_set_resolution(
                    SimpleNamespace(name="arm-test", resolution="1920x1080")
                )
            self.assertEqual(config["display_resolution"], "1920x1080")
            save_vm.assert_called_once_with("arm-test", config)


if __name__ == "__main__":
    unittest.main()
