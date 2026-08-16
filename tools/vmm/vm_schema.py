#!/usr/bin/env python3
"""Versioned EdgeOS Workstation virtual machine configuration schema.

This is original EdgeOS code licensed under MPL-2.0.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any


CURRENT_CONFIG_VERSION = 6
MAX_BUILD_JOBS = 256
SUPPORTED_DISPLAY_RESOLUTIONS = (
    "640x480",
    "800x600",
    "1024x768",
    "1280x720",
    "1280x800",
    "1366x768",
    "1440x900",
    "1600x900",
    "1920x1080",
    "2560x1440",
    "3840x2160",
)
MIN_DISPLAY_WIDTH = 320
MIN_DISPLAY_HEIGHT = 200
MAX_DISPLAY_WIDTH = 7680
MAX_DISPLAY_HEIGHT = 4320
MAX_DISPLAY_FRAMEBUFFER_BYTES = 128 * 1024 * 1024
DISPLAY_RESOLUTION_RE = re.compile(r"^(\d{2,5})[xX×](\d{2,5})$")


class ConfigError(RuntimeError):
    """A VM configuration is malformed or uses an unsupported schema."""


def migrate_config(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    config = dict(raw)
    version = config.get("config_version", 1)
    if not isinstance(version, int) or version < 1:
        raise ConfigError("config_version must be a positive integer")
    if version > CURRENT_CONFIG_VERSION:
        raise ConfigError(
            f"configuration version {version} is newer than supported version "
            f"{CURRENT_CONFIG_VERSION}"
        )
    changed = False
    if version == 1:
        cpus = _positive_int(config.get("cpus", 4), "cpus")
        config.setdefault("cpu_sockets", 1)
        config.setdefault("cpu_cores", cpus)
        config.setdefault("cpu_threads", 1)
        config.setdefault("firmware_mode", "auto")
        config.setdefault("boot_order", "d")
        config.setdefault("boot_menu", True)
        config.setdefault("boot_delay_ms", 0)
        config.setdefault("rtc_base", "utc")
        config.setdefault("sound", "none")
        config.setdefault("balloon", False)
        config.setdefault("rng", False)
        config.setdefault("disk_read_only", False)
        config.setdefault("cdrom_connected", True)
        config["config_version"] = 2
        version = 2
        changed = True
    if version == 2:
        normalized_storage: list[dict[str, Any]] = []
        for index, disk in enumerate(config.get("storage", []), start=1):
            item = dict(disk)
            item.setdefault("name", f"Hard Disk {index + 1}")
            item.setdefault("format", "qcow2" if str(item.get("path", "")).endswith(".qcow2") else "raw")
            item.setdefault("controller", config.get("disk_controller", "virtio-blk"))
            item.setdefault("cache", config.get("disk_cache", "none"))
            item.setdefault("aio", config.get("disk_aio", "native"))
            item.setdefault("discard", config.get("disk_discard", "unmap"))
            item.setdefault("read_only", False)
            normalized_storage.append(item)
        config["storage"] = normalized_storage
        config.setdefault("usb_devices", [])
        config["config_version"] = 3
        version = 3
        changed = True
    if version == 3:
        config.setdefault("shared_folders", [])
        config["config_version"] = 4
        version = 4
        changed = True
    if version == 4:
        config.setdefault("display_resolution", "800x600")
        config["config_version"] = 5
        version = 5
        changed = True
    if version == 5:
        config.setdefault("build_jobs", 0)
        config["config_version"] = 6
        version = 6
        changed = True
    validate_config(config)
    return config, changed


def validate_config(config: dict[str, Any]) -> None:
    _positive_int(config.get("cpus", 4), "cpus")
    _positive_int(config.get("cpu_sockets", 1), "cpu_sockets")
    _positive_int(config.get("cpu_cores", config.get("cpus", 4)), "cpu_cores")
    _positive_int(config.get("cpu_threads", 1), "cpu_threads")
    build_jobs = config.get("build_jobs", 0)
    if (
        isinstance(build_jobs, bool)
        or not isinstance(build_jobs, int)
        or not 0 <= build_jobs <= MAX_BUILD_JOBS
    ):
        raise ConfigError(
            f"build_jobs must be an integer between 0 and {MAX_BUILD_JOBS}"
        )
    memory = config.get("memory", "2048M")
    if not isinstance(memory, str) or not memory.strip():
        raise ConfigError("memory must be a non-empty QEMU memory string")
    architecture = config.get("architecture", "x86_64")
    if architecture not in ("x86_64", "arm64", "aarch64"):
        raise ConfigError(f"unsupported architecture: {architecture!r}")
    networks = config.get("networks", [])
    if not isinstance(networks, list):
        raise ConfigError("networks must be a list")
    for index, network in enumerate(networks):
        if not isinstance(network, dict):
            raise ConfigError(f"networks[{index}] must be an object")
        if not isinstance(network.get("type", "user"), str):
            raise ConfigError(f"networks[{index}].type must be a string")
        if network.get("type", "user") not in ("user", "tap", "bridge", "macvtap", "macvlan", "socket", "none"):
            raise ConfigError(f"networks[{index}].type is unsupported")
    storage = config.get("storage", [])
    if not isinstance(storage, list):
        raise ConfigError("storage must be a list")
    for index, disk in enumerate(storage):
        if not isinstance(disk, dict) or not isinstance(disk.get("path"), str) or not disk["path"].strip():
            raise ConfigError(f"storage[{index}] must contain a string path")
        if disk.get("format", "raw") not in ("raw", "qcow2"):
            raise ConfigError(f"storage[{index}].format must be raw or qcow2")
        if not isinstance(disk.get("controller", "virtio-blk"), str):
            raise ConfigError(f"storage[{index}].controller must be a string")
    usb_devices = config.get("usb_devices", [])
    if not isinstance(usb_devices, list):
        raise ConfigError("usb_devices must be a list")
    for index, device in enumerate(usb_devices):
        if not isinstance(device, dict):
            raise ConfigError(f"usb_devices[{index}] must be an object")
        for key in ("vendor_id", "product_id"):
            value = device.get(key)
            if not isinstance(value, str) or not _valid_usb_id(value):
                raise ConfigError(f"usb_devices[{index}].{key} must be a four-digit hexadecimal ID")
    shared_folders = config.get("shared_folders", [])
    if not isinstance(shared_folders, list):
        raise ConfigError("shared_folders must be a list")
    for index, folder in enumerate(shared_folders):
        if not isinstance(folder, dict):
            raise ConfigError(f"shared_folders[{index}] must be an object")
        tag = folder.get("tag")
        path = folder.get("path")
        if not isinstance(tag, str) or not tag or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in tag):
            raise ConfigError(f"shared_folders[{index}].tag is invalid")
        if not isinstance(path, str) or not path.strip() or "," in path:
            raise ConfigError(f"shared_folders[{index}].path must be a non-empty path without commas")
    if config.get("firmware_mode", "auto") not in ("auto", "uefi", "bios"):
        raise ConfigError("firmware_mode must be auto, uefi, or bios")
    if config.get("rtc_base", "utc") not in ("utc", "localtime"):
        raise ConfigError("rtc_base must be utc or localtime")
    normalize_display_resolution(config.get("display_resolution", "800x600"))


def normalize_display_resolution(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("display_resolution must be a WIDTHxHEIGHT string")
    match = DISPLAY_RESOLUTION_RE.fullmatch(value.strip())
    if not match:
        raise ConfigError("display_resolution must use WIDTHxHEIGHT format")
    width = int(match.group(1))
    height = int(match.group(2))
    if not (MIN_DISPLAY_WIDTH <= width <= MAX_DISPLAY_WIDTH):
        raise ConfigError(
            f"display width must be between {MIN_DISPLAY_WIDTH} and {MAX_DISPLAY_WIDTH}"
        )
    if not (MIN_DISPLAY_HEIGHT <= height <= MAX_DISPLAY_HEIGHT):
        raise ConfigError(
            f"display height must be between {MIN_DISPLAY_HEIGHT} and {MAX_DISPLAY_HEIGHT}"
        )
    if width * height * 4 > MAX_DISPLAY_FRAMEBUFFER_BYTES:
        raise ConfigError("display framebuffer must not exceed 128 MiB")
    return f"{width}x{height}"


def effective_gpu(config: dict[str, Any]) -> str:
    """Return the display device that the QEMU command will actually attach."""
    architecture = str(config.get("architecture", "x86_64"))
    if architecture in ("arm64", "aarch64"):
        return "ramfb"
    return str(config.get("gpu", "std"))


def supports_boot_display_resolution(config: dict[str, Any]) -> bool:
    """Return whether UEFI can apply the configured boot display resolution."""
    architecture = str(config.get("architecture", "x86_64"))
    return (
        architecture in ("arm64", "aarch64")
        and effective_gpu(config) == "ramfb"
    )


def load_config(path: Path, *, migrate: bool = True) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"unable to load {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"configuration root in {path} must be an object")
    source_version = raw.get("config_version", 1)
    config, changed = migrate_config(raw)
    if changed and migrate:
        backup = path.with_name(f"{path.name}.v{source_version}.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        save_config(path, config)
    return config


def save_config(path: Path, config: dict[str, Any]) -> None:
    data = dict(config)
    data["config_version"] = CURRENT_CONFIG_VERSION
    validate_config(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(path)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _valid_usb_id(value: str) -> bool:
    if len(value) != 4:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value)
