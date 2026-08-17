#!/usr/bin/env python3
"""EdgeOS virtual machine manager.

The manager keeps VM rootfs images separate from kernel build artifacts so a
new kernel can be compiled and booted without recreating user data.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
from pathlib import Path
from typing import Any

from qmp_client import QmpClient, QmpError, legacy_hmp_command, wait_for_qmp
from vm_schema import (
    ConfigError,
    load_config,
    normalize_display_refresh_hz as normalize_schema_display_refresh_hz,
    normalize_display_resolution as normalize_schema_display_resolution,
    save_config,
    supports_boot_display_resolution,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = REPO_ROOT / ".edgeos-vms"
TEMPLATES_DIR = STATE_DIR / "templates"
INSTANCES_DIR = STATE_DIR / "instances"
GUEST_TOOLS_SCRIPT = Path(__file__).resolve().with_name("edgeos_guest_tools.sh")
MIN_BOOT_MEMORY_MB = 2048
MEMORY_RE = re.compile(r"^(\d+)([KkMmGg]?)$")


DEFAULTS: dict[str, Any] = {
    "architecture": "x86_64",
    "profile": "edgeos",
    "cpus": 4,
    "cpu_sockets": 1,
    "cpu_cores": 4,
    "cpu_threads": 1,
    "build_jobs": 0,
    "memory": "2048M",
    "machine": "pc,accel=kvm",
    "cpu_model": "host,migratable=off",
    "kvm": True,
    "rootfs_size_mb": 512,
    "disk_controller": "nvme",
    "disk_model": "edgeos-rootfs",
    "disk_cache": "none",
    "disk_aio": "native",
    "disk_discard": "unmap",
    "disk_read_only": False,
    "networks": [{"type": "user", "model": "e1000"}],
    "usb": "virtio-input",
    "gpu": "std",
    "virgl": False,
    "display_backend": "default",
    "display_resolution": "800x600",
    "display_refresh_hz": 60,
    "firmware_mode": "auto",
    "boot_order": "d",
    "boot_menu": True,
    "boot_delay_ms": 0,
    "rtc_base": "utc",
    "sound": "none",
    "balloon": False,
    "rng": False,
    "boot_params": ["console=ttyS0"],
    "qemu_args": [],
    "passthrough": [],
    "storage": [],
    "rootfs_path": "rootfs.img",
    "iso_path": "build/out/edgeos-vm.iso",
    "uefi_path": "build/out/edgeos-arm64-uefi.img",
    "obj_dir": "build/obj",
    "out_dir": "build/out",
}

SERIAL_MARKED_COMMAND_DELAY = 0.0005
SERIAL_HEREDOC_DELAY = 0.001
SERIAL_HEREDOC_LINE_DELAY = 0.01


TEMPLATE_DEFAULTS: dict[str, dict[str, Any]] = {
    "edgeos": {
        "profile": "edgeos",
        "memory": "2048M",
        "rootfs_size_mb": 512,
        "boot_params": ["console=ttyS0"],
    },
    "alpine": {
        "profile": "alpine",
        "memory": "2048M",
        # XFCE plus development and diagnostic tools can exceed 2 GiB after
        # package caches and upgrades. Keep enough headroom for desktop use.
        "rootfs_size_mb": 4096,
        "boot_params": ["console=ttyS0"],
    },
    "debian": {
        "profile": "debian",
        "memory": "4096M",
        "rootfs_size_mb": 8192,
        # EdgeOS currently exposes fbdev rather than DRM/KMS.  systemd's seat
        # rules recognize that real fallback mode through the Linux
        # nomodeset command-line contract.
        "boot_params": [
            "console=tty0",
            "console=ttyS0",
            "nomodeset",
            "quiet",
            "splash",
            "plymouth.ignore-serial-consoles",
        ],
    },
    "arm64": {
        "architecture": "arm64",
        "profile": "alpine",
        "memory": "8192M",
        "machine": "virt,gic-version=3,acpi=off",
        "cpu_model": "host",
        "accelerator": "hvf",
        "kvm": False,
        # Persistent ARM64 guests attach rootfs as a separate virtio disk;
        # only the UEFI loader remains on the FAT32 ESP.
        "rootfs_size_mb": 8192,
        "disk_controller": "virtio-mmio",
        "networks": [{"type": "user", "model": "virtio-net-device"}],
        "usb": "virtio-input",
        "gpu": "ramfb",
        "display_backend": "default",
        "display_resolution": "800x600",
        "display_refresh_hz": 60,
        "boot_params": [
            "console=tty1",
            "nomodeset",
            "quiet",
            "splash",
            "plymouth.ignore-serial-consoles",
        ],
    },
    "tap": {
        "networks": [{"type": "tap", "ifname": "tap0", "model": "e1000"}],
    },
    "bridge": {
        "networks": [{"type": "bridge", "bridge": "br0", "model": "e1000"}],
    },
    "macvtap": {
        "networks": [{"type": "macvtap", "ifname": "edge-macvtap0", "model": "e1000"}],
    },
}


SUDO_PASSWORD = "2294001xyj"
SUDO_REEXEC_ENV = "EDGEOS_VM_SUDO_REEXEC"
DEFAULT_GUEST_ROOT_PASSWORD_HASH = (
    "$6$edgeos$fBGq3tzKqj1d/Mx7YFi1bR0TF0bggz7XwW6PBmucCNFAQA97VvO95xxyFBL4ENOhKcdxohhDO99GCByHvdluA."
)


class VmError(RuntimeError):
    pass


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def run(
    cmd: list[str],
    *,
    cwd: Path = REPO_ROOT,
    check: bool = True,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=str(cwd), text=True, check=check, pass_fds=pass_fds)


def need_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for prefix in (
        "/usr/sbin", "/sbin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
        "/opt/homebrew/opt/e2fsprogs/bin", "/opt/homebrew/opt/e2fsprogs/sbin",
        "/usr/local/bin", "/usr/local/sbin",
        "/usr/local/opt/e2fsprogs/bin", "/usr/local/opt/e2fsprogs/sbin",
    ):
        candidate = Path(prefix) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise VmError(f"required tool not found: {name}")


def need_any_tool(*names: str) -> str:
    for name in names:
        try:
            return need_tool(name)
        except VmError:
            continue
    raise VmError(f"required tool not found: {' or '.join(names)}")


def parse_memory_mb(value: str) -> int | None:
    match = MEMORY_RE.match(value.strip())
    if not match:
        return None
    amount = int(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "g":
        return amount * 1024
    if suffix == "k":
        return max(1, amount // 1024)
    return amount


def qemu_memory_value(value: str) -> str:
    mb = parse_memory_mb(value)
    if mb is None:
        return value
    # GRUB can fail before the kernel is loaded with "out of memory" on the
    # current VM ISO/kernel layout when started with 1024M. Keep the VMM floor
    # high enough that users get kernel and OpenRC logs instead of a stale GRUB
    # window with the real error hidden on serial.
    if mb < MIN_BOOT_MEMORY_MB:
        return f"{MIN_BOOT_MEMORY_MB}M"
    return value


def ensure_state() -> None:
    STATE_DIR.mkdir(exist_ok=True)
    TEMPLATES_DIR.mkdir(exist_ok=True)
    INSTANCES_DIR.mkdir(exist_ok=True)


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def instance_dir(name: str) -> Path:
    if not name or "/" in name or name in (".", ".."):
        raise VmError("VM name must be a simple path component")
    return INSTANCES_DIR / name


def config_path(name: str) -> Path:
    return instance_dir(name) / "vm.json"


def load_vm(name: str) -> dict[str, Any]:
    path = config_path(name)
    if not path.is_file():
        raise VmError(f"VM not found: {name}")
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        raise VmError(str(exc)) from exc
    cfg["_name"] = name
    cfg["_dir"] = str(instance_dir(name))
    return cfg


def save_vm(name: str, cfg: dict[str, Any]) -> None:
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    try:
        save_config(config_path(name), data)
    except ConfigError as exc:
        raise VmError(str(exc)) from exc


def vm_path(cfg: dict[str, Any], key: str) -> Path:
    value = Path(str(cfg[key]))
    if value.is_absolute():
        return value
    return Path(str(cfg["_dir"])) / value


def template_config(name: str) -> dict[str, Any]:
    if name in TEMPLATE_DEFAULTS:
        return TEMPLATE_DEFAULTS[name]
    path = TEMPLATES_DIR / f"{name}.json"
    if path.is_file():
        return load_json(path)
    raise VmError(f"template not found: {name}")


def apply_templates(names: list[str]) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    for name in names:
        cfg = deep_merge(cfg, template_config(name))
    return cfg


def make_args(cfg: dict[str, Any]) -> list[str]:
    if cfg.get("architecture", "x86_64") in ("arm64", "aarch64"):
        return [
            f"OUT={vm_path(cfg, 'out_dir')}",
            f"ARM64_ROOTFS_SIZE_MB={cfg['rootfs_size_mb']}",
        ]
    return [
        "ARCH=x86",
        "SRCARCH=x86",
        "ROOTFS_ARCH=x86_64",
        f"OBJ={vm_path(cfg, 'obj_dir')}",
        f"OUT={vm_path(cfg, 'out_dir')}",
        f"ROOTFS_PROFILE={cfg['profile']}",
        f"ROOTFS_SIZE_MB={cfg['rootfs_size_mb']}",
    ]


def resolve_build_jobs(cfg: dict[str, Any]) -> int:
    """Return the effective bounded make parallelism for a VM build."""
    configured = cfg.get("build_jobs", 0)
    if isinstance(configured, bool) or not isinstance(configured, int):
        raise VmError("build_jobs must be an integer")
    if not 0 <= configured <= 256:
        raise VmError("build_jobs must be between 0 and 256")
    if configured:
        return configured
    return min(256, max(1, os.cpu_count() or 1))


def run_make(cfg: dict[str, Any], arguments: list[str]) -> None:
    jobs = resolve_build_jobs(cfg)
    make_program = "make"

    # Apple's make spends minutes in implicit-rule search for the generated
    # BSD bridge dependency graph after compilation has completed.  GNU make
    # evaluates the same graph in seconds and is already the supported build
    # tool on non-macOS hosts, so prefer it when Homebrew provides it.
    if platform.system() == "Darwin":
        make_program = shutil.which("gmake") or make_program
    print(f"[build] using {jobs} parallel job{'s' if jobs != 1 else ''}", flush=True)
    run([make_program, f"-j{jobs}", *arguments])


def build_kernel(cfg: dict[str, Any], reconfigure: bool = False) -> None:
    out_dir = vm_path(cfg, "out_dir")
    obj_dir = vm_path(cfg, "obj_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    obj_dir.mkdir(parents=True, exist_ok=True)
    args = make_args(cfg)
    if cfg.get("architecture", "x86_64") in ("arm64", "aarch64"):
        efi = out_dir / "arm64" / "BOOTAA64.EFI"
        run_make(cfg, [*args, str(efi)])
        write_arm64_uefi_image(cfg, efi, vm_path(cfg, "rootfs_path"))
        return
    if reconfigure or not (REPO_ROOT / ".config").is_file():
        run_make(cfg, [*args, "x86_64_defconfig"])
    run_make(cfg, [*args, "olddefconfig"])
    run_make(cfg, [*args, str(out_dir / "edgeos.bin")])
    write_boot_iso(cfg)


def build_rootfs(cfg: dict[str, Any]) -> Path:
    out_dir = vm_path(cfg, "out_dir")
    architecture = cfg.get("architecture", "x86_64")
    if cfg["profile"] == "debian":
        normalized_architecture = (
            "arm64" if architecture in ("arm64", "aarch64") else "x86_64"
        )
        rootfs = out_dir / f"rootfs-debian-{normalized_architecture}.img"
        run([
            sys.executable,
            str(REPO_ROOT / "tools" / "rootfs" / "build_debian_rootfs.py"),
            "--architecture", normalized_architecture,
            "--size-mb", str(cfg["rootfs_size_mb"]),
            "--output", str(rootfs),
            "--rootfs-dir", str(REPO_ROOT / "rootfs" / "debian" / normalized_architecture),
        ])
        if not rootfs.is_file():
            raise VmError(f"rootfs build did not produce {rootfs}")
        return rootfs
    if architecture in ("arm64", "aarch64"):
        rootfs = out_dir / "rootfs-alpine-arm64.img"
        run_make(cfg, [*make_args(cfg), str(rootfs)])
        if not rootfs.is_file():
            raise VmError(f"rootfs build did not produce {rootfs}")
        return rootfs
    rootfs_name = "rootfs-alpine.img" if cfg["profile"] == "alpine" else "rootfs.img"
    run_make(cfg, [*make_args(cfg), str(out_dir / rootfs_name)])
    built = out_dir / rootfs_name
    if not built.is_file():
        raise VmError(f"rootfs build did not produce {built}")
    return built


def sanitize_hostname(value: str) -> str:
    out = []
    last_dash = False
    for ch in value.strip().lower():
        ok = ("a" <= ch <= "z") or ("0" <= ch <= "9")
        if ok:
            out.append(ch)
            last_dash = False
        elif ch in ("-", "_", ".", " "):
            if out and not last_dash:
                out.append("-")
                last_dash = True
    host = "".join(out).strip("-")
    if not host:
        host = "edgeos-vm"
    if len(host) > 63:
        host = host[:63].rstrip("-") or "edgeos-vm"
    return host


def debugfs_write_text(image: Path, dst: str, content: str, mode: int | None = None) -> None:
    debugfs = need_tool("debugfs")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        subprocess.run([debugfs, "-w", "-R", f"rm {dst}", str(image)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run([debugfs, "-w", "-R", f"write {tmp_path} {dst}", str(image)])
        if mode is not None:
            run([debugfs, "-w", "-R", f"sif {dst} mode 010{mode:04o}", str(image)])
    finally:
        tmp_path.unlink(missing_ok=True)


def debugfs_mkdir_p(image: Path, path: str) -> None:
    debugfs = need_tool("debugfs")
    current = ""
    for component in path.strip("/").split("/"):
        if not component:
            continue
        current += f"/{component}"
        subprocess.run(
            [debugfs, "-w", "-R", f"mkdir {current}", str(image)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def debugfs_read_text(image: Path, src: str) -> str:
    debugfs = need_tool("debugfs")
    result = subprocess.run(
        [debugfs, "-R", f"cat {src}", str(image)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or "File not found" in result.stderr:
        raise VmError(f"unable to read {src} from {image}")
    return result.stdout


def debugfs_path_exists(image: Path, path: str) -> bool:
    debugfs = need_tool("debugfs")
    result = subprocess.run(
        [debugfs, "-R", f"stat {path}", str(image)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    output = result.stdout + result.stderr
    return result.returncode == 0 and "File not found" not in output


def configure_rootfs_identity(image: Path, hostname: str) -> None:
    host = sanitize_hostname(hostname)
    hosts = (
        f"127.0.1.1\t{host}\n"
        "127.0.0.1\tlocalhost localhost.localdomain\n"
        "::1\t\tlocalhost localhost.localdomain\n"
    )
    debugfs_write_text(image, "/etc/hostname", host + "\n")
    debugfs_write_text(image, "/etc/hosts", hosts)


def configure_generated_rootfs_login(image: Path) -> None:
    shadow = debugfs_read_text(image, "/etc/shadow")
    output: list[str] = []
    found_root = False
    for line in shadow.splitlines():
        fields = line.split(":")
        if fields and fields[0] == "root":
            if len(fields) < 2:
                raise VmError("malformed root entry in generated /etc/shadow")
            fields[1] = DEFAULT_GUEST_ROOT_PASSWORD_HASH
            line = ":".join(fields)
            found_root = True
        output.append(line)
    if not found_root:
        raise VmError("generated rootfs has no root entry in /etc/shadow")
    debugfs_write_text(image, "/etc/shadow", "\n".join(output) + "\n", mode=0o600)


def generated_rootfs_needs_login_rewrite(profile: str) -> bool:
    """Return whether Workstation must set the legacy generated root login."""
    return profile != "debian"


def verify_rootfs_image(image: Path) -> None:
    e2fsck = need_tool("e2fsck")
    run([e2fsck, "-f", "-n", str(image)])


def ext_filesystem_size_bytes(image: Path) -> int:
    dumpe2fs = need_tool("dumpe2fs")
    command = [dumpe2fs, "-h", str(image)]
    print("+ " + " ".join(command))
    result = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise VmError(f"could not inspect ext filesystem capacity: {image}")
    block_count = re.search(r"(?m)^Block count:\s+(\d+)\s*$", result.stdout)
    block_size = re.search(r"(?m)^Block size:\s+(\d+)\s*$", result.stdout)
    if not block_count or not block_size:
        raise VmError(f"dumpe2fs returned no ext capacity for {image}")
    return int(block_count.group(1)) * int(block_size.group(1))


def repair_rootfs_image(image: Path) -> None:
    e2fsck = need_tool("e2fsck")
    result = run([e2fsck, "-f", "-y", str(image)], check=False)
    if result.returncode not in (0, 1):
        raise VmError(
            f"e2fsck could not repair {image}: exit status {result.returncode}"
        )


def ensure_rootfs_capacity(cfg: dict[str, Any]) -> bool:
    """Grow a raw ext filesystem to the configured VM capacity."""
    image = vm_path(cfg, "rootfs_path")
    target_mb = int(cfg.get("rootfs_size_mb", 0))
    target_bytes = target_mb * 1024 * 1024
    if target_mb <= 0 or not image.is_file():
        return False
    if str(cfg.get("rootfs_format", "raw")) != "raw":
        raise VmError(
            f"automatic filesystem growth requires a raw rootfs: {image}"
        )
    filesystem_bytes = ext_filesystem_size_bytes(image)
    if image.stat().st_size >= target_bytes and filesystem_bytes >= target_bytes:
        return False
    resize2fs = need_tool("resize2fs")
    previous_mb = filesystem_bytes // (1024 * 1024)
    if image.stat().st_size < target_bytes:
        with image.open("r+b") as rootfs:
            rootfs.truncate(target_bytes)
    repair_rootfs_image(image)
    run([resize2fs, str(image)])
    verify_rootfs_image(image)
    print(f"grew rootfs {rel(image)} from {previous_mb} MiB to {target_mb} MiB")
    return True


def write_boot_iso(cfg: dict[str, Any]) -> None:
    grub = need_any_tool("grub-mkrescue", "x86_64-elf-grub-mkrescue")
    out_dir = vm_path(cfg, "out_dir")
    kernel = out_dir / "edgeos.bin"
    if not kernel.is_file():
        raise VmError(f"kernel not found: {kernel}")

    iso = vm_path(cfg, "iso_path")
    iso_dir = out_dir / "vmm-isodir"
    boot_dir = iso_dir / "boot"
    grub_dir = boot_dir / "grub"
    if iso_dir.exists():
        shutil.rmtree(iso_dir)
    grub_dir.mkdir(parents=True)
    shutil.copy2(kernel, boot_dir / "edgeos.bin")
    cmdline = display_boot_command_line(cfg)
    (grub_dir / "grub.cfg").write_text(
        "serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1\n"
        "terminal_input serial console\n"
        "terminal_output serial console\n"
        "set timeout=0\n"
        "set default=0\n"
        "insmod all_video\n"
        "set gfxmode=1024x768x32,800x600x32,auto\n"
        "set gfxpayload=keep\n\n"
        'menuentry "EdgeOS VM persistent rootfs" {\n'
        f"    multiboot2 /boot/edgeos.bin {cmdline}\n"
        "    boot\n"
        "}\n",
        encoding="ascii",
    )
    iso.parent.mkdir(parents=True, exist_ok=True)
    run([grub, "-o", str(iso), str(iso_dir)])


def write_arm64_uefi_image(cfg: dict[str, Any], efi: Path, rootfs: Path) -> None:
    if not efi.is_file():
        raise VmError(f"ARM64 UEFI kernel not found: {efi}")
    if not rootfs.is_file():
        raise VmError(f"ARM64 rootfs not found: {rootfs}")
    mformat = need_tool("mformat")
    mmd = need_tool("mmd")
    mcopy = need_tool("mcopy")
    image = vm_path(cfg, "uefi_path")
    image.parent.mkdir(parents=True, exist_ok=True)
    efi_mb = max(1, (efi.stat().st_size + (1024 * 1024 - 1)) // (1024 * 1024))
    # VMM machines attach rootfs as a writable virtio block device. Keep the
    # ESP independent from guest data so writes survive reboot just as they do
    # on x86_64. Standalone ARM64 images may still embed rootfs.img separately.
    image_mb = max(384, efi_mb + 32)
    with image.open("wb") as out:
        out.truncate(image_mb * 1024 * 1024)
    run([mformat, "-i", str(image), "-F", "-v", "EDGEOSARM", "::"])
    run([mmd, "-i", str(image), "::/EFI", "::/EFI/BOOT", "::/boot"])
    run([mcopy, "-i", str(image), str(efi), "::/EFI/BOOT/BOOTAA64.EFI"])
    write_arm64_video_config(cfg, image)
    write_arm64_command_line(cfg, image)
    arm64_rootfs_dirty_marker(cfg).unlink(missing_ok=True)


def normalize_display_resolution(value: str) -> str:
    try:
        return normalize_schema_display_resolution(value)
    except ConfigError as exc:
        raise VmError(str(exc)) from exc


def normalize_display_refresh_hz(value: Any) -> int:
    try:
        return normalize_schema_display_refresh_hz(value)
    except ConfigError as exc:
        raise VmError(str(exc)) from exc


def display_boot_command_line(cfg: dict[str, Any]) -> str:
    resolution = normalize_display_resolution(
        str(cfg.get("display_resolution", "800x600"))
    )
    refresh_hz = normalize_display_refresh_hz(
        cfg.get("display_refresh_hz", 60)
    )
    parameters = [
        str(value)
        for value in cfg.get("boot_params", [])
        if not str(value).startswith("edgeos.video=")
    ]
    parameters.append(f"edgeos.video={resolution}@{refresh_hz}")
    return " ".join(parameters)


def write_arm64_video_config(cfg: dict[str, Any], image: Path | None = None) -> None:
    if cfg.get("architecture", "x86_64") not in ("arm64", "aarch64"):
        return
    target = image or vm_path(cfg, "uefi_path")
    if not target.is_file():
        raise VmError(f"ARM64 UEFI image not found: {target}")
    resolution = normalize_display_resolution(
        str(cfg.get("display_resolution", "800x600"))
    )
    mcopy = need_tool("mcopy")
    with tempfile.NamedTemporaryFile("w", encoding="ascii", delete=False) as stream:
        stream.write(resolution + "\n")
        temporary = Path(stream.name)
    try:
        run([mcopy, "-o", "-i", str(target), str(temporary), "::/boot/video.cfg"])
    finally:
        temporary.unlink(missing_ok=True)


def write_arm64_command_line(cfg: dict[str, Any], image: Path | None = None) -> None:
    if cfg.get("architecture", "x86_64") not in ("arm64", "aarch64"):
        return
    target = image or vm_path(cfg, "uefi_path")
    if not target.is_file():
        raise VmError(f"ARM64 UEFI image not found: {target}")
    command_line = display_boot_command_line(cfg)
    mcopy = need_tool("mcopy")
    with tempfile.NamedTemporaryFile("w", encoding="ascii", delete=False) as stream:
        stream.write(command_line + "\n")
        temporary = Path(stream.name)
    try:
        run([mcopy, "-o", "-i", str(target), str(temporary), "::/boot/cmdline"])
    finally:
        temporary.unlink(missing_ok=True)


def arm64_rootfs_dirty_marker(cfg: dict[str, Any]) -> Path:
    return Path(str(cfg["_dir"])) / "arm64-rootfs-dirty"


def persist_arm64_guest_files(
    cfg: dict[str, Any], files: dict[str, list[str]]
) -> None:
    if cfg.get("architecture", "x86_64") not in ("arm64", "aarch64"):
        return
    rootfs = vm_path(cfg, "rootfs_path")
    for destination, lines in files.items():
        parent = destination.rpartition("/")[0] or "/"
        debugfs_mkdir_p(rootfs, parent)
        debugfs_write_text(
            rootfs, destination, "\n".join(lines) + "\n", mode=0o644
        )
    arm64_rootfs_dirty_marker(cfg).touch()


def debugfs_ensure_symlink(image: Path, path: str, target: str) -> bool:
    if debugfs_path_exists(image, path):
        return False
    debugfs_mkdir_p(image, path.rpartition("/")[0] or "/")
    debugfs = need_tool("debugfs")
    run([debugfs, "-w", "-R", f"symlink {path} {target}", str(image)])
    if not debugfs_path_exists(image, path):
        raise VmError(f"failed to create {path} in {image}")
    return True


def repair_arm64_desktop_image(cfg: dict[str, Any]) -> None:
    if cfg.get("architecture", "x86_64") not in ("arm64", "aarch64"):
        return
    rootfs = vm_path(cfg, "rootfs_path")
    changed = False
    if debugfs_path_exists(rootfs, "/usr/bin/Thunar"):
        changed |= debugfs_ensure_symlink(rootfs, "/usr/bin/thunar", "Thunar")
    if debugfs_path_exists(rootfs, "/etc/init.d/dbus"):
        changed |= debugfs_ensure_symlink(
            rootfs, "/etc/runlevels/default/dbus", "/etc/init.d/dbus"
        )
    if changed:
        arm64_rootfs_dirty_marker(cfg).touch()


def refresh_arm64_rootfs_payload(cfg: dict[str, Any]) -> None:
    marker = arm64_rootfs_dirty_marker(cfg)
    if not marker.is_file():
        return
    rootfs = vm_path(cfg, "rootfs_path")
    if not rootfs.is_file():
        raise VmError("cannot refresh ARM64 rootfs payload before boot")
    marker.unlink()


def copy_file(src: Path, dst: Path) -> None:
    if not src.is_file():
        raise VmError(f"source file not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def qemu_disk_args(
    cfg: dict[str, Any],
    image: Path,
    index: int = 0,
    disk_config: dict[str, Any] | None = None,
) -> list[str]:
    options = disk_config or {}
    drive_id = f"disk{index}"
    disk_aio = str(options.get("aio", cfg.get("disk_aio", "native")))
    if disk_aio == "native" and not sys.platform.startswith("linux"):
        disk_aio = "threads"
    image_format = str(options.get("format", "qcow2" if image.suffix == ".qcow2" else "raw"))
    drive = [
        f"file={image}",
        f"format={image_format}",
        "if=none",
        f"id={drive_id}",
        f"cache={options.get('cache', cfg.get('disk_cache', 'none'))}",
        f"aio={disk_aio}",
        f"discard={options.get('discard', cfg.get('disk_discard', 'unmap'))}",
    ]
    read_only = bool(options.get("read_only", index == 0 and cfg.get("disk_read_only", False)))
    if read_only:
        drive.append("readonly=on")
    args = ["-drive", ",".join(drive)]
    controller = options.get("controller", cfg.get("disk_controller", "nvme"))
    if (controller == "nvme" and sys.platform == "darwin" and
            cfg.get("architecture", "x86_64") == "x86_64" and
            cfg.get("_selected_accelerator") == "tcg"):
        controller = "virtio-blk"
    if controller == "nvme":
        serial = str(options.get("serial", cfg.get("disk_model", f"edgeos-disk{index}")))
        args += ["-device", f"nvme,serial={serial},drive={drive_id}"]
    elif controller in ("virtio-blk", "virtio-mmio"):
        model = "virtio-blk-device" if cfg.get("architecture") in ("arm64", "aarch64") else "virtio-blk-pci"
        args += ["-device", f"{model},drive={drive_id}"]
    elif controller == "ide":
        args = ["-drive", f"file={image},format={image_format},if=ide,index={index},media=disk"]
    else:
        args += ["-device", f"{controller},drive={drive_id}"]
    return args


def resolve_storage_path(cfg: dict[str, Any], value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return Path(str(cfg["_dir"])) / path


def macvtap_device_path(ifname: str) -> Path:
    ifindex_path = Path("/sys/class/net") / ifname / "ifindex"
    if not ifindex_path.is_file():
        raise VmError(f"macvtap interface not found: {ifname}; run the macvtap setup command first")
    ifindex = ifindex_path.read_text(encoding="ascii").strip()
    tap = Path("/dev") / f"tap{ifindex}"
    if not tap.exists():
        raise VmError(f"macvtap device not found: {tap}")
    return tap


def qemu_net_args(cfg: dict[str, Any], inherited_fds: list[int] | None = None) -> list[str]:
    args: list[str] = []
    for idx, net in enumerate(cfg.get("networks", [])):
        net_id = f"net{idx}"
        model = net.get("model", "e1000")
        mode = net.get("type", "user")
        if mode == "none":
            continue
        if mode == "tap":
            ifname = net.get("ifname", f"tap{idx}")
            args += ["-netdev", f"tap,id={net_id},ifname={ifname},script=no,downscript=no"]
        elif mode == "bridge":
            br = net.get("bridge", "br0")
            args += ["-netdev", f"bridge,id={net_id},br={br}"]
        elif mode in ("macvtap", "macvlan"):
            if inherited_fds is None:
                raise VmError("internal error: macvtap requires inherited fd tracking")
            ifname = net.get("ifname", net.get("name", f"edge-macvtap{idx}"))
            tap_path = macvtap_device_path(str(ifname))
            fd = os.open(tap_path, os.O_RDWR | os.O_NONBLOCK)
            inherited_fds.append(fd)
            args += ["-netdev", f"tap,id={net_id},fd={fd}"]
        elif mode == "socket":
            socket_args = ",".join(f"{k}={v}" for k, v in net.items() if k not in ("type", "model"))
            args += ["-netdev", f"socket,id={net_id},{socket_args}"]
        else:
            hostfwd = net.get("hostfwd", [])
            extra = "".join(f",hostfwd={item}" for item in hostfwd)
            args += ["-netdev", f"user,id={net_id}{extra}"]
        device = f"{model},netdev={net_id}"
        mac = str(net.get("mac", "")).strip()
        if mac:
            device += f",mac={mac}"
        if not bool(net.get("connected", True)):
            device += ",link_down=on"
        args += ["-device", device]
    return args


def apply_cpu_topology_overrides(
    cfg: dict[str, Any],
    *,
    cpus: int | None,
    sockets: int | None,
    cores: int | None,
    threads: int | None,
) -> None:
    """Apply CLI topology overrides while preserving QEMU's SMP invariant."""

    def positive(name: str, value: int) -> int:
        value = int(value)
        if value < 1:
            raise VmError(f"{name} must be at least 1")
        return value

    topology_overridden = any(value is not None for value in (sockets, cores, threads))
    if cpus is not None:
        cpu_count = positive("cpus", cpus)
        socket_count = positive("cpu sockets", sockets) if sockets is not None else 1
        thread_count = positive("CPU threads", threads) if threads is not None else 1
        if cores is None:
            divisor = socket_count * thread_count
            if cpu_count % divisor:
                raise VmError(
                    "CPU count must be divisible by sockets times threads: "
                    f"cpus={cpu_count} sockets={socket_count} threads={thread_count}"
                )
            core_count = cpu_count // divisor
        else:
            core_count = positive("CPU cores", cores)
        product = socket_count * core_count * thread_count
        if product != cpu_count:
            raise VmError(
                "CPU topology does not match CPU count: "
                f"cpus={cpu_count} sockets={socket_count} "
                f"cores={core_count} threads={thread_count}"
            )
        cfg.update(
            {
                "cpus": cpu_count,
                "cpu_sockets": socket_count,
                "cpu_cores": core_count,
                "cpu_threads": thread_count,
            }
        )
        return

    if not topology_overridden:
        return
    socket_count = positive("CPU sockets", sockets) if sockets is not None else max(1, int(cfg.get("cpu_sockets", 1)))
    core_count = positive("CPU cores", cores) if cores is not None else max(1, int(cfg.get("cpu_cores", 1)))
    thread_count = positive("CPU threads", threads) if threads is not None else max(1, int(cfg.get("cpu_threads", 1)))
    cfg.update(
        {
            "cpus": socket_count * core_count * thread_count,
            "cpu_sockets": socket_count,
            "cpu_cores": core_count,
            "cpu_threads": thread_count,
        }
    )


def qemu_smp_value(cfg: dict[str, Any]) -> str:
    sockets = max(1, int(cfg.get("cpu_sockets", 1)))
    cores = max(1, int(cfg.get("cpu_cores", cfg.get("cpus", 4))))
    threads = max(1, int(cfg.get("cpu_threads", 1)))
    cpus = max(1, int(cfg.get("cpus", sockets * cores * threads)))
    if sockets * cores * threads != cpus:
        raise VmError(
            "invalid CPU topology: "
            f"cpus={cpus} sockets={sockets} cores={cores} threads={threads}"
        )
    return f"cpus={cpus},sockets={sockets},cores={cores},threads={threads}"


def qemu_optional_device_args(cfg: dict[str, Any], architecture: str) -> list[str]:
    args: list[str] = []
    sound = str(cfg.get("sound", "none"))
    if sound == "ac97" and architecture == "x86_64":
        args += ["-device", "AC97"]
    elif sound in ("hda", "intel-hda") and architecture == "x86_64":
        args += ["-device", "ich9-intel-hda", "-device", "hda-duplex"]
    if bool(cfg.get("balloon", False)):
        model = "virtio-balloon-device" if architecture in ("arm64", "aarch64") else "virtio-balloon-pci"
        args += ["-device", model]
    if bool(cfg.get("rng", False)):
        model = "virtio-rng-device" if architecture in ("arm64", "aarch64") else "virtio-rng-pci"
        args += ["-object", "rng-random,id=rng0,filename=/dev/urandom", "-device", f"{model},rng=rng0"]
    return args


def qemu_usb_args(cfg: dict[str, Any]) -> list[str]:
    mode = cfg.get("usb", "virtio-input")
    nodefaults = bool(cfg.get("nodefaults", str(mode).startswith("xhci")))
    host_devices = qemu_usb_host_args(cfg)
    if mode == "virtio-input":
        args = [
            "-device",
            "virtio-tablet-pci,disable-modern=off,disable-legacy=on",
            "-device",
            "virtio-keyboard-pci,disable-modern=off,disable-legacy=on",
        ]
        if host_devices:
            args += ["-device", "qemu-xhci,id=usb0"]
        return args + host_devices
    if mode == "off":
        return (["-device", "qemu-xhci,id=usb0"] + host_devices) if host_devices else []
    if mode == "uhci-mouse":
        return ["-device", "piix3-usb-uhci,id=usb0", "-device", "usb-mouse,bus=usb0.0"] + qemu_usb_host_args(cfg)
    if mode == "xhci-mouse":
        if nodefaults:
            return [
                "-device", "qemu-xhci,id=usb0",
                "-device", "usb-kbd,bus=usb0.0",
                "-device", "usb-mouse,bus=usb0.0",
            ] + qemu_usb_host_args(cfg)
        return ["-device", "qemu-xhci,id=usb0", "-device", "usb-mouse,bus=usb0.0"] + qemu_usb_host_args(cfg)
    if mode == "xhci-keyboard":
        return ["-device", "qemu-xhci,id=usb0", "-device", "usb-kbd,bus=usb0.0"] + qemu_usb_host_args(cfg)
    if mode == "xhci-kbd-uhci-mouse":
        return [
            "-device",
            "qemu-xhci,id=usb0",
            "-device",
            "usb-kbd,bus=usb0.0",
            "-device",
            "piix3-usb-uhci,id=usb1",
            "-device",
            "usb-mouse,bus=usb1.0",
        ] + qemu_usb_host_args(cfg)
    return [
        "-device",
        "qemu-xhci,id=usb0",
        "-device",
        "usb-kbd,bus=usb0.0",
        "-device",
        "usb-mouse,bus=usb0.0",
    ] + qemu_usb_host_args(cfg)


def qemu_usb_host_args(cfg: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for device in cfg.get("usb_devices", []):
        vendor_id = str(device["vendor_id"]).lower()
        product_id = str(device["product_id"]).lower()
        spec = f"usb-host,vendorid=0x{vendor_id},productid=0x{product_id}"
        serial = str(device.get("serial", "")).strip()
        if serial:
            spec += f",serial={serial}"
        args += ["-device", spec]
    return args


def qemu_shared_folder_args(cfg: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for folder in cfg.get("shared_folders", []):
        host_path = Path(str(folder["path"])).expanduser().resolve()
        if not host_path.is_dir():
            raise VmError(f"shared folder not found: {host_path}")
        tag = str(folder["tag"])
        spec = f"local,path={host_path},mount_tag={tag},security_model=none,multidevs=remap"
        if bool(folder.get("read_only", False)):
            spec += ",readonly=on"
        args += ["-virtfs", spec]
    return args


def qemu_display_args(cfg: dict[str, Any], display: str) -> list[str]:
    gpu = str(cfg.get("gpu", "std"))
    gl_device = gpu in ("virtio-gpu-gl-pci", "virtio-vga-gl", "virgl-experimental")
    if display in ("none", "off"):
        return ["-display", "none"]
    if display not in ("window", "default", ""):
        return ["-display", display]
    backend = str(cfg.get("display_backend", "default"))
    if gl_device:
        if backend in ("default", "window", ""):
            backend = "gtk"
        if "gl=" not in backend:
            backend += ",gl=on"
        return ["-display", backend]
    if backend not in ("default", "window", ""):
        return ["-display", backend]
    return []


def qemu_gpu_args(cfg: dict[str, Any], no_default_devices: bool) -> list[str]:
    gpu = str(cfg.get("gpu", "std"))
    if gpu in ("none", "off"):
        return ["-vga", "none"]
    if gpu in ("virtio-gpu-gl-pci", "virgl-experimental"):
        return ["-vga", "none", "-device", "virtio-gpu-gl-pci"]
    if gpu == "virtio-vga-gl":
        return ["-vga", "none", "-device", "virtio-vga-gl"]
    if gpu in ("virtio-gpu", "virtio-gpu-pci", "virtio-gpu-gl", "virgl"):
        return ["-vga", "none", "-device", "virtio-gpu-pci"]
    if gpu in ("virtio-vga", "virtio"):
        return ["-vga", "virtio"]
    if gpu == "qxl":
        return ["-vga", "qxl"]
    if gpu == "bochs-display":
        return ["-vga", "none", "-device", "bochs-display"]
    if no_default_devices:
        return ["-vga", str(cfg.get("vga", "std"))]
    return []


def arm64_firmware() -> Path:
    configured = os.environ.get("EDGEOS_AARCH64_EFI")
    candidates = [
        Path(configured) if configured else None,
        Path("/opt/homebrew/share/qemu/edk2-aarch64-code.fd"),
        Path("/usr/local/share/qemu/edk2-aarch64-code.fd"),
        Path("/usr/share/AAVMF/AAVMF_CODE.fd"),
        Path("/usr/share/qemu-efi-aarch64/QEMU_EFI.fd"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise VmError("AArch64 UEFI firmware not found; set EDGEOS_AARCH64_EFI")


def qemu_accelerators(qemu: str) -> set[str]:
    result = subprocess.run([qemu, "-accel", "help"], text=True, capture_output=True, check=False)
    return {
        line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines()
        if line.strip() and " " not in line.strip() and line.strip() != "Accelerators"
    }


def select_accelerator(qemu: str, cfg: dict[str, Any], requested: str | None = None) -> str:
    available = qemu_accelerators(qemu)
    choice = requested or str(cfg.get("accelerator", "auto"))
    if choice in ("", "auto"):
        if sys.platform == "darwin" and "hvf" in available:
            choice = "hvf"
        elif Path("/dev/kvm").exists() and "kvm" in available:
            choice = "kvm"
        elif sys.platform.startswith("netbsd") and "nvmm" in available:
            choice = "nvmm"
        elif sys.platform.startswith("dragonfly") and "nvmm" in available:
            choice = "nvmm"
        elif "tcg" in available:
            choice = "tcg"
        else:
            raise VmError(f"no supported QEMU accelerator found in {qemu}")
    if choice not in available:
        raise VmError(f"QEMU accelerator {choice!r} is unavailable; supported: {', '.join(sorted(available))}")
    return choice


def machine_without_accel(value: str) -> str:
    parts = [part for part in value.split(",") if not part.startswith("accel=")]
    return ",".join(parts)


def qemu_cmd_arm64(
    cfg: dict[str, Any], *, background: bool, machine: str | None,
    cpu_model: str | None, memory: str | None, display: str,
    accelerator: str | None,
) -> list[str]:
    qemu = need_tool("qemu-system-aarch64")
    image = vm_path(cfg, "uefi_path")
    if not image.is_file():
        raise VmError(f"ARM64 UEFI image not found: {image}; run update-kernel first")
    host_arm = platform.machine().lower() in ("arm64", "aarch64")
    accel = select_accelerator(qemu, cfg, accelerator)
    cfg["_selected_accelerator"] = accel
    if accel == "hvf" and not (sys.platform == "darwin" and host_arm):
        raise VmError("HVF acceleration requires an Apple Silicon macOS host")
    selected_cpu = cpu_model or ("host" if accel == "hvf" else str(cfg.get("cpu_model", "cortex-a72")))
    selected_machine = machine_without_accel(machine or str(cfg.get("machine", "virt,gic-version=3,acpi=off")))
    qmp_args: list[str] = []
    if background:
        for filename in ("qmp.sock", "qmp-events.sock"):
            qmp_sock = Path(str(cfg["_dir"])) / filename
            qmp_sock.unlink(missing_ok=True)
            qmp_args += ["-qmp", f"unix:{qmp_sock},server=on,wait=off"]
    cmd = [
        qemu,
        "-machine", selected_machine,
        "-accel", accel,
        "-global", "virtio-mmio.force-legacy=false",
        "-cpu", selected_cpu,
        "-smp", qemu_smp_value(cfg),
        "-m", qemu_memory_value(memory or str(cfg.get("memory", "2048M"))),
        "-bios", str(arm64_firmware()),
        "-monitor", "none",
    ]
    cmd += qmp_args
    if display in ("none", "off"):
        cmd += ["-display", "none"]
    elif sys.platform == "darwin" and display in ("window", "default", ""):
        cmd += ["-display", "cocoa"]
    elif display not in ("window", "default", ""):
        cmd += ["-display", display]
    gpu = str(cfg.get("gpu", "ramfb"))
    if gpu in (
        "virtio-gpu", "virtio-gpu-pci", "virtio-gpu-gl",
        "virtio-gpu-gl-pci", "virtio-vga", "virtio-vga-gl", "virgl",
        "virgl-experimental",
    ):
        # The ARM virt machine exposes the GPU through the native VirtIO MMIO
        # bus.  Its 2D protocol is shared with the PCI driver; only transport
        # register access differs.  QEMU's macOS build has no VirGL backend,
        # so GL-labelled configurations retain the accelerated 2D device.
        cmd += ["-device", "virtio-gpu-device"]
    elif gpu not in ("none", "off"):
        cmd += ["-device", "ramfb"]
    if background:
        serial_log = Path(str(cfg["_dir"])) / "serial.log"
        serial_sock = Path(str(cfg["_dir"])) / "serial.sock"
        serial_sock.unlink(missing_ok=True)
        cmd += [
            "-chardev", f"socket,id=serial0,path={serial_sock},server=on,wait=off,logfile={serial_log},logappend=on",
            "-serial", "chardev:serial0",
        ]
    else:
        cmd += ["-serial", "stdio"]
    cmd += [
        *qemu_disk_args(cfg, vm_path(cfg, "rootfs_path"), 0),
        "-drive", f"if=none,file={image},format=raw,id=esp",
        "-device", "virtio-blk-device,drive=esp,bootindex=1",
    ]
    for idx, item in enumerate(cfg.get("storage", []), start=1):
        storage_path = resolve_storage_path(cfg, str(item["path"]))
        if not storage_path.is_file():
            raise VmError(f"additional disk not found: {storage_path}")
        cmd += qemu_disk_args(cfg, storage_path, idx, item)
    inherited_fds: list[int] = []
    cmd += qemu_net_args(cfg, inherited_fds)
    if inherited_fds:
        cfg["_qemu_inherited_fds"] = inherited_fds
    cmd += [
        "-device", "virtio-tablet-device,serial=edgeos-virtio-input-config-alignment-0000000000000000000000000000",
        "-device", "virtio-keyboard-device,serial=edgeos-virtio-input-config-alignment-0000000000000000000000000000",
        "-no-reboot",
    ]
    if cfg.get("usb_devices"):
        cmd += ["-device", "qemu-xhci,id=usb0"]
        cmd += qemu_usb_host_args(cfg)
    cmd += qemu_optional_device_args(cfg, "arm64")
    cmd += qemu_shared_folder_args(cfg)
    cmd += list(cfg.get("qemu_args", []))
    suspend_state = configured_suspend_state(cfg)
    if suspend_state is not None:
        if not suspend_state.is_file():
            raise VmError(f"suspend state not found: {suspend_state}")
        cmd += ["-incoming", f"file:{suspend_state}"]
    return [str(x) for x in cmd]


def qemu_cmd(
    cfg: dict[str, Any],
    *,
    background: bool = False,
    kvm: bool | None = None,
    machine: str | None = None,
    cpu_model: str | None = None,
    memory: str | None = None,
    display: str = "none",
    accelerator: str | None = None,
) -> list[str]:
    if cfg.get("architecture", "x86_64") in ("arm64", "aarch64"):
        return qemu_cmd_arm64(
            cfg, background=background, machine=machine, cpu_model=cpu_model,
            memory=memory, display=display, accelerator=accelerator,
        )
    qemu = need_tool("qemu-system-x86_64")
    iso = vm_path(cfg, "iso_path")
    disk = vm_path(cfg, "rootfs_path")
    if not iso.is_file():
        raise VmError(f"boot ISO not found: {iso}; run update-kernel first")
    if not disk.is_file():
        raise VmError(f"rootfs image not found: {disk}")

    cmd = [qemu]
    use_kvm = cfg.get("kvm", True) if kvm is None else kvm
    requested_accel = accelerator or (None if use_kvm else "tcg")
    accel = select_accelerator(qemu, cfg, requested_accel)
    cfg["_selected_accelerator"] = accel
    machine = machine_without_accel(machine or str(cfg.get("machine", "pc")))
    cpu_model = cpu_model or (str(cfg.get("cpu_model", "host,migratable=off")) if accel != "tcg" else "qemu64")
    usb_mode = str(cfg.get("usb", "virtio-input"))
    no_default_devices = bool(cfg.get("nodefaults", False))
    if (usb_mode.startswith("xhci") or usb_mode == "virtio-input") and "i8042=" not in machine:
        # Keep explicit input modes from leaving QEMU's built-in PS/2
        # controller as the active keyboard/mouse sink. The guest still sees
        # the selected xHCI or VirtIO input devices added below.
        machine += ",i8042=off"

    cmd += ["-accel", accel]
    if no_default_devices:
        # QEMU's default PC machine adds PS/2 keyboard/mouse devices.  With
        # those present, QEMU window input can be consumed by i8042 while the
        # explicit xHCI usb-kbd/usb-mouse devices sit idle, making evdev/Xorg
        # tests falsely pass or fail against the wrong input path.
        cmd.append("-nodefaults")
    qmp_args: list[str] = []
    if background:
        for filename in ("qmp.sock", "qmp-events.sock"):
            qmp_sock = Path(str(cfg["_dir"])) / filename
            qmp_sock.unlink(missing_ok=True)
            qmp_args += ["-qmp", f"unix:{qmp_sock},server=on,wait=off"]

    cmd += [
        "-cpu",
        cpu_model,
        "-machine",
        machine,
        "-smp",
        qemu_smp_value(cfg),
        "-m",
        qemu_memory_value(memory or str(cfg.get("memory", "2048M"))),
        "-rtc",
        f"base={cfg.get('rtc_base', 'utc')},clock=host",
        "-monitor",
        "none",
    ]
    cmd += qmp_args
    firmware_mode = str(cfg.get("firmware_mode", "auto"))
    firmware = str(cfg.get("firmware", "")).strip()
    if not firmware and firmware_mode != "bios" and sys.platform == "darwin":
        qemu_prefix = Path(qemu).parent.parent
        candidate = qemu_prefix / "share" / "qemu" / "edk2-x86_64-code.fd"
        if candidate.is_file():
            firmware = str(candidate)
    if firmware_mode == "uefi" and not firmware:
        raise VmError("UEFI firmware was requested but no x86_64 EDK2 firmware was found")
    if firmware:
        if not Path(firmware).is_file():
            raise VmError(f"x86 firmware not found: {firmware}")
        cmd += [
            "-drive",
            f"if=pflash,format=raw,unit=0,readonly=on,file={firmware}",
        ]
    cmd += qemu_display_args(cfg, display)
    cmd += qemu_gpu_args(cfg, no_default_devices)
    if background:
        serial_log = Path(str(cfg["_dir"])) / "serial.log"
        serial_pty = Path(str(cfg["_dir"])) / "serial.pty"
        serial_sock = Path(str(cfg["_dir"])) / "serial.sock"
        serial_pty.unlink(missing_ok=True)
        serial_sock.unlink(missing_ok=True)
        cmd += [
            "-chardev",
            f"socket,id=serial0,path={serial_sock},server=on,wait=off,logfile={serial_log},logappend=on",
            "-serial",
            "chardev:serial0",
        ]
    else:
        cmd += ["-serial", "stdio"]
    cmd += qemu_disk_args(cfg, disk, 0)
    for idx, item in enumerate(cfg.get("storage", []), start=1):
        storage_path = resolve_storage_path(cfg, str(item["path"]))
        if not storage_path.is_file():
            raise VmError(f"additional disk not found: {storage_path}")
        cmd += qemu_disk_args(cfg, storage_path, idx, item)
    if bool(cfg.get("cdrom_connected", True)):
        cmd += ["-cdrom", str(iso)]
    inherited_fds: list[int] = []
    cmd += qemu_net_args(cfg, inherited_fds)
    cmd += qemu_usb_args(cfg)
    cmd += qemu_optional_device_args(cfg, "x86_64")
    cmd += qemu_shared_folder_args(cfg)
    cmd += list(cfg.get("passthrough", []))
    boot = f"order={cfg.get('boot_order', 'd')}"
    boot += f",menu={'on' if bool(cfg.get('boot_menu', True)) else 'off'}"
    delay = max(0, int(cfg.get("boot_delay_ms", 0)))
    if delay:
        boot += f",splash-time={delay}"
    cmd += ["-boot", boot]
    cmd += list(cfg.get("qemu_args", []))
    suspend_state = configured_suspend_state(cfg)
    if suspend_state is not None:
        if not suspend_state.is_file():
            raise VmError(f"suspend state not found: {suspend_state}")
        cmd += ["-incoming", f"file:{suspend_state}"]
    if inherited_fds:
        cfg["_qemu_inherited_fds"] = inherited_fds
    else:
        cfg.pop("_qemu_inherited_fds", None)
    return [str(x) for x in cmd]


def tail_text(path: Path, max_lines: int = 40) -> str:
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def serial_prompt_state(text: str) -> str:
    """Return the newest interactive prompt represented in a serial log tail."""
    shell_position = text.rfind(":~#")
    for pattern in (
        r"(?m)^/[^\r\n]* #\s*$",
        r"(?m)^[^ \r\n]+:[^\r\n]*[#$]\s*$",
    ):
        for match in re.finditer(pattern, text):
            shell_position = max(shell_position, match.start())
    positions = {
        "shell": shell_position,
        "login": text.rfind("login:"),
        "password": text.rfind("Password:"),
    }
    state, position = max(positions.items(), key=lambda item: item[1])
    return state if position >= 0 else ""


def wait_for_serial_prompt(path: Path, state: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if serial_prompt_state(tail_text(path, max_lines=80)) == state:
            return True
        time.sleep(0.2)
    return False


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def write_fd_all(fd: int, data: bytes) -> None:
    pos = 0
    while pos < len(data):
        try:
            n = os.write(fd, data[pos:])
        except BlockingIOError:
            time.sleep(0.002)
            continue
        if n <= 0:
            time.sleep(0.002)
            continue
        pos += n


def wait_for_text(path: Path, needle: str, timeout: float, start_size: int = 0) -> bool:
    needle_b = needle.encode("utf-8", errors="replace")
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = read_bytes(path)
        if needle_b in data[start_size:]:
            return True
        time.sleep(0.2)
    return needle_b in read_bytes(path)[start_size:]


def wait_for_text_with_progress(
    path: Path,
    needle: str,
    inactivity_timeout: float,
    hard_timeout: float,
    start_size: int = 0,
) -> bool:
    """Wait for a marker while allowing long commands that keep producing output."""
    needle_b = needle.encode("utf-8", errors="replace")
    now = time.time()
    inactivity_deadline = now + inactivity_timeout
    hard_deadline = now + hard_timeout
    observed_size = start_size

    while time.time() < hard_deadline:
        data = read_bytes(path)
        if needle_b in data[start_size:]:
            return True
        if len(data) > observed_size:
            observed_size = len(data)
            inactivity_deadline = time.time() + inactivity_timeout
        if time.time() >= inactivity_deadline:
            return False
        time.sleep(0.2)
    return needle_b in read_bytes(path)[start_size:]


def write_serial_line(
    fd: int,
    line: str,
    delay: float = 0.015,
    eol: str = "\r\n",
    *,
    drain: bool = True,
) -> None:
    # QEMU exposes the serial console through a Unix PTY.  Send one carriage
    # return plus line feed. BusyBox getty/login accepts CR for credentials,
    # but once Xorg switches the framebuffer VT the pty-backed serial shell can
    # echo a command line without dispatching it until LF arrives.  CRLF matches
    # common terminal behavior and keeps marker-driven VMM commands reliable.
    #
    # Keep the byte pacing conservative.  EdgeOS' current UART/line-discipline
    # path can drop bytes when long VMM helper commands are pasted too quickly,
    # which leaves x11-run waiting for markers from a truncated shell command.
    data = (line + eol).encode("utf-8")
    can_drain = os.isatty(fd)
    for byte in data:
        write_fd_all(fd, bytes([byte]))
        if drain and can_drain:
            termios.tcdrain(fd)
        if delay:
            time.sleep(delay)


def write_serial_bytes(fd: int, data: bytes, delay: float = 0.001) -> None:
    can_drain = os.isatty(fd)
    for byte in data:
        write_fd_all(fd, bytes([byte]))
        if can_drain:
            termios.tcdrain(fd)
        if delay:
            time.sleep(delay)


def serial_log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def serial_run_marked(
    fd: int,
    serial_log: Path,
    command: str,
    marker: str,
    timeout: float,
    *,
    delay: float = SERIAL_MARKED_COMMAND_DELAY,
    drain: bool = False,
) -> None:
    start_size = serial_log_size(serial_log)
    write_serial_line(fd, command, delay=delay, eol="\n", drain=drain)
    if not wait_for_text(serial_log, marker, timeout, start_size):
        raise VmError(f"guest command did not report marker {marker}: {command}")


def write_serial_text(
    fd: int,
    text: str,
    delay: float = SERIAL_MARKED_COMMAND_DELAY,
    *,
    drain: bool = False,
    line_ending: str = "\n",
) -> None:
    # Multi-line heredoc input uses one canonical LF per line.  CR translation
    # is a terminal-mode property and may be disabled by a program that changes
    # termios; relying on it can leave a literal carriage return in a shell
    # delimiter.  LF remains the line delimiter in canonical and raw modes.
    data = text.replace("\n", line_ending).encode("utf-8")
    line_end = line_ending.encode("utf-8")
    can_drain = os.isatty(fd)
    for byte in data:
        write_fd_all(fd, bytes([byte]))
        if drain and can_drain:
            termios.tcdrain(fd)
        if delay:
            time.sleep(delay)
        if line_end and byte == line_end[-1]:
            # Keep heredoc writes compatible with EdgeOS' current UART RX path.
            # X11/XFCE setup uses serial as the control plane, and a dropped
            # byte in a helper script turns into a false kernel/userland
            # failure. Drain once per input line instead of every byte so the
            # transport remains reliable without making x11-run take minutes.
            if can_drain:
                termios.tcdrain(fd)
            if SERIAL_HEREDOC_LINE_DELAY:
                time.sleep(SERIAL_HEREDOC_LINE_DELAY)


def serial_write_guest_file(fd: int, serial_log: Path, path: str, lines: list[str], marker: str, timeout: float) -> None:
    qpath = shlex.quote(path)
    print(f"writing guest file {path}: {len(lines)} heredoc line(s)")
    content = "\n".join(lines) + "\n"
    expected_bytes = len(content.encode("utf-8"))
    transfer_timeout = min(
        timeout,
        max(20.0, expected_bytes * SERIAL_HEREDOC_DELAY * 4.0 +
            len(lines) * SERIAL_HEREDOC_LINE_DELAY * 2.0),
    )

    for attempt in range(1, 4):
        attempt_marker = f"{marker}_TRY{attempt}"
        delimiter = f"EDGEOS_VMM_FILE_{attempt_marker}"
        while delimiter in lines:
            delimiter += "_EOF"
        start_size = serial_log_size(serial_log)

        # The first and final heredoc lines control the shell parser.  Pace
        # those boundaries conservatively; a dropped byte in either line can
        # leave the guest shell waiting forever even when the body arrived.
        write_serial_line(
            fd,
            f"cat > {qpath} <<'{delimiter}'",
            delay=0.004,
            eol="\n",
            drain=False,
        )
        write_serial_text(
            fd,
            content,
            delay=SERIAL_HEREDOC_DELAY,
            drain=False,
            line_ending="\n",
        )
        write_serial_line(fd, delimiter, delay=0.004, eol="\n", drain=False)
        write_serial_line(
            fd,
            f"rc=$?; echo {attempt_marker}_WRITE_$rc",
            delay=0.004,
            eol="\n",
            drain=False,
        )

        if wait_for_text(
            serial_log,
            f"{attempt_marker}_WRITE_0",
            transfer_timeout,
            start_size,
        ):
            try:
                serial_run_marked(
                    fd,
                    serial_log,
                    f"sync; test -s {qpath} && "
                    f"test $(wc -c < {qpath}) -eq {expected_bytes}; "
                    f"echo {attempt_marker}_FILE_$?",
                    f"{attempt_marker}_FILE_0",
                    transfer_timeout,
                )
                serial_run_marked(
                    fd,
                    serial_log,
                    f"test $(wc -l < {qpath}) -eq {len(lines)}; "
                    f"echo {attempt_marker}_LINES_$?",
                    f"{attempt_marker}_LINES_0",
                    transfer_timeout,
                )
                return
            except VmError:
                pass

        # Abort a malformed or unterminated shell construct before retrying.
        # This byte is delivered to the guest UART, not to the host process.
        write_fd_all(fd, b"\x03\n")
        time.sleep(0.5)

    raise VmError(f"guest serial transfer failed integrity checks for {path}")


_serial_pending_status: dict[int, tuple[Path, int, str]] = {}


def _serial_status_result(path: Path, start_size: int, marker: str) -> int | None:
    data = read_bytes(path)[start_size:]
    matches = list(
        re.finditer(re.escape(marker.encode("ascii")) + rb"_(\d{1,3})", data)
    )
    if not matches:
        return None
    status = int(matches[-1].group(1))
    return status if status <= 255 else None


def serial_check_status(fd: int, serial_log: Path, command: str, marker: str, timeout: float) -> int | None:
    pending = _serial_pending_status.get(fd)
    if pending:
        pending_log, pending_start, pending_marker = pending
        status = _serial_status_result(pending_log, pending_start, pending_marker)
        if status is not None:
            _serial_pending_status.pop(fd, None)
            if pending_marker == marker:
                return status
        else:
            # A timed-out request is still present in the guest UART or shell.
            # Do not enqueue another command behind it; callers can poll this
            # request again or reach their own hard deadline without flooding
            # a slow TCG guest's finite receive queue.
            return None

    start_size = serial_log_size(serial_log)
    write_serial_line(fd, f"{command}; echo {marker}_$?", eol="\n")
    _serial_pending_status[fd] = (serial_log, start_size, marker)
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = _serial_status_result(serial_log, start_size, marker)
        if status is not None:
            _serial_pending_status.pop(fd, None)
            return status
        time.sleep(0.2)
    return None


def serial_check_status_with_progress(
    fd: int,
    serial_log: Path,
    command: str,
    marker: str,
    inactivity_timeout: float,
    hard_timeout: float,
) -> int | None:
    start_size = serial_log_size(serial_log)
    write_serial_line(fd, f"{command}; echo {marker}_$?", eol="\n")
    now = time.time()
    inactivity_deadline = now + inactivity_timeout
    hard_deadline = now + hard_timeout
    observed_size = start_size
    marker_pattern = re.compile(
        re.escape(marker.encode("ascii")) + rb"_(\d{1,3})(?:\r?\n|$)"
    )

    while time.time() < hard_deadline:
        data = read_bytes(serial_log)
        matches = list(marker_pattern.finditer(data[start_size:]))
        if matches:
            status = int(matches[-1].group(1))
            return status if status <= 255 else None
        if len(data) > observed_size:
            observed_size = len(data)
            inactivity_deadline = time.time() + inactivity_timeout
        if time.time() >= inactivity_deadline:
            return None
        time.sleep(0.2)
    return None


_serial_drainers: dict[int, tuple[int, threading.Thread]] = {}


def open_serial_pty(cfg: dict[str, Any]) -> tuple[int, Path]:
    serial_sock = Path(str(cfg["_dir"])) / "serial.sock"
    if serial_sock.exists():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(serial_sock))
        except PermissionError:
            s.close()
            if os.geteuid() == 0:
                raise
            subprocess.run(
                ["sudo", "-S", "-p", "", sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                cwd=str(REPO_ROOT),
                input=SUDO_PASSWORD + "\n",
                text=True,
                check=True,
            )
            raise SystemExit(0)
        fd = s.detach()
        _serial_pending_status.pop(fd, None)
        os.set_blocking(fd, False)
        drain_fd = os.dup(fd)

        def drain_serial_socket() -> None:
            try:
                while True:
                    try:
                        chunk = os.read(drain_fd, 4096)
                    except BlockingIOError:
                        time.sleep(0.02)
                        continue
                    if not chunk:
                        break
            except OSError:
                pass
            finally:
                try:
                    os.close(drain_fd)
                except OSError:
                    pass

        drainer = threading.Thread(target=drain_serial_socket, daemon=True)
        drainer.start()
        _serial_drainers[fd] = (drain_fd, drainer)
        return fd, serial_sock

    pty_file = Path(str(cfg["_dir"])) / "serial.pty"
    if not pty_file.is_file():
        raise VmError(f"serial channel not found for {cfg['_name']}; start the VM with --background first")
    pty_path = Path(pty_file.read_text(encoding="ascii").strip())
    try:
        fd = os.open(pty_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except PermissionError:
        if os.geteuid() == 0:
            raise
        subprocess.run(
            ["sudo", "-S", "-p", "", sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=str(REPO_ROOT),
            input=SUDO_PASSWORD + "\n",
            text=True,
            check=True,
        )
        raise SystemExit(0)
    tty.setraw(fd)
    _serial_pending_status.pop(fd, None)
    return fd, pty_path


def assert_kvm_usable() -> None:
    kvm = Path("/dev/kvm")
    if not kvm.exists():
        raise VmError("/dev/kvm is not available; start with --no-kvm")
    if not os.access(kvm, os.R_OK | os.W_OK):
        raise VmError("/dev/kvm exists but is not accessible; start with --no-kvm or fix KVM group permissions")


def qemu_start_needs_sudo(cfg: dict[str, Any], args: argparse.Namespace, inherited_fds: tuple[int, ...]) -> bool:
    if cfg.get("_selected_accelerator") != "kvm":
        return False
    kvm = Path("/dev/kvm")
    if not kvm.exists():
        raise VmError("/dev/kvm is not available; start with --no-kvm")
    if os.access(kvm, os.R_OK | os.W_OK):
        return False
    return True


def sudo_qemu_cmd(cmd: list[str], inherited_fds: tuple[int, ...]) -> list[str]:
    sudo = ["sudo", "-S", "-p", ""]
    if inherited_fds:
        sudo += ["-C", str(max(inherited_fds) + 1)]
    return [*sudo, *cmd]


def command_start_should_reexec_sudo(cfg: dict[str, Any], args: argparse.Namespace) -> bool:
    if os.environ.get(SUDO_REEXEC_ENV) == "1":
        return False
    if os.geteuid() == 0:
        return False
    requested = args.accelerator or str(cfg.get("accelerator", "auto"))
    if args.no_kvm or requested not in ("kvm",):
        return False
    kvm = Path("/dev/kvm")
    return kvm.exists() and not os.access(kvm, os.R_OK | os.W_OK)


def reexec_start_with_sudo() -> None:
    env_args = [f"{SUDO_REEXEC_ENV}=1"]
    for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        value = os.environ.get(key)
        if value:
            env_args.append(f"{key}={value}")
    cmd = ["sudo", "-S", "-p", "", "env", *env_args, sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        input=SUDO_PASSWORD + "\n",
        text=True,
        check=True,
    )


def command_create(args: argparse.Namespace) -> None:
    ensure_state()
    vdir = instance_dir(args.name)
    if vdir.exists():
        raise VmError(f"VM already exists: {args.name}")
    vdir.mkdir(parents=True)
    cfg = apply_templates(args.template)
    if args.architecture in ("arm64", "aarch64"):
        selected_profile = cfg.get("profile", "edgeos")
        cfg = deep_merge(cfg, TEMPLATE_DEFAULTS["arm64"])
        if selected_profile == "debian":
            cfg["profile"] = "debian"
    elif args.architecture:
        cfg["architecture"] = args.architecture
    cfg.update(
        {
            "created_at": int(time.time()),
            "description": args.description,
            "_name": args.name,
            "_dir": str(vdir),
        }
    )
    apply_cpu_topology_overrides(
        cfg,
        cpus=args.cpus,
        sockets=args.cpu_sockets,
        cores=args.cpu_cores,
        threads=args.cpu_threads,
    )
    if args.memory:
        cfg["memory"] = args.memory
    if args.accelerator:
        cfg["accelerator"] = args.accelerator
    if args.cpu_model:
        cfg["cpu_model"] = args.cpu_model
    if args.usb:
        cfg["usb"] = args.usb
    if args.gpu:
        cfg["gpu"] = args.gpu
    if args.virgl:
        cfg["virgl"] = True
    if args.display_backend:
        cfg["display_backend"] = args.display_backend
    if args.disk_controller:
        cfg["disk_controller"] = args.disk_controller
    if args.disk_cache:
        cfg["disk_cache"] = args.disk_cache
    if args.disk_aio:
        cfg["disk_aio"] = args.disk_aio
    if args.disk_discard:
        cfg["disk_discard"] = args.disk_discard
    if args.disk_read_only:
        cfg["disk_read_only"] = True
    if args.firmware_mode:
        cfg["firmware_mode"] = args.firmware_mode
    if args.boot_order:
        cfg["boot_order"] = args.boot_order
    if args.no_boot_menu:
        cfg["boot_menu"] = False
    if args.boot_delay_ms is not None:
        cfg["boot_delay_ms"] = args.boot_delay_ms
    if args.rtc_base:
        cfg["rtc_base"] = args.rtc_base
    if args.sound:
        cfg["sound"] = args.sound
    if args.balloon:
        cfg["balloon"] = True
    if args.rng:
        cfg["rng"] = True
    if args.desktop:
        cfg["desktop"] = args.desktop
    cfg["hostname"] = sanitize_hostname(args.hostname or args.name)
    if args.net:
        cfg["networks"] = [parse_network(item) for item in args.net]
    if args.rootfs_size_mb:
        cfg["rootfs_size_mb"] = args.rootfs_size_mb
    if args.jobs is not None:
        cfg["build_jobs"] = args.jobs

    disk = vm_path(cfg, "rootfs_path")
    if args.import_rootfs:
        copy_file(Path(args.import_rootfs), disk)
        if args.hostname:
            configure_rootfs_identity(disk, cfg["hostname"])
    else:
        built = build_rootfs(cfg)
        copy_file(built, disk)
        configure_rootfs_identity(disk, cfg["hostname"])
        # The Debian builder sets both initial passwords before creating the
        # image. Replacing /etc/shadow here would recreate it as root:root and
        # prevent Debian's set-group-ID unix_chkpwd helper from reading hashes.
        if generated_rootfs_needs_login_rewrite(cfg["profile"]):
            configure_generated_rootfs_login(disk)
        verify_rootfs_image(disk)
    save_vm(args.name, cfg)
    build_kernel(cfg, reconfigure=args.reconfigure)
    print(f"created VM {args.name}")
    print(f"  config: {rel(config_path(args.name))}")
    print(f"  rootfs: {rel(disk)}")
    if cfg.get("architecture", "x86_64") in ("arm64", "aarch64"):
        print(f"  uefi:   {rel(vm_path(cfg, 'uefi_path'))}")
    else:
        print(f"  iso:    {rel(vm_path(cfg, 'iso_path'))}")


def parse_network(value: str) -> dict[str, Any]:
    parts = value.split(",")
    net: dict[str, Any] = {"type": parts[0]}
    for part in parts[1:]:
        key, sep, item = part.partition("=")
        if not sep:
            raise VmError(f"invalid network option: {part}")
        if key == "hostfwd":
            net.setdefault("hostfwd", []).append(item)
        elif key == "connected":
            net[key] = item.lower() not in ("0", "false", "no", "off")
        else:
            net[key] = item
    net.setdefault("model", "e1000")
    return net


def command_list(_: argparse.Namespace) -> None:
    ensure_state()
    rows = []
    for path in sorted(INSTANCES_DIR.glob("*/vm.json")):
        cfg = load_json(path)
        rows.append((path.parent.name, cfg.get("architecture", "x86_64"), cfg.get("profile", ""), cfg.get("memory", ""), cfg.get("cpus", "")))
    if not rows:
        print("no VMs")
        return
    for name, architecture, profile, memory, cpus in rows:
        print(f"{name:20} arch={architecture:7} profile={profile:8} memory={memory:8} cpus={cpus}")


def command_show(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    print(json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")}, indent=2, sort_keys=True))


def command_update_kernel(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    if args.jobs is not None:
        cfg["build_jobs"] = args.jobs
    build_kernel(cfg, reconfigure=args.reconfigure)
    save_vm(args.name, cfg)
    artifact_key = "uefi_path" if cfg.get("architecture", "x86_64") in ("arm64", "aarch64") else "iso_path"
    print(f"updated kernel boot image for {args.name}: {rel(vm_path(cfg, artifact_key))}")


def running_pid(cfg: dict[str, Any]) -> int | None:
    pidfile = Path(str(cfg["_dir"])) / "qemu.pid"
    try:
        pid = int(pidfile.read_text(encoding="ascii").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    probe = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip().startswith("Z"):
        return None
    return pid


def runtime_status(cfg: dict[str, Any]) -> dict[str, Any]:
    pid = running_pid(cfg)
    if pid is None:
        suspend_state = configured_suspend_state(cfg)
        suspended = suspend_state is not None and suspend_state.is_file()
        return {
            "running": False,
            "status": "suspended" if suspended else "shutdown",
            "singlestep": False,
            "qmp": False,
            "pid": None,
            "suspend_state": str(suspend_state) if suspended else None,
        }
    qmp_path = Path(str(cfg["_dir"])) / "qmp.sock"
    if qmp_path.exists():
        try:
            with QmpClient(qmp_path) as client:
                status = client.query_status()
            status.update({"running": True, "qmp": True, "pid": pid})
            return status
        except (OSError, QmpError):
            pass
    return {
        "running": True,
        "status": "running",
        "singlestep": False,
        "qmp": False,
        "pid": pid,
        "legacy_monitor": (Path(str(cfg["_dir"])) / "qemu-monitor.sock").exists(),
    }


def command_status(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    status = runtime_status(cfg)
    if args.json:
        print(json.dumps(status, sort_keys=True))
        return
    print(f"{args.name}: {status['status']}")
    print(f"  running: {'yes' if status['running'] else 'no'}")
    print(f"  pid: {status.get('pid') or '-'}")
    print(f"  qmp: {'connected' if status.get('qmp') else 'unavailable'}")


def wait_for_vm_exit(cfg: dict[str, Any], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if running_pid(cfg) is None:
            cleanup_runtime_files(cfg)
            return True
        time.sleep(0.1)
    return running_pid(cfg) is None


def cleanup_runtime_files(cfg: dict[str, Any]) -> None:
    directory = Path(str(cfg["_dir"]))
    for filename in (
        "qemu.pid",
        "qmp.sock",
        "qmp-events.sock",
        "qemu-monitor.sock",
        "serial.sock",
        "serial.pty",
        "vnc.port",
    ):
        (directory / filename).unlink(missing_ok=True)
    save_json(
        directory / "runtime.json",
        {
            "pid": None,
            "stopped_at": int(time.time()),
            "status": "shutdown",
            "qmp": False,
        },
    )


def send_machine_command(
    cfg: dict[str, Any],
    qmp_command: str,
    legacy_command: str,
) -> str:
    qmp_path = Path(str(cfg["_dir"])) / "qmp.sock"
    if qmp_path.exists():
        try:
            with QmpClient(qmp_path) as client:
                client.execute(qmp_command)
            return "qmp"
        except (OSError, QmpError) as exc:
            raise VmError(str(exc)) from exc
    legacy_path = Path(str(cfg["_dir"])) / "qemu-monitor.sock"
    if legacy_path.exists():
        try:
            legacy_hmp_command(legacy_path, legacy_command)
            return "legacy-monitor"
        except (OSError, QmpError) as exc:
            raise VmError(f"legacy monitor command failed: {exc}") from exc
    raise VmError("VM has no reachable QMP or legacy monitor endpoint")


def command_shutdown(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    if running_pid(cfg) is None:
        cleanup_runtime_files(cfg)
        print(f"{args.name} is already powered off")
        return
    transport = send_machine_command(cfg, "system_powerdown", "system_powerdown")
    print(f"requested graceful shutdown of {args.name} through {transport}")
    if wait_for_vm_exit(cfg, args.timeout):
        print(f"powered off {args.name}")
        return
    if not args.force:
        raise VmError(
            f"guest did not power off within {args.timeout:.1f} seconds; "
            "retry with --force to terminate QEMU"
        )
    terminate_vm_process(cfg, force=True)
    cleanup_runtime_files(cfg)
    print(f"forced power off of {args.name} after graceful shutdown timed out")


def command_reset(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    if running_pid(cfg) is None:
        raise VmError(f"VM is not running: {args.name}")
    transport = send_machine_command(cfg, "system_reset", "system_reset")
    print(f"reset {args.name} through {transport}")


def command_pause(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    if running_pid(cfg) is None:
        raise VmError(f"VM is not running: {args.name}")
    transport = send_machine_command(cfg, "stop", "stop")
    print(f"paused {args.name} through {transport}")


def command_resume(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    if running_pid(cfg) is None:
        raise VmError(f"VM is not running: {args.name}")
    transport = send_machine_command(cfg, "cont", "cont")
    print(f"resumed {args.name} through {transport}")


def configured_suspend_state(cfg: dict[str, Any]) -> Path | None:
    value = str(cfg.get("suspend_state", "")).strip()
    if not value:
        return None
    return resolve_storage_path(cfg, value)


def command_suspend(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    if running_pid(cfg) is None:
        raise VmError(f"VM is not running: {args.name}")
    qmp_path = Path(str(cfg["_dir"])) / "qmp.sock"
    if not qmp_path.exists():
        raise VmError("suspend requires a VM started with QMP support")
    suspend_dir = Path(str(cfg["_dir"])) / "suspend"
    suspend_dir.mkdir(parents=True, exist_ok=True)
    state = suspend_dir / "memory.state"
    temporary = suspend_dir / f".memory.state-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    deadline = time.monotonic() + args.timeout
    previous_status = "running"
    try:
        with QmpClient(qmp_path, timeout=5.0) as client:
            previous_status = str(client.query_status().get("status", "running"))
            client.execute("migrate", {"uri": f"file:{temporary}"})
            while time.monotonic() < deadline:
                migration = client.execute("query-migrate")
                status = str(migration.get("status", "unknown"))
                if status == "completed":
                    break
                if status in ("failed", "cancelled"):
                    raise VmError(f"suspend migration {status}: {migration.get('error-desc', '')}")
                time.sleep(0.1)
            else:
                client.execute("migrate_cancel")
                raise VmError(f"suspend did not complete within {args.timeout:.1f} seconds")
            os.replace(temporary, state)
            cfg["suspend_state"] = str(state.relative_to(Path(str(cfg["_dir"]))))
            cfg["suspended_at"] = int(time.time())
            save_vm(args.name, cfg)
            client.execute("quit")
    except BaseException:
        temporary.unlink(missing_ok=True)
        if previous_status == "running" and running_pid(cfg) is not None:
            try:
                with QmpClient(qmp_path) as client:
                    client.execute("cont")
            except (OSError, QmpError):
                pass
        raise
    if not wait_for_vm_exit(cfg, 10.0):
        raise VmError("QEMU did not exit after saving suspend state")
    cleanup_runtime_files(cfg)
    print(f"suspended {args.name}: {rel(state)}")


def terminate_vm_process(cfg: dict[str, Any], force: bool) -> None:
    pid = running_pid(cfg)
    if pid is None:
        return
    selected_signal = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, selected_signal)
    except PermissionError:
        signal_name = "-9" if force else "-TERM"
        subprocess.run(
            ["sudo", "-S", "-p", "", "kill", signal_name, str(pid)],
            input=SUDO_PASSWORD + "\n",
            text=True,
            check=True,
        )
    if not wait_for_vm_exit(cfg, 5.0):
        raise VmError(f"QEMU process {pid} did not exit")


def command_start(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    rootfs_grown = False
    pidfile = Path(str(cfg["_dir"])) / "qemu.pid"
    if pidfile.is_file():
        try:
            existing_pid = int(pidfile.read_text(encoding="ascii").strip())
            os.kill(existing_pid, 0)
        except (OSError, ValueError):
            pidfile.unlink(missing_ok=True)
        else:
            raise VmError(f"VM is already running: {args.name} pid={existing_pid}")
    if not args.dry_run:
        rootfs_grown = ensure_rootfs_capacity(cfg)
    if args.update_kernel:
        build_kernel(cfg, reconfigure=False)
    if command_start_should_reexec_sudo(cfg, args):
        reexec_start_with_sudo()
        return
    if (not args.dry_run and rootfs_grown and not args.update_kernel and
            cfg.get("architecture", "x86_64") in ("arm64", "aarch64")):
        efi = vm_path(cfg, "out_dir") / "arm64" / "BOOTAA64.EFI"
        if not efi.is_file():
            build_kernel(cfg, reconfigure=False)
        else:
            write_arm64_uefi_image(cfg, efi, vm_path(cfg, "rootfs_path"))
    if not args.dry_run and cfg.get("architecture", "x86_64") in ("arm64", "aarch64"):
        refresh_arm64_rootfs_payload(cfg)
        write_arm64_video_config(cfg)
        write_arm64_command_line(cfg)
    cmd = qemu_cmd(
        cfg,
        background=args.background,
        kvm=False if args.no_kvm else None,
        machine=args.machine,
        cpu_model=args.cpu_model,
        memory=args.memory,
        display=args.display,
        accelerator=args.accelerator,
    )
    if args.dry_run:
        print(" ".join(cmd))
        for fd in cfg.pop("_qemu_inherited_fds", []):
            os.close(fd)
        return
    inherited_fds = tuple(cfg.pop("_qemu_inherited_fds", []))
    use_sudo = qemu_start_needs_sudo(cfg, args, inherited_fds)
    popen_cmd = sudo_qemu_cmd(cmd, inherited_fds) if use_sudo else cmd
    if args.background:
        log = Path(str(cfg["_dir"])) / "qemu.log"
        serial_pty = Path(str(cfg["_dir"])) / "serial.pty"
        serial_sock = Path(str(cfg["_dir"])) / "serial.sock"
        serial_log = Path(str(cfg["_dir"])) / "serial.log"
        serial_pty.unlink(missing_ok=True)
        serial_sock.unlink(missing_ok=True)
        serial_log.unlink(missing_ok=True)
        with log.open("ab") as out:
            try:
                proc = subprocess.Popen(
                    popen_cmd,
                    cwd=str(REPO_ROOT),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE if use_sudo else None,
                    text=True,
                    pass_fds=inherited_fds,
                    start_new_session=(os.name == "posix"),
                )
                if use_sudo and proc.stdin:
                    proc.stdin.write(SUDO_PASSWORD + "\n")
                    proc.stdin.close()
            finally:
                for fd in inherited_fds:
                    os.close(fd)
        time.sleep(0.5)
        rc = proc.poll()
        if rc is not None:
            pidfile.unlink(missing_ok=True)
            log_tail = tail_text(log)
            detail = f"\n{log_tail}" if log_tail else ""
            raise VmError(f"QEMU exited immediately with status {rc}{detail}")
        pidfile.write_text(str(proc.pid), encoding="ascii")
        qmp_path = Path(str(cfg["_dir"])) / "qmp.sock"
        try:
            qmp_status = wait_for_qmp(qmp_path, timeout=5.0)
        except QmpError as exc:
            try:
                terminate_vm_process(cfg, force=True)
            finally:
                cleanup_runtime_files(cfg)
            raise VmError(f"QEMU started without a usable QMP control channel: {exc}") from exc
        save_json(
            Path(str(cfg["_dir"])) / "runtime.json",
            {
                "pid": proc.pid,
                "started_at": int(time.time()),
                "status": qmp_status.get("status", "running"),
                "qmp": True,
            },
        )
        resumed_state = configured_suspend_state(cfg)
        if resumed_state is not None:
            cfg.pop("suspend_state", None)
            cfg.pop("suspended_at", None)
            save_vm(args.name, cfg)
            resumed_state.unlink(missing_ok=True)
            print(f"resumed {args.name} from suspend state")
        for _ in range(20):
            text = read_text(log)
            m = re.search(r"char device redirected to (/dev/pts/\d+) \(label serial0\)", text)
            if m:
                serial_pty.write_text(m.group(1), encoding="ascii")
                break
            time.sleep(0.1)
        print(f"started {args.name} pid={proc.pid}")
        print(f"  serial: {rel(Path(str(cfg['_dir'])) / 'serial.log')}")
        print(f"  qmp: {rel(qmp_path)} status={qmp_status.get('status', 'running')}")
        if serial_sock.exists():
            print(f"  serial socket: {rel(serial_sock)}")
        elif serial_pty.is_file():
            print(f"  serial pty: {serial_pty.read_text(encoding='ascii').strip()}")
        else:
            print("  serial channel: pending; check qemu.log")
        print(f"  qemu log: {rel(log)}")
    else:
        try:
            if use_sudo:
                subprocess.run(
                    popen_cmd,
                    cwd=str(REPO_ROOT),
                    input=SUDO_PASSWORD + "\n",
                    text=True,
                    check=True,
                    pass_fds=inherited_fds,
                )
            else:
                run(cmd, pass_fds=inherited_fds)
        finally:
            for fd in inherited_fds:
                os.close(fd)


def edgeos_xorg_config_lines() -> list[str]:
    """Return the shared fbdev and stable evdev hardware configuration."""
    return [
        '# EdgeOS exposes one stable keyboard and pointer evdev endpoint.',
        '# USB and VirtIO hotplug continue through those kernel endpoints.',
        'Section "ServerFlags"',
        '    Option "AutoAddDevices" "false"',
        '    Option "AllowMouseOpenFail" "false"',
        'EndSection',
        'Section "Device"',
        '    Identifier "EdgeOS framebuffer"',
        '    Driver "fbdev"',
        '    Option "fbdev" "/dev/fb0"',
        '    Option "ShadowFB" "false"',
        'EndSection',
        'Section "Monitor"',
        '    Identifier "EdgeOS monitor"',
        'EndSection',
        'Section "Screen"',
        '    Identifier "EdgeOS screen"',
        '    Device "EdgeOS framebuffer"',
        '    Monitor "EdgeOS monitor"',
        'EndSection',
        'Section "InputDevice"',
        '    Identifier "EdgeOS keyboard"',
        '    Driver "evdev"',
        '    Option "Device" "/dev/input/event0"',
        '    Option "CoreKeyboard" "true"',
        'EndSection',
        'Section "InputDevice"',
        '    Identifier "EdgeOS pointer"',
        '    Driver "evdev"',
        '    Option "Device" "/dev/input/event1"',
        '    Option "CorePointer" "true"',
        '    Option "Mode" "Absolute"',
        'EndSection',
        'Section "ServerLayout"',
        '    Identifier "EdgeOS layout"',
        '    Screen "EdgeOS screen"',
        '    InputDevice "EdgeOS keyboard" "CoreKeyboard"',
        '    InputDevice "EdgeOS pointer" "CorePointer"',
        'EndSection',
    ]


def edgeos_xfce_persistent_files() -> dict[str, list[str]]:
    desktop_defaults = """<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-desktop" version="1.0">
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitor0" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="0"/>
          <property name="color-style" type="int" value="0"/>
          <property name="rgba1" type="array">
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="1.0"/>
          </property>
          <property name="last-image" type="string" value="/usr/share/backgrounds/xfce/xfce-blue.jpg"/>
        </property>
      </property>
      <property name="monitorDefault" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="0"/>
          <property name="color-style" type="int" value="0"/>
          <property name="rgba1" type="array">
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="1.0"/>
          </property>
          <property name="last-image" type="string" value="/usr/share/backgrounds/xfce/xfce-blue.jpg"/>
        </property>
      </property>
    </property>
  </property>
</channel>""".splitlines()
    window_manager_defaults = """<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfwm4" version="1.0">
  <property name="general" type="empty">
    <property name="use_compositing" type="bool" value="false"/>
  </property>
</channel>""".splitlines()
    return {
        "/etc/X11/xorg.conf.d/90-edgeos-fbdev.conf": edgeos_xorg_config_lines(),
        "/etc/xdg/mimeapps.list": [
            "[Default Applications]",
            "inode/directory=thunar.desktop;",
            "",
            "[Added Associations]",
            "inode/directory=thunar.desktop;",
        ],
        "/etc/xdg/xfce4/helpers.rc": [
            "FileManager=Thunar",
            "TerminalEmulator=xfce4-terminal",
        ],
        "/etc/chromium.d/edgeos-renderer": [
            'CHROMIUM_FLAGS="$CHROMIUM_FLAGS --enable-unsafe-swiftshader '
            '--use-gl=angle --use-angle=swiftshader"',
        ],
        "/root/.config/mimeapps.list": [
            "[Default Applications]",
            "inode/directory=thunar.desktop;",
            "",
            "[Added Associations]",
            "inode/directory=thunar.desktop;",
        ],
        "/root/.config/xfce4/helpers.rc": [
            "FileManager=Thunar",
            "TerminalEmulator=xfce4-terminal",
        ],
        "/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml": desktop_defaults,
        "/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml": window_manager_defaults,
    }


def x11_application_alive_command(pidfile: str, statusfile: str) -> str:
    """Accept a live client or a successful single-instance handoff."""
    return (
        f"sleep 2; pid=$(cat {pidfile} 2>/dev/null); "
        f"(test -n \"$pid\" && kill -0 \"$pid\" 2>/dev/null) || "
        f"test \"$(cat {statusfile} 2>/dev/null)\" = 0"
    )


def command_x11_run(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    fd, pty_path = open_serial_pty(cfg)
    serial_log = Path(str(cfg["_dir"])) / "serial.log"
    marker = f"EX{int(time.time()) % 100000}"
    xorg_log = f"/tmp/x{args.display_num}.log"
    x_socket = f"/tmp/.X11-unix/X{args.display_num}"
    xorg_conf = f"/run/edgeos-x{args.display_num}.conf"
    desktop = args.desktop.strip().lower()
    x11_deps = [
        "xorg-server",
        "xf86-video-fbdev",
        "xf86-input-evdev",
        "xinit",
    ]
    window_manager = args.window_manager.strip()
    if args.no_start_server and window_manager == "twm":
        window_manager = ""
    if window_manager.lower() in ("", "none", "no", "off"):
        window_manager = ""
    desktop_xfce = desktop in ("xfce", "xfce4")
    if desktop_xfce:
        x11_deps.extend([
            "dbus",
            "dbus-x11",
            "elogind",
            "elogind-openrc",
            "xfce4",
            "xfce4-screensaver",
            "xfce4-terminal",
            "thunar",
            "xclock",
            "xterm",
            "adwaita-icon-theme",
            "adwaita-xfce-icon-theme",
            "hicolor-icon-theme",
            "gdk-pixbuf-loaders",
            "xdg-utils",
            "librsvg",
            "mousepad",
            "xdpyinfo",
            "xdotool",
            "xclip",
            "xinput",
            "xprop",
            "xwininfo",
        ])
        if window_manager == "twm":
            window_manager = "/run/edgeos-start-xfce"
    if window_manager and not desktop_xfce:
        x11_deps.append(window_manager.split()[0])
    x11_deps.append("xsetroot")
    default_terminal = "xterm -bg white -fg black -geometry 80x24+250+120 -title EdgeOS_xterm"
    if desktop_xfce:
        default_terminal = "xfce4-terminal --geometry=96x28+220+120 --title=EdgeOS_XFCE_terminal"
    client_display = f":{args.display_num}"
    session_env = f"DISPLAY={client_display} XDG_RUNTIME_DIR=/tmp/edgeos-runtime-0"
    app_names = list(args.app or [])
    # Keep an explicit "--app xterm" usable.  The placement/visibility checks
    # below look for EdgeOS_xterm so users are not left at a plain X root
    # cursor when they ask for the most common terminal by its short name.
    app_names = [default_terminal if app.strip() == "xterm" else app for app in app_names]
    if not desktop_xfce and not args.no_default_terminal and not any("xterm" in app.split()[0] for app in app_names):
        app_names.insert(0, default_terminal)
    if not desktop_xfce and not app_names:
        app_names = [default_terminal]
    if desktop_xfce and not args.no_default_terminal and not any(
        app.split()[0] == "xfce4-terminal" for app in app_names
    ):
        app_names.append(default_terminal)
    if not args.no_install_deps and not desktop_xfce:
        x11_deps.extend(app.split()[0] for app in app_names)
    echo_disabled = False
    try:
        start_size = serial_log_size(serial_log)
        print(f"serial pty: {pty_path}")
        # Attach to an already logged-in shell when possible. If the guest is
        # waiting at getty, use the default Alpine test credentials created by
        # the VMM rootfs profile. This is intentionally a VMM convenience path,
        # not kernel/rootfs special-casing.
        deadline = time.time() + args.timeout
        log_tail = tail_text(serial_log, max_lines=80)
        prompt_state = serial_prompt_state(log_tail)
        while not prompt_state:
            if time.time() >= deadline:
                raise VmError("guest did not reach a shell or login prompt on serial")
            time.sleep(0.5)
            log_tail = tail_text(serial_log, max_lines=80)
            prompt_state = serial_prompt_state(log_tail)
        shell_ready = prompt_state == "shell"
        if not shell_ready and not prompt_state:
            write_serial_line(fd, f"echo {marker}_PROBE", eol="\n")
            shell_ready = wait_for_text(serial_log, f"{marker}_PROBE", 10, start_size)
        if not shell_ready:
            if prompt_state == "login":
                write_serial_line(fd, args.login, eol="\r")
                if wait_for_text(serial_log, "Password:", 10, start_size):
                    write_serial_line(fd, args.password)
            elif prompt_state == "password":
                write_serial_line(fd, args.password)
            reached_shell = wait_for_serial_prompt(serial_log, "shell", args.timeout)
            if not reached_shell and serial_prompt_state(
                    tail_text(serial_log, max_lines=80)) != "shell":
                raise VmError("guest shell prompt was not reached on serial")

        # Keep VMM automation from self-throttling on serial echo.  The guest
        # shell otherwise echoes every command byte through the same UART used
        # for marker output; long X11 setup input can take minutes to drain
        # before the shell even dispatches it.  Restore echo before returning
        # the shell to the user.
        serial_run_marked(
            fd,
            serial_log,
            f"set +m 2>/dev/null || true; stty -echo 2>/dev/null || true; "
            f"echo {marker}_STTY_NOECHO",
            f"{marker}_STTY_NOECHO",
            args.timeout,
        )
        echo_disabled = True

        if not args.no_install_deps:
            packages = " ".join(dict.fromkeys(x11_deps))
            print(f"installing/checking guest X11 packages: {packages}")
            apk_status = serial_check_status_with_progress(
                fd,
                serial_log,
                f"apk add --no-progress {packages}",
                f"{marker}_APK_DONE",
                max(args.timeout, 300),
                max(args.timeout * 6, 3600),
            )
            if apk_status is None:
                raise VmError("guest apk command stopped making progress before reporting its status")
            if apk_status != 0:
                raise VmError(f"guest apk command failed with exit status {apk_status}")
            serial_run_marked(
                fd,
                serial_log,
                f"test -e /usr/lib/xorg/modules/drivers/fbdev_drv.so; echo {marker}_FBDEV_$?",
                f"{marker}_FBDEV_0",
                args.timeout,
            )
            if desktop_xfce:
                serial_run_marked(
                    fd,
                    serial_log,
                    "command -v startxfce4 >/dev/null && command -v xfce4-session >/dev/null && "
                    "command -v xfwm4 >/dev/null && command -v xfdesktop >/dev/null && "
                    "command -v xfce4-panel >/dev/null && command -v dbus-launch >/dev/null && "
                    "command -v dbus-run-session >/dev/null; "
                    f"echo {marker}_XFCE_BINS_$?",
                    f"{marker}_XFCE_BINS_0",
                    args.timeout,
                )

        if desktop_xfce:
            # Rootfs images produced while symlink(2) was incomplete can have
            # Thunar's ELF but lack the lowercase link declared by its Alpine
            # package.  Exo filters that helper as unavailable, so restore the
            # package-owned link only when the stale state is actually present.
            serial_run_marked(
                fd,
                serial_log,
                "if [ -x /usr/bin/Thunar ] && [ ! -e /usr/bin/thunar ]; then "
                "ln -s Thunar /usr/bin/thunar; fi; "
                "test ! -x /usr/bin/Thunar || test -e /usr/bin/thunar; "
                f"echo {marker}_XFCE_PACKAGE_LINKS_$?",
                f"{marker}_XFCE_PACKAGE_LINKS_0",
                args.timeout,
            )
            # A regular Alpine desktop enables the stock system bus in the
            # default runlevel.  Direct startxfce4 sessions otherwise spend
            # seconds retrying power, thumbnail, and desktop integrations.
            # Keep this as service configuration rather than launching a
            # private replacement daemon or suppressing D-Bus failures.
            serial_run_marked(
                fd,
                serial_log,
                "if command -v systemctl >/dev/null 2>&1; then "
                "systemctl start dbus.service >/dev/null 2>&1 && "
                "systemctl is-active --quiet dbus.service; "
                "elif command -v rc-update >/dev/null 2>&1 && "
                "[ -x /etc/init.d/dbus ]; then "
                "rc-update add dbus default >/dev/null && "
                "(rc-service dbus status >/dev/null 2>&1 || "
                "rc-service dbus start >/dev/null); "
                "else test ! -x /etc/init.d/dbus; fi; "
                f"echo {marker}_XFCE_SYSTEM_DBUS_$?",
                f"{marker}_XFCE_SYSTEM_DBUS_0",
                args.timeout,
            )
            persistent_files = edgeos_xfce_persistent_files()
            for index, (destination, lines) in enumerate(
                persistent_files.items(), start=1
            ):
                directory = destination.rpartition("/")[0] or "/"
                serial_run_marked(
                    fd,
                    serial_log,
                    f"mkdir -p {shlex.quote(directory)}; "
                    f"echo {marker}_XFCE_CONF_DIR_{index}_$?",
                    f"{marker}_XFCE_CONF_DIR_{index}_0",
                    args.timeout,
                )
                serial_write_guest_file(
                    fd,
                    serial_log,
                    destination,
                    lines,
                    f"{marker}_XFCE_PERSISTENT_{index}",
                    args.timeout,
                )
            persist_arm64_guest_files(cfg, persistent_files)
            repair_arm64_desktop_image(cfg)

        if not args.no_start_server and not desktop_xfce:
            print(f"starting Xorg on :{args.display_num} vt{args.vt}")
            serial_run_marked(
                fd,
                serial_log,
                "for p in Xorg twm xterm xclock xfce4-session xfwm4 xfce4-panel "
                "xfdesktop xfsettingsd xfconfd xfce4-terminal thunar "
                "dbus-launch dbus-daemon; do "
                "  pkill -x $p >/dev/null 2>&1 || true; "
                "  for pid in $(pidof $p 2>/dev/null); do kill -TERM $pid >/dev/null 2>&1 || true; done; "
                "done; "
                "sleep 1; "
                "for p in Xorg twm xterm xclock xfce4-session xfwm4 xfce4-panel "
                "xfdesktop xfsettingsd xfconfd xfce4-terminal thunar "
                "dbus-launch dbus-daemon; do "
                "  for pid in $(pidof $p 2>/dev/null); do kill -KILL $pid >/dev/null 2>&1 || true; done; "
                "done; "
                f"echo {marker}_XKILL",
                f"{marker}_XKILL",
                args.timeout,
            )
            serial_run_marked(
                fd,
                serial_log,
                f"rm -f {shlex.quote(x_socket)} {shlex.quote(xorg_log)} /run/edgeos-xt{args.display_num} "
                "/tmp/edgeos-xfsettingsd.log /tmp/edgeos-xfwm4.log "
                "/tmp/edgeos-xfdesktop.log /tmp/edgeos-xfce4-panel.log "
                f"/tmp/edgeos-dbus-{args.display_num}.env /tmp/edgeos-dbus-{args.display_num}.out; "
                f"echo {marker}_XCLEAN",
                f"{marker}_XCLEAN",
                args.timeout,
            )
            serial_run_marked(
                fd,
                serial_log,
                f"mkdir -p /tmp/.X11-unix; echo {marker}_MKXDIR_$?",
                f"{marker}_MKXDIR_0",
                args.timeout,
            )
            serial_run_marked(
                fd,
                serial_log,
                "mkdir -p /run; "
                f"echo {marker}_MKRUN_$?",
                f"{marker}_MKRUN_0",
                args.timeout,
            )
            serial_write_guest_file(
                fd,
                serial_log,
                xorg_conf,
                edgeos_xorg_config_lines(),
                f"{marker}_XCONF",
                args.timeout,
            )
            serial_run_marked(
                fd,
                serial_log,
                # Avoid -retro here.  It deliberately paints the historical X
                # stipple/root cursor and can preserve a stale framebuffer text
                # cursor in the top-left corner while fbdev takes ownership.
                #
                # Keep Xorg's own log file quiet during startup.  Some Xorg
                # builds parse the registry protocol-name file once per
                # extension and emit warning-level lines for entries they do
                # not consume.  Under the current EdgeOS serial/debug workflow
                # that warning storm can delay client readiness for minutes
                # without changing runtime behavior.
                f"Xorg :{args.display_num} vt{args.vt} -config {xorg_conf} -br -audit 0 -verbose 0 -logverbose 0 >{xorg_log} 2>&1 & echo {marker}_XSTART",
                f"{marker}_XSTART",
                args.timeout,
            )
            deadline = time.time() + args.timeout
            live_xorg_probe = (
                "live=0; "
                "for p in $(pgrep -x Xorg 2>/dev/null); do "
                "grep -Eq '^State:[[:space:]]*[RS]' /proc/$p/status 2>/dev/null && live=1; "
                "done; "
                "test \"$live\" = 1"
            )
            while True:
                # EdgeOS currently backs pathname AF_UNIX sockets with an
                # in-kernel registry; the visible /tmp/.X11-unix/XN node may
                # not report as a socket inode yet.  Require a non-zombie Xorg
                # too, otherwise a stale path left by an exited server makes
                # later xsetroot/XFCE diagnostics chase the wrong failure.
                status = serial_check_status(fd, serial_log, f"test -e {x_socket} && {live_xorg_probe}", f"{marker}_XSOCK", 5)
                if status == 0:
                    break
                if time.time() >= deadline:
                    raise VmError(f"Xorg did not create {x_socket}; check guest {xorg_log}")
                time.sleep(1)
            deadline = time.time() + args.timeout
            while True:
                # Xorg creates the display socket before it has finished
                # loading fbdev, evdev, fonts, and extensions.  A visible
                # root paint is a safer readiness probe than xwininfo here:
                # xwininfo can block on the current AF_UNIX/X11 path even
                # after Xorg accepts clients, while xsetroot both exercises
                # the client socket and proves fbdev updates reach QEMU.
                xready_err = f"/tmp/edgeos-xready-{args.display_num}.err"
                xready_out = f"/tmp/edgeos-xready-{args.display_num}.out"
                xready_probe = (
                    f'DISPLAY={client_display} xsetroot -cursor_name left_ptr -solid black >{xready_out} 2>{xready_err}; '
                    "rc=$?; "
                    "if [ $rc -ne 0 ]; then "
                    f"echo {marker}_XREADY_ERR_BEGIN; "
                    f"cat {xready_err}; "
                    f"echo {marker}_XREADY_ERR_END; "
                    "fi; "
                    "exit $rc"
                )
                xready_command = (
                    f"timeout 20 sh -c {shlex.quote(xready_probe)}; "
                    "rc=$?; "
                    "if [ $rc -ne 0 ]; then pkill -x xsetroot >/dev/null 2>&1 || true; fi; "
                    "exit $rc"
                )
                status = serial_check_status(
                    fd,
                    serial_log,
                    f"sh -c {shlex.quote(xready_command)}",
                    f"{marker}_XREADY",
                    min(args.timeout, 45),
                )
                if status == 0:
                    break
                if time.time() >= deadline:
                    raise VmError(f"Xorg did not become client-ready; check guest {xorg_log}")
                time.sleep(1)
            # On the current EdgeOS Xorg stack, client readiness can still race
            # slightly with module/input initialization.  A successful xsetroot
            # probe already proves clients can connect, so keep this brief.
            time.sleep(1)
        if desktop_xfce:
            if not args.no_start_server:
                print(f"preparing Xorg config for startxfce4 on :{args.display_num} vt{args.vt}")
                serial_run_marked(
                    fd,
                    serial_log,
                    "for p in Xorg twm xterm xclock xfce4-session xfwm4 xfce4-panel "
                    "xfdesktop xfsettingsd xfconfd xfce4-terminal thunar "
                    "dbus-launch dbus-daemon; do "
                    "  pkill -x $p >/dev/null 2>&1 || true; "
                    "  for pid in $(pidof $p 2>/dev/null); do kill -TERM $pid >/dev/null 2>&1 || true; done; "
                    "done; "
                    "sleep 1; "
                    "for p in Xorg twm xterm xclock xfce4-session xfwm4 xfce4-panel "
                    "xfdesktop xfsettingsd xfconfd xfce4-terminal thunar "
                    "dbus-launch dbus-daemon; do "
                    "  for pid in $(pidof $p 2>/dev/null); do kill -KILL $pid >/dev/null 2>&1 || true; done; "
                    "done; "
                    f"echo {marker}_XKILL",
                    f"{marker}_XKILL",
                    args.timeout,
                )
                serial_run_marked(
                    fd,
                    serial_log,
                    f"rm -f {shlex.quote(x_socket)} {shlex.quote(xorg_log)} /run/edgeos-xt{args.display_num} "
                    "/tmp/edgeos-xfsettingsd.log /tmp/edgeos-xfwm4.log "
                    "/tmp/edgeos-xfdesktop.log /tmp/edgeos-xfce4-panel.log "
                    f"/tmp/edgeos-dbus-{args.display_num}.env /tmp/edgeos-dbus-{args.display_num}.out; "
                    f"echo {marker}_XCLEAN",
                    f"{marker}_XCLEAN",
                    args.timeout,
                )
                serial_run_marked(
                    fd,
                    serial_log,
                    f"mkdir -p /tmp/.X11-unix /run; echo {marker}_MKXDIR_$?",
                    f"{marker}_MKXDIR_0",
                    args.timeout,
                )
                serial_write_guest_file(
                    fd,
                    serial_log,
                    xorg_conf,
                    edgeos_xorg_config_lines(),
                    f"{marker}_XCONF",
                    args.timeout,
                )
                serial_run_marked(
                    fd,
                    serial_log,
                    "rm -rf /tmp/edgeos-runtime-0 /tmp/.xfsm-ICE-*; "
                    "mkdir -p /tmp/edgeos-runtime-0; chmod 700 /tmp/edgeos-runtime-0; "
                    f"echo {marker}_XDG_RUNTIME_$?",
                    f"{marker}_XDG_RUNTIME_0",
                    args.timeout,
                )
                xfce_script = "/run/edgeos-start-xfce"
                xfce_script_body = """#!/bin/sh
set -eu
exec >/tmp/edgeos-startxfce4-current.log 2>&1
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/edgeos-runtime-0}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/tmp/edgeos-xfce-config-0}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp/edgeos-xfce-cache-0}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-/tmp/edgeos-xfce-data-0}"
rm -rf "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$HOME/Desktop" "$HOME/Downloads" "$HOME/Templates" "$HOME/Public" "$HOME/Documents" "$HOME/Music" "$HOME/Pictures" "$HOME/Videos"
mkdir -p "$XDG_CONFIG_HOME/xfce4/xfconf/xfce-perchannel-xml"
cat >"$XDG_CONFIG_HOME/user-dirs.dirs" <<'EDGEOS_XDG_USER_DIRS'
XDG_DESKTOP_DIR="$HOME/Desktop"
XDG_DOWNLOAD_DIR="$HOME/Downloads"
XDG_TEMPLATES_DIR="$HOME/Templates"
XDG_PUBLICSHARE_DIR="$HOME/Public"
XDG_DOCUMENTS_DIR="$HOME/Documents"
XDG_MUSIC_DIR="$HOME/Music"
XDG_PICTURES_DIR="$HOME/Pictures"
XDG_VIDEOS_DIR="$HOME/Videos"
EDGEOS_XDG_USER_DIRS
cat >"$XDG_CONFIG_HOME/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml" <<'EDGEOS_XFCE_DESKTOP'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-desktop" version="1.0">
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitor0" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="0"/>
          <property name="color-style" type="int" value="0"/>
          <property name="rgba1" type="array">
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="1.0"/>
          </property>
          <property name="last-image" type="string" value="/usr/share/backgrounds/xfce/xfce-blue.jpg"/>
        </property>
      </property>
      <property name="monitorDefault" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="0"/>
          <property name="color-style" type="int" value="0"/>
          <property name="rgba1" type="array">
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="1.0"/>
          </property>
          <property name="last-image" type="string" value="/usr/share/backgrounds/xfce/xfce-blue.jpg"/>
        </property>
      </property>
      <property name="monitorS" type="empty">
        <property name="workspace0" type="empty">
          <property name="image-style" type="int" value="0"/>
          <property name="color-style" type="int" value="0"/>
          <property name="rgba1" type="array">
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="1.0"/>
          </property>
          <property name="last-image" type="string" value="/usr/share/backgrounds/xfce/xfce-blue.jpg"/>
        </property>
      </property>
    </property>
  </property>
</channel>
EDGEOS_XFCE_DESKTOP
cat >"$XDG_CONFIG_HOME/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml" <<'EDGEOS_XFWM4'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfwm4" version="1.0">
  <property name="general" type="empty">
    <property name="use_compositing" type="bool" value="false"/>
  </property>
</channel>
EDGEOS_XFWM4

# The EdgeOS verification VM currently runs Xorg on fbdev/virtio-gpu without
# kernel DRM acceleration.  Keep XFCE on GTK's software path so startup does
# not pull Mesa Gallium/LLVM through file-backed mmap and monopolize the guest
# before the session manager can create the panel/desktop.  These settings are
# intentionally scoped to this /run-only test launcher; the Alpine rootfs stays
# unmodified and normal Linux distributions can choose their own session env.
export GDK_GL=disable
export GDK_RENDERING=image
export GSK_RENDERER=cairo
export NO_AT_BRIDGE=1
export XDG_CURRENT_DESKTOP=XFCE
export DESKTOP_SESSION=xfce
export XDG_SESSION_DESKTOP=xfce

# A desktop installation normally seeds this through its setup utility.  The
# VMM verifier creates an ephemeral XDG home, so provide the standard directory
# association there and exercise Thunar through the normal xdg-open path.
cat >"$XDG_CONFIG_HOME/mimeapps.list" <<'EDGEOS_MIMEAPPS'
[Default Applications]
inode/directory=thunar.desktop;

[Added Associations]
inode/directory=thunar.desktop;
EDGEOS_MIMEAPPS
mkdir -p "$XDG_CONFIG_HOME/xfce4"
cat >"$XDG_CONFIG_HOME/xfce4/helpers.rc" <<'EDGEOS_XFCE_HELPERS'
FileManager=Thunar
TerminalEmulator=xfce4-terminal
EDGEOS_XFCE_HELPERS

# Start the real XFCE session under a per-run DBus session bus.  Keep this in
# the VMM launcher rather than writing rootfs policy: the kernel is still
# running unmodified XFCE binaries, and the cleanup path above removes stale
# per-run state before each verification.
xfce_step() {
    printf 'XFCE_STEP %s %s\\n' "$(date +%s 2>/dev/null || echo 0)" "$1"
}

xfce_step cleanup-stale
# Stale process cleanup is done by the parent serial command before this
# launcher is exec'd.  Keep the script itself out of pkill/procfs scans: under
# heavy X startup load that can block before XFCE has even started, which makes
# the readiness logs point at the wrong layer.

# XFCE is a normal Linux desktop stack and several components expect the
# system bus to exist before startxfce4 launches the session.  Alpine does not
# enable that service just because the dbus package is installed, so the VMM
# verifier starts the stock daemon in /run when available.  This does not patch
# XFCE or the rootfs; it mirrors the service state a regular Linux desktop boot
# provides and keeps kernel ABI failures visible instead of hidden behind
# multi-minute D-Bus connection timeouts.
if command -v dbus-daemon >/dev/null 2>&1; then
    mkdir -p /run/dbus
    if [ ! -S /run/dbus/system_bus_socket ] ||
       ! timeout 3 dbus-send --system --type=method_call --print-reply \
           --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames \
           >/tmp/edgeos-dbus-system-check.log 2>&1; then
        rm -f /run/dbus/system_bus_socket /run/dbus/pid
        dbus-daemon --system --fork >/tmp/edgeos-dbus-system.log 2>&1 || true
    fi
fi
if command -v rc-service >/dev/null 2>&1 && [ -x /etc/init.d/elogind ]; then
    rc-service elogind status >/dev/null 2>&1 ||
        rc-service elogind start </dev/null >/tmp/edgeos-elogind.log 2>&1 || true
fi

xfce_step start-session
unset DISPLAY
exec dbus-run-session -- sh -c '
printf "DBUS_SESSION_BUS_ADDRESS=%s\n" "$DBUS_SESSION_BUS_ADDRESS" >"/tmp/edgeos-dbus-EDGEOS_DISPLAY_NUM.env"
if command -v dbus-update-activation-environment >/dev/null 2>&1; then
    dbus-update-activation-environment \
        DBUS_SESSION_BUS_ADDRESS DISPLAY XDG_RUNTIME_DIR \
        >/tmp/edgeos-dbus-activation-env-EDGEOS_DISPLAY_NUM.log 2>&1 || true
fi
exec startxfce4 -- :EDGEOS_DISPLAY_NUM vtEDGEOS_VT_NUM -config EDGEOS_XORG_CONF -br -audit 0 -verbose 0 -logverbose 0
'
""".replace("EDGEOS_DISPLAY_NUM", str(args.display_num)).replace("EDGEOS_VT_NUM", str(args.vt)).replace("EDGEOS_XORG_CONF", xorg_conf).replace("EDGEOS_XORG_LOG", xorg_log)
                serial_write_guest_file(
                    fd,
                    serial_log,
                    xfce_script,
                    xfce_script_body.splitlines(),
                    f"{marker}_XFCE_SCRIPT",
                    args.timeout,
                )
                serial_run_marked(
                    fd,
                    serial_log,
                    f"chmod +x {xfce_script}; echo {marker}_XFCE_SCRIPT_CHMOD_$?",
                    f"{marker}_XFCE_SCRIPT_CHMOD_0",
                    args.timeout,
                )
            else:
                serial_run_marked(
                    fd,
                    serial_log,
                    "mkdir -p /tmp/edgeos-runtime-0; chmod 700 /tmp/edgeos-runtime-0; "
                    f"echo {marker}_XDG_RUNTIME_$?",
                    f"{marker}_XDG_RUNTIME_0",
                    args.timeout,
                )

        if window_manager:
            wm_bin = window_manager.split()[0]
            wm_log = f"/tmp/edgeos-x11-wm-{args.display_num}.log"
            if window_manager == "twm":
                # twm's interactive placement mode leaves users staring at an
                # empty X root until they click a placement outline.  Generate
                # a tiny per-boot config so VMM-launched X clients appear
                # immediately while keeping the guest rootfs policy untouched.
                twmrc = "/run/edgeos-twmrc"
                twmrc_body = 'NoGrabServer\\nNoIconManagers\\nRandomPlacement\\nDontMoveOff\\nUsePPosition "on"\\n'
                serial_run_marked(
                    fd,
                    serial_log,
                    f"printf {shlex.quote(twmrc_body)} >{twmrc}; echo {marker}_TWMRC_$?",
                    f"{marker}_TWMRC_0",
                    args.timeout,
                )
                window_manager = f"twm -f {twmrc}"
            if window_manager:
                print(f"starting window manager on DISPLAY={client_display}: {window_manager}")
            else:
                print(f"not starting an additional window manager on DISPLAY={client_display}")
            if desktop_xfce:
                cleanup_cmd = (
                    "for p in startxfce4 xinit Xorg xkbcomp dbus-run-session "
                    "dbus-launch xfce4-session xfwm4 xfdesktop "
                    "xfsettingsd xfconf-query xfconfd xfce4-panel xfce4-power-manager "
                    "xfce4-terminal Thunar tumblerd gvfsd gvfsd-trash gvfsd-metadata "
                    "at-spi-bus-launcher at-spi2-registryd twm xterm xclock; do "
                    "pkill -x \"$p\" >/dev/null 2>&1 || true; done; "
                    "rm -f /tmp/.X0-lock /tmp/.X1-lock /tmp/.X2-lock "
                    "/tmp/.X11-unix/X0 /tmp/.X11-unix/X1 /tmp/.X11-unix/X2 "
                    "/tmp/edgeos-dbus-0.env /tmp/edgeos-dbus-1.env /tmp/edgeos-dbus-2.env; "
                    "pkill -x xfce4-session >/dev/null 2>&1 || true; "
                    "pkill -x xfwm4 >/dev/null 2>&1 || true; "
                    "pkill -x xfdesktop >/dev/null 2>&1 || true; "
                    "pkill -x xfce4-panel >/dev/null 2>&1 || true; "
                    "pkill -x xfsettingsd >/dev/null 2>&1 || true; "
                    "pkill -x twm >/dev/null 2>&1 || true; "
                    "pkill -x dbus-launch >/dev/null 2>&1 || true"
                )
            else:
                cleanup_cmd = f"pkill -x {shlex.quote(wm_bin)} >/dev/null 2>&1 || true"
            if window_manager:
                serial_run_marked(
                    fd,
                    serial_log,
                    f"{cleanup_cmd}; ( {session_env} {window_manager} </dev/null >{wm_log} 2>&1 & ); echo {marker}_WM_LAUNCH",
                    f"{marker}_WM_LAUNCH",
                    args.timeout,
                )
            else:
                serial_run_marked(
                    fd,
                    serial_log,
                    f"{cleanup_cmd}; echo {marker}_WM_LAUNCH",
                    f"{marker}_WM_LAUNCH",
                    args.timeout,
                )
            if desktop_xfce:
                deadline = time.time() + args.timeout
                last_progress_report = 0.0
                last_ready_probe = 0.0
                xfce_ready_probe = (
                    "pidof xfce4-session >/dev/null && "
                    "pidof xfwm4 >/dev/null && "
                    "pidof xfdesktop >/dev/null && "
                    "pidof xfce4-panel >/dev/null"
                )
                while True:
                    now = time.time()
                    if now - last_ready_probe >= 5:
                        status = serial_check_status(
                            fd,
                            serial_log,
                            xfce_ready_probe,
                            f"{marker}_WM_READY",
                            5,
                        )
                        last_ready_probe = now
                        if status == 0:
                            break
                    if time.time() >= deadline:
                        diag_marker = f"{marker}_XFCE_DIAG_FINAL"
                        # Keep failure collection to one bounded shell command.
                        # Uploading a heredoc here competes with a saturated GUI
                        # workload and can leave the login shell consuming the
                        # diagnostic itself instead of reporting the blocked
                        # process states that we need to fix.
                        diag_cmd = (
                            f"echo {diag_marker}_BEGIN; echo ---processes---; "
                            "ps -ef | grep -E 'Xorg|xfce|xfwm|xfdesktop|xfsettings|xfconf|panel|Thunar|tumbler|dbus|gst|terminal|xclock' | grep -v grep || true; "
                            "echo ---proc-status---; "
                            "for p in $(pidof xfce4-session xfwm4 xfdesktop xfsettingsd xfce4-panel Thunar tumblerd dbus-daemon gst-plugin-scanner xfce4-terminal Xorg 2>/dev/null); do "
                            "echo ---PID-$p---; sed -n '1,120p' /proc/$p/status 2>&1; done; "
                            f"echo ---wm-log---; tail -80 {wm_log} 2>&1 || true; "
                            "echo ---component-logs---; "
                            "tail -40 /tmp/edgeos-xfsettingsd.log /tmp/edgeos-xfwm4.log /tmp/edgeos-xfdesktop.log /tmp/edgeos-xfce4-panel.log /tmp/edgeos-tumbler.log /tmp/edgeos-x11-thunar-*.log /tmp/edgeos-x11-xfce4-terminal-*.log 2>&1 || true; "
                            f"echo ---x-windows---; DISPLAY={client_display} timeout 5 xwininfo -root -tree 2>&1 | head -80 || true; "
                            f"echo {diag_marker}_END"
                        )
                        serial_run_marked(
                            fd,
                            serial_log,
                            diag_cmd,
                            f"{diag_marker}_END",
                            45,
                        )
                        raise VmError("XFCE did not start xfwm4, xfdesktop, and xfce4-panel; diagnostics were written to the serial log")
                    if now - last_progress_report >= 30:
                        print("waiting for XFCE components...")
                        detail_marker = f"{marker}_XFCE_WAIT"
                        detail_cmd = (
                            f"echo {detail_marker}_BEGIN; "
                            "for p in xfce4-session xfwm4 xfdesktop xfce4-panel; do "
                            "printf '%s=' \"$p\"; "
                            "pid=$(pidof \"$p\" 2>/dev/null | awk '{print $1}'); "
                            "if [ -n \"$pid\" ]; then echo yes pid=$pid; "
                            "printf 'syscall='; cat /proc/$pid/syscall 2>/dev/null || echo unavailable; "
                            "else echo no; fi; "
                            "done; echo session-log:; "
                            f"tail -12 {wm_log} 2>/dev/null || true; "
                            f"echo {detail_marker}_END"
                        )
                        try:
                            serial_run_marked(
                                fd,
                                serial_log,
                                detail_cmd,
                                f"{detail_marker}_END",
                                10,
                            )
                        except VmError:
                            pass
                        last_progress_report = now
                    time.sleep(1)
                # EWMH properties are published late during XFWM startup, so
                # wait for all session components before checking them.  The
                # root window's _NET_SUPPORTING_WM_CHECK reference is the
                # standard proof that a window manager owns the screen; XFWM
                # does not promise to expose a top-level window named "Xfwm4".
                xfwm_managed_probe = (
                    f"DISPLAY={client_display} timeout 5 sh -c '"
                    "wm=$(xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | "
                    "sed -n \"s/.*# \\(0x[0-9a-fA-F]*\\).*/\\1/p\"); "
                    "[ -n \"$wm\" ] && "
                    "xprop -id \"$wm\" _NET_WM_NAME WM_NAME 2>/dev/null | "
                    "grep -qi xfwm4'"
                )
                # The same timeout chosen for the full desktop launch applies
                # to XFWM ownership.  On fbdev, XFWM can publish its EWMH check
                # window several minutes after the component processes appear
                # while icon/theme workers finish their first cold-cache pass.
                # A separate 90-second cap killed a healthy Xorg session before
                # the caller's requested 5-10 minute validation window elapsed.
                managed_deadline = time.time() + args.timeout
                while time.time() < managed_deadline:
                    if serial_check_status(
                        fd,
                        serial_log,
                        xfwm_managed_probe,
                        f"{marker}_XFWM_MANAGED",
                        10,
                    ) == 0:
                        break
                    time.sleep(2)
                else:
                    diag_marker = f"{marker}_XFWM_DIAG"
                    serial_run_marked(
                        fd,
                        serial_log,
                        f"echo {diag_marker}_BEGIN; "
                        f"DISPLAY={client_display} timeout 5 xprop -root 2>&1 || true; "
                        f"DISPLAY={client_display} timeout 5 xwininfo -root -tree 2>&1 | head -120 || true; "
                        f"tail -80 {wm_log} /tmp/edgeos-startxfce4-current.log 2>&1 || true; "
                        f"echo {diag_marker}_END",
                        f"{diag_marker}_END",
                        30,
                    )
                    raise VmError(
                        "XFWM started but did not publish _NET_SUPPORTING_WM_CHECK"
                    )
                activation_env_cmd = (
                    f"DISPLAY={client_display}; export DISPLAY; "
                    "XDG_RUNTIME_DIR=/tmp/edgeos-runtime-0; export XDG_RUNTIME_DIR; "
                    f"if [ -f /tmp/edgeos-dbus-{args.display_num}.env ]; then "
                    f". /tmp/edgeos-dbus-{args.display_num}.env; "
                    "export DBUS_SESSION_BUS_ADDRESS; fi; "
                    "if command -v dbus-update-activation-environment >/dev/null 2>&1; then "
                    "dbus-update-activation-environment "
                    "DBUS_SESSION_BUS_ADDRESS DISPLAY XDG_RUNTIME_DIR "
                    f">/tmp/edgeos-dbus-activation-env-{args.display_num}.log 2>&1; "
                    f"echo {marker}_DBUS_ACTIVATION_ENV_$?; "
                    "else "
                    f"echo {marker}_DBUS_ACTIVATION_ENV_127; "
                    "fi"
                )
                serial_run_marked(
                    fd,
                    serial_log,
                    activation_env_cmd,
                    f"{marker}_DBUS_ACTIVATION_ENV_0",
                    args.timeout,
                )
            else:
                serial_run_marked(
                    fd,
                    serial_log,
                    f"sleep 2; pgrep {shlex.quote(wm_bin)} >/dev/null; echo {marker}_WM_READY_$?",
                    f"{marker}_WM_READY_0",
                    args.timeout,
                )

        for app in app_names:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", app.split()[0])
            app_log = f"/tmp/edgeos-x11-{safe_name}-{args.display_num}.log"
            app_pidfile = f"/tmp/edgeos-x11-{safe_name}-{args.display_num}.pid"
            app_statusfile = f"/tmp/edgeos-x11-{safe_name}-{args.display_num}.status"
            app_session_setup = (
                f"export DISPLAY={client_display}; export XDG_RUNTIME_DIR=/tmp/edgeos-runtime-0; "
                "export NO_AT_BRIDGE=1 GDK_GL=disable GDK_RENDERING=image "
                "GSK_RENDERER=cairo; "
            )
            if desktop_xfce:
                app_session_setup += (
                    f"if [ -f /tmp/edgeos-dbus-{args.display_num}.env ]; then "
                    f". /tmp/edgeos-dbus-{args.display_num}.env; "
                    "export DBUS_SESSION_BUS_ADDRESS; fi; "
                )
            print(f"launching on DISPLAY={client_display}: {app}")
            app_launch_body = (
                f"{app_session_setup}"
                f"rm -f {app_statusfile}; "
                f"({app} </dev/null >{app_log} 2>&1; "
                f"echo $? >{app_statusfile}) & "
                f"echo $! >{app_pidfile}"
            )
            serial_run_marked(
                fd,
                serial_log,
                f"sh -c {shlex.quote(app_launch_body)}; "
                f"echo {marker}_APP_{safe_name}_LAUNCH",
                f"{marker}_APP_{safe_name}_LAUNCH",
                args.timeout,
            )
            app_status = serial_check_status(
                fd,
                serial_log,
                x11_application_alive_command(app_pidfile, app_statusfile),
                f"{marker}_APP_{safe_name}",
                min(args.timeout, 15),
            )
            if app_status != 0:
                raise VmError(
                    f"X11 application failed during startup: {app}; "
                    f"guest log: {app_log}"
                )
        if desktop_xfce:
            # Let XFCE own root-window painting after the session is live.
            # A verifier-side xsetroot/xrefresh after panel/client mapping can
            # repaint the fbdev root over child windows and then depend on a
            # second expose cycle to recover. That masks the kernel ABI under
            # test and makes screenshots look blank even when xfdesktop/panel
            # are running. The Xorg readiness probe above already proved a
            # root paint can reach /dev/fb0; full desktop verification should
            # now observe unmodified XFCE drawing.
            serial_run_marked(
                fd,
                serial_log,
                f"echo {marker}_XFCE_REPAINT_0",
                f"{marker}_XFCE_REPAINT_0",
                args.timeout,
            )
        # Do not attempt a no-WM SetInputFocus here.  xterm's top-level window
        # can reject X_SetInputFocus with BadMatch; a real WM or a user click
        # will focus the terminal.  Keeping this path passive avoids serial
        # deadlocks while still launching a visible terminal.

        if not desktop_xfce and not args.no_start_server:
            # Minimal X11 smoke tests sometimes need one root repaint after
            # clients map.  Do not do this for XFCE: on the current EdgeOS
            # fbdev/Xorg stack, full-screen root/temp-window repaints can
            # overwrite the visible surface and expose a missing child-window
            # repaint path, leaving a gray/black desktop even though X windows
            # are mapped.  A full desktop should paint itself without VMM-side
            # root-fill probes after session launch.
            repaint_log = f"/tmp/edgeos-x11-repaint-{args.display_num}.log"
            repaint_out = f"/tmp/edgeos-x11-repaint-{args.display_num}.out"
            repaint_script = (
                f"DISPLAY={client_display}; export DISPLAY; "
                "XDG_RUNTIME_DIR=/tmp/edgeos-runtime-0; export XDG_RUNTIME_DIR; "
                f"for d in 1 4 10 20; do "
                f"sleep $d; "
                f"xsetroot -cursor_name left_ptr -solid black >>{repaint_log} 2>&1 || true; "
                f"done"
            )
            serial_run_marked(
                fd,
                serial_log,
                f"DISPLAY={client_display} xsetroot -cursor_name left_ptr -solid black; echo {marker}_REFRESH_$?",
                f"{marker}_REFRESH_0",
                args.timeout,
            )

        # Leave the interactive serial shell in the same state users expect
        # after entering an X11 session: subsequent commands can simply run
        # `xterm`, `xclock`, etc.  This intentionally affects only the current
        # serial login shell; persistent rootfs policy belongs to userland.
        serial_run_marked(
            fd,
            serial_log,
            f"export DISPLAY={client_display}; export XDG_RUNTIME_DIR=/tmp/edgeos-runtime-0; echo {marker}_DISPLAY_EXPORT_$?",
            f"{marker}_DISPLAY_EXPORT_0",
            args.timeout,
        )
        serial_run_marked(
            fd,
            serial_log,
            f"stty echo 2>/dev/null || true; echo {marker}_STTY_ECHO",
            f"{marker}_STTY_ECHO",
            args.timeout,
        )
        echo_disabled = False
        if not desktop_xfce and not args.no_start_server:
            serial_run_marked(
                fd,
                serial_log,
                f"nohup sh -c {shlex.quote(repaint_script)} >{repaint_out} 2>&1 & echo {marker}_REPAINT_$?",
                f"{marker}_REPAINT_0",
                args.timeout,
            )

        print(f"DISPLAY={client_display} is ready")
        print(f"guest Xorg log: {xorg_log}")
        print(f"serial log: {rel(serial_log)}")
    finally:
        _serial_pending_status.pop(fd, None)
        if echo_disabled:
            try:
                write_serial_line(
                    fd,
                    "stty echo 2>/dev/null || true",
                    delay=SERIAL_MARKED_COMMAND_DELAY,
                    eol="\n",
                    drain=False,
                )
            except OSError:
                pass
        drainer = _serial_drainers.pop(fd, None)
        if drainer:
            # The receive thread owns a dup of the connected socket.  Closing
            # only the writer leaves QEMU attached to that stale client and a
            # later VMM/terminal connection can type forever without receiving
            # output.  shutdown(2) applies to the shared socket description,
            # wakes the drainer, and lets QEMU accept the next serial client.
            shutdown_socket = socket.socket(fileno=os.dup(fd))
            try:
                shutdown_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                shutdown_socket.close()
        os.close(fd)
        if drainer:
            drainer[1].join(timeout=2)


def command_stop(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    if running_pid(cfg) is None:
        cleanup_runtime_files(cfg)
        print(f"{args.name} is already stopped")
        return
    terminate_vm_process(cfg, force=args.force)
    cleanup_runtime_files(cfg)
    print(f"{'killed' if args.force else 'stopped'} {args.name}")


def close_serial_automation(fd: int) -> None:
    drainer = _serial_drainers.pop(fd, None)
    if drainer:
        shutdown_socket = socket.socket(fileno=os.dup(fd))
        try:
            shutdown_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            shutdown_socket.close()
    try:
        os.close(fd)
    except OSError:
        pass
    if drainer:
        drainer[1].join(timeout=2)


def acquire_guest_shell(cfg: dict[str, Any], timeout: float, login: str, password: str) -> tuple[int, Path]:
    if running_pid(cfg) is None:
        raise VmError(f"VM is not running: {cfg['_name']}")
    fd, _channel = open_serial_pty(cfg)
    serial_log = Path(str(cfg["_dir"])) / "serial.log"
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            state = serial_prompt_state(tail_text(serial_log, max_lines=100))
            if state == "shell":
                return fd, serial_log
            if state == "login":
                start = serial_log_size(serial_log)
                write_serial_line(fd, login, eol="\r")
                if wait_for_text(serial_log, "Password:", 10.0, start):
                    write_serial_line(fd, password)
                if wait_for_serial_prompt(serial_log, "shell", min(30.0, timeout)):
                    return fd, serial_log
            elif state == "password":
                write_serial_line(fd, password)
                if wait_for_serial_prompt(serial_log, "shell", min(30.0, timeout)):
                    return fd, serial_log
            time.sleep(0.25)
    except BaseException:
        close_serial_automation(fd)
        raise
    close_serial_automation(fd)
    raise VmError("guest shell prompt was not reached on the serial console")


def command_guest_push(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        raise VmError(f"host file not found: {source}")
    if source.stat().st_size > args.max_size_mb * 1024 * 1024:
        raise VmError(f"file exceeds the {args.max_size_mb} MB serial transfer limit")
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    lines = [encoded[index:index + 512] for index in range(0, len(encoded), 512)]
    marker = f"EDGEOS_PUSH_{os.getpid()}_{int(time.time())}"
    fd, serial_log = acquire_guest_shell(cfg, args.timeout, args.login, args.password)
    temporary = f"/tmp/.edgeos-transfer-{os.getpid()}.b64"
    destination = shlex.quote(args.destination)
    try:
        serial_run_marked(fd, serial_log, f"stty -echo 2>/dev/null || true; echo {marker}_READY", f"{marker}_READY", args.timeout)
        serial_write_guest_file(fd, serial_log, temporary, lines, marker, args.timeout)
        serial_run_marked(
            fd,
            serial_log,
            f"base64 -d {shlex.quote(temporary)} > {destination} && rm -f {shlex.quote(temporary)}; echo {marker}_DONE_$?",
            f"{marker}_DONE_0",
            args.timeout,
        )
        print(f"sent {source} to {args.name}:{args.destination}")
    finally:
        try:
            write_serial_line(fd, "stty echo 2>/dev/null || true", delay=0.002, eol="\n", drain=False)
        except OSError:
            pass
        close_serial_automation(fd)


def command_guest_pull(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    destination = Path(args.destination).expanduser().resolve()
    marker = f"EDGEOS_PULL_{os.getpid()}_{int(time.time())}"
    fd, serial_log = acquire_guest_shell(cfg, args.timeout, args.login, args.password)
    start = serial_log_size(serial_log)
    try:
        command = (
            f"stty -echo 2>/dev/null || true; echo {marker}_BEGIN; "
            f"base64 {shlex.quote(args.source)}; rc=$?; echo {marker}_END_$rc"
        )
        write_serial_line(fd, command, delay=SERIAL_MARKED_COMMAND_DELAY, eol="\n", drain=False)
        if not wait_for_text_with_progress(serial_log, f"{marker}_END_0", 15.0, args.timeout, start):
            raise VmError(f"guest file transfer failed or timed out: {args.source}")
        payload = read_bytes(serial_log)[start:].decode("ascii", errors="ignore").replace("\r", "")
        begin = payload.rfind(f"{marker}_BEGIN")
        end = payload.rfind(f"{marker}_END_0")
        if begin < 0 or end <= begin:
            raise VmError("guest transfer markers were not captured")
        encoded = "".join(payload[begin + len(f"{marker}_BEGIN"):end].split())
        data = base64.b64decode(encoded, validate=True)
        if len(data) > args.max_size_mb * 1024 * 1024:
            raise VmError(f"file exceeds the {args.max_size_mb} MB serial transfer limit")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.transfer-{os.getpid()}")
        temporary.write_bytes(data)
        os.replace(temporary, destination)
        print(f"received {args.name}:{args.source} into {destination}")
    finally:
        try:
            write_serial_line(fd, "stty echo 2>/dev/null || true", delay=0.002, eol="\n", drain=False)
        except OSError:
            pass
        close_serial_automation(fd)


def command_install_guest_tools(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    if not GUEST_TOOLS_SCRIPT.is_file():
        raise VmError(f"Guest Tools payload not found: {GUEST_TOOLS_SCRIPT}")
    marker = f"EDGEOS_TOOLS_{os.getpid()}_{int(time.time())}"
    fd, serial_log = acquire_guest_shell(cfg, args.timeout, args.login, args.password)
    payload = GUEST_TOOLS_SCRIPT.read_text(encoding="utf-8").splitlines()
    shares = []
    for folder in cfg.get("shared_folders", []):
        access = "ro" if bool(folder.get("read_only", False)) else "rw"
        mount_path = str(folder.get("mount_path", f"/mnt/hgfs/{folder['tag']}"))
        shares.append(f"{folder['tag']}|{mount_path}|{access}")
    if not shares:
        shares.append("# no shared folders configured")
    try:
        serial_run_marked(fd, serial_log, f"stty -echo 2>/dev/null || true; echo {marker}_READY", f"{marker}_READY", args.timeout)
        serial_write_guest_file(fd, serial_log, "/tmp/edgeos-guest-tools.sh", payload, f"{marker}_SCRIPT", args.timeout)
        serial_write_guest_file(fd, serial_log, "/tmp/edgeos-workstation-shares", shares, f"{marker}_SHARES", args.timeout)
        command = (
            "chmod 0755 /tmp/edgeos-guest-tools.sh && "
            "/tmp/edgeos-guest-tools.sh install && "
            "mkdir -p /etc && cp /tmp/edgeos-workstation-shares /etc/edgeos-workstation-shares && "
            "/usr/local/sbin/edgeos-guest-tools mount-all >/tmp/edgeos-guest-tools-mount.log 2>&1 || true; "
            f"/usr/local/sbin/edgeos-guest-tools status; echo {marker}_DONE_$?"
        )
        serial_run_marked(fd, serial_log, command, f"{marker}_DONE_0", args.timeout)
        print(f"installed EdgeOS Workstation Guest Tools in {args.name}")
    finally:
        try:
            write_serial_line(fd, "stty echo 2>/dev/null || true", delay=0.002, eol="\n", drain=False)
        except OSError:
            pass
        close_serial_automation(fd)


def require_powered_off(cfg: dict[str, Any], operation: str) -> None:
    if running_pid(cfg) is not None:
        raise VmError(f"power off {cfg['_name']} before {operation}")


def safe_snapshot_name(value: str) -> str:
    if not value or value in (".", ".."):
        raise VmError("snapshot name cannot be empty")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if any(character not in allowed for character in value):
        raise VmError("snapshot names may contain only letters, numbers, periods, underscores, and hyphens")
    return value


def snapshot_disk_targets(cfg: dict[str, Any]) -> list[tuple[str, Path, str]]:
    targets = [("primary", vm_path(cfg, "rootfs_path"), "primary")]
    for index, device in enumerate(cfg.get("storage", []), start=1):
        targets.append((f"disk-{index}", resolve_storage_path(cfg, str(device["path"])), f"storage:{index - 1}"))
    if cfg.get("architecture") in ("arm64", "aarch64"):
        uefi = vm_path(cfg, "uefi_path")
        if uefi.is_file():
            targets.append(("arm64-uefi", uefi, "arm64-uefi"))
    return targets


def atomic_restore_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise VmError(f"snapshot disk not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.restore-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def command_snapshot(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    require_powered_off(cfg, "creating a disk snapshot")
    stamp = safe_snapshot_name(args.snapshot or time.strftime("%Y%m%d-%H%M%S"))
    snap_dir = Path(str(cfg["_dir"])) / "snapshots"
    destination = snap_dir / stamp
    if destination.exists() or (snap_dir / f"{stamp}.img").exists():
        raise VmError(f"snapshot already exists: {stamp}")
    snap_dir.mkdir(parents=True, exist_ok=True)
    temporary = snap_dir / f".{stamp}.creating-{os.getpid()}"
    temporary.mkdir(parents=True, exist_ok=False)
    disks: list[dict[str, Any]] = []
    try:
        for filename, source, target in snapshot_disk_targets(cfg):
            if not source.is_file():
                raise VmError(f"disk not found: {source}")
            snapshot_file = f"{filename}{source.suffix or '.img'}"
            copy_file(source, temporary / snapshot_file)
            disks.append({
                "file": snapshot_file,
                "target": target,
                "source": str(source),
                "size": source.stat().st_size,
            })
        save_json(temporary / "snapshot.json", {
            "schema_version": 1,
            "name": stamp,
            "vm_name": args.name,
            "created_at": int(time.time()),
            "description": args.description or "",
            "disks": disks,
        })
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(f"snapshot created: {rel(destination)}")


def command_restore(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    require_powered_off(cfg, "restoring a snapshot")
    name = safe_snapshot_name(args.snapshot)
    snapshot_root = Path(str(cfg["_dir"])) / "snapshots"
    directory = snapshot_root / name
    legacy = snapshot_root / f"{name}.img"
    if legacy.is_file() and not directory.exists():
        atomic_restore_file(legacy, vm_path(cfg, "rootfs_path"))
        print(f"restored {args.name} from legacy snapshot {name}")
        return
    manifest_path = directory / "snapshot.json"
    try:
        manifest = load_json(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise VmError(f"invalid snapshot manifest: {manifest_path}: {exc}") from exc
    disks = manifest.get("disks", [])
    if not isinstance(disks, list) or not disks:
        raise VmError(f"snapshot has no disks: {name}")
    for disk in disks:
        target = str(disk.get("target", ""))
        if target == "primary":
            destination = vm_path(cfg, "rootfs_path")
        elif target == "arm64-uefi":
            destination = vm_path(cfg, "uefi_path")
        elif target.startswith("storage:"):
            index = int(target.partition(":")[2])
            storage = cfg.get("storage", [])
            if index >= len(storage):
                raise VmError(f"snapshot references missing storage device {index}")
            destination = resolve_storage_path(cfg, str(storage[index]["path"]))
        else:
            raise VmError(f"snapshot contains an unknown disk target: {target}")
        atomic_restore_file(directory / str(disk["file"]), destination)
    print(f"restored {args.name} from {args.snapshot}")


def command_snapshot_delete(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    name = safe_snapshot_name(args.snapshot)
    snapshot_root = Path(str(cfg["_dir"])) / "snapshots"
    directory = snapshot_root / name
    legacy = snapshot_root / f"{name}.img"
    if directory.is_dir():
        shutil.rmtree(directory)
    elif legacy.is_file():
        legacy.unlink()
    else:
        raise VmError(f"snapshot not found: {name}")
    print(f"deleted snapshot {name} from {args.name}")


def command_snapshot_list(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    snapshot_root = Path(str(cfg["_dir"])) / "snapshots"
    snapshots: list[dict[str, Any]] = []
    for path in sorted(snapshot_root.iterdir()) if snapshot_root.is_dir() else []:
        if path.is_dir() and (path / "snapshot.json").is_file():
            try:
                snapshots.append(load_json(path / "snapshot.json"))
            except (OSError, json.JSONDecodeError):
                snapshots.append({"name": path.name, "invalid": True})
        elif path.suffix == ".img":
            snapshots.append({"name": path.stem, "created_at": int(path.stat().st_mtime), "legacy": True})
    if args.json:
        print(json.dumps(snapshots, indent=2, sort_keys=True))
    else:
        for snapshot in snapshots:
            print(snapshot.get("name", "unknown"))


def clone_disk(source: Path, destination: Path, linked: bool) -> tuple[Path, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not linked:
        copy_file(source, destination)
        return destination, "qcow2" if destination.suffix == ".qcow2" else "raw"
    qemu_img = need_tool("qemu-img")
    info = subprocess.run([qemu_img, "info", "--output=json", str(source)], text=True, capture_output=True, check=False)
    if info.returncode != 0:
        raise VmError((info.stderr or info.stdout).strip())
    source_format = str(json.loads(info.stdout).get("format", "raw"))
    destination = destination.with_suffix(".qcow2")
    run([qemu_img, "create", "-f", "qcow2", "-F", source_format, "-b", str(source.resolve()), str(destination)])
    return destination, "qcow2"


def command_clone(args: argparse.Namespace) -> None:
    src = load_vm(args.source)
    require_powered_off(src, "cloning")
    dst_dir = instance_dir(args.name)
    if dst_dir.exists():
        raise VmError(f"VM already exists: {args.name}")
    temporary = dst_dir.with_name(f".{dst_dir.name}.cloning-{os.getpid()}")
    temporary.mkdir(parents=True)
    cfg = {k: v for k, v in src.items() if not k.startswith("_")}
    cfg["_name"] = args.name
    cfg["_dir"] = str(temporary)
    try:
        source_primary = vm_path(src, "rootfs_path")
        primary_name = "rootfs.qcow2" if args.linked else Path(str(cfg.get("rootfs_path", "rootfs.img"))).name
        primary_path, primary_format = clone_disk(source_primary, temporary / primary_name, args.linked)
        cfg["rootfs_path"] = primary_path.name
        cfg["rootfs_format"] = primary_format
        cloned_storage: list[dict[str, Any]] = []
        for index, device in enumerate(src.get("storage", []), start=1):
            source = resolve_storage_path(src, str(device["path"]))
            suffix = ".qcow2" if args.linked else (source.suffix or ".img")
            disk_path, disk_format = clone_disk(source, temporary / f"disk-{index}{suffix}", args.linked)
            cloned = dict(device)
            cloned["path"] = disk_path.name
            cloned["format"] = disk_format
            cloned_storage.append(cloned)
        cfg["storage"] = cloned_storage
        for key in ("iso_path", "uefi_path"):
            if key not in src:
                continue
            source = vm_path(src, key)
            if source.is_file():
                destination = temporary / Path(str(cfg.get(key, source.name))).name
                copy_file(source, destination)
                cfg[key] = destination.name
        save_config(temporary / "vm.json", {key: value for key, value in cfg.items() if not key.startswith("_")})
        os.replace(temporary, dst_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(f"{'linked' if args.linked else 'full'} clone created: {args.name}")


def command_delete(args: argparse.Namespace) -> None:
    vdir = instance_dir(args.name)
    if not vdir.exists():
        raise VmError(f"VM not found: {args.name}")
    if not args.yes:
        raise VmError("refusing to delete without --yes")
    shutil.rmtree(vdir)
    print(f"deleted VM {args.name}")


def command_tap(args: argparse.Namespace) -> None:
    script = REPO_ROOT / "tools/net/setup_tap_nat.sh"
    cmd = ["sudo", str(script), args.ifname]
    if args.uplink:
        env = os.environ.copy()
        env["EDGEOS_UPLINK_IF"] = args.uplink
        print("+ EDGEOS_UPLINK_IF=" + args.uplink + " " + " ".join(cmd))
        subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, check=True)
    else:
        run(cmd)


def command_macvtap(args: argparse.Namespace) -> None:
    script = REPO_ROOT / "tools/net/setup_macvtap.sh"
    cmd = ["sudo"]
    if args.mode != "bridge" and not args.parent:
        cmd += ["env", f"EDGEOS_MACVTAP_MODE={args.mode}"]
    cmd += [str(script), args.ifname]
    if args.parent:
        cmd += [args.parent, args.mode]
    run(cmd)


def command_template_save(args: argparse.Namespace) -> None:
    cfg = load_vm(args.vm)
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    save_json(TEMPLATES_DIR / f"{args.name}.json", data)
    print(f"saved template {args.name}")


def command_template_list(_: argparse.Namespace) -> None:
    ensure_state()
    names = sorted(TEMPLATE_DEFAULTS)
    names += sorted(path.stem for path in TEMPLATES_DIR.glob("*.json"))
    for name in names:
        print(name)


def command_set_resolution(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    if not supports_boot_display_resolution(cfg):
        raise VmError(
            "boot display resolution selection currently requires an ARM64 ramfb VM"
        )
    resolution = normalize_display_resolution(args.resolution)
    cfg["display_resolution"] = resolution
    save_vm(args.name, cfg)
    if running_pid(cfg) is None and vm_path(cfg, "uefi_path").is_file():
        write_arm64_video_config(cfg)
    print(f"display resolution for {args.name} set to {resolution}")
    print("the new mode will be selected by UEFI on the next VM start")


def command_set_refresh_rate(args: argparse.Namespace) -> None:
    cfg = load_vm(args.name)
    refresh_hz = normalize_display_refresh_hz(args.refresh_hz)
    cfg["display_refresh_hz"] = refresh_hz
    save_vm(args.name, cfg)
    if running_pid(cfg) is None:
        if cfg.get("architecture", "x86_64") in ("arm64", "aarch64"):
            if vm_path(cfg, "uefi_path").is_file():
                write_arm64_command_line(cfg)
        elif (vm_path(cfg, "out_dir") / "edgeos.bin").is_file():
            write_boot_iso(cfg)
    print(f"display refresh for {args.name} set to {refresh_hz} Hz")
    print("the new cadence will be selected on the next VM start")


def build_jobs_argument(value: str) -> int:
    try:
        jobs = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("jobs must be an integer") from exc
    if not 0 <= jobs <= 256:
        raise argparse.ArgumentTypeError("jobs must be between 0 and 256")
    return jobs


def add_common_create_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--template", action="append", default=["edgeos"], help="template/profile name; may be repeated")
    p.add_argument("--description", default="")
    p.add_argument("--architecture", "--arch", choices=["x86_64", "arm64", "aarch64"], help="guest architecture")
    p.add_argument("--import-rootfs", help="copy an existing ext2/ext4 rootfs image into the VM")
    p.add_argument("--rootfs-size-mb", type=int)
    p.add_argument("--cpus", type=int)
    p.add_argument("--cpu-sockets", type=int)
    p.add_argument("--cpu-cores", type=int)
    p.add_argument("--cpu-threads", type=int)
    p.add_argument(
        "--jobs",
        type=build_jobs_argument,
        help="parallel build jobs (0 selects all available host CPUs)",
    )
    p.add_argument("--memory")
    p.add_argument("--accelerator", choices=["auto", "kvm", "hvf", "nvmm", "whpx", "tcg"])
    p.add_argument("--cpu-model", help="QEMU CPU model override")
    p.add_argument("--usb", help="USB/input mode: virtio-input, off, uhci-mouse, xhci-mouse, xhci-keyboard, xhci-kbd-uhci-mouse, or xhci-input")
    p.add_argument("--gpu", help="GPU model: std, virtio-gpu, virtio-vga, virtio-gpu-gl-pci, virtio-vga-gl, qxl, bochs-display, or none")
    p.add_argument("--virgl", action="store_true", help="record that the VM wants VirGL; use --gpu virtio-gpu-gl-pci for the experimental QEMU GL device")
    p.add_argument("--display-backend", help="QEMU display backend for window mode, e.g. default, gtk, gtk,gl=on, or sdl")
    p.add_argument("--disk-controller", help="root disk controller: nvme, virtio-blk, ide, or a QEMU device model")
    p.add_argument("--disk-cache", choices=["none", "writeback", "writethrough", "unsafe"])
    p.add_argument("--disk-aio", choices=["native", "threads", "io_uring"])
    p.add_argument("--disk-discard", choices=["unmap", "ignore"])
    p.add_argument("--disk-read-only", action="store_true")
    p.add_argument("--firmware-mode", choices=["auto", "uefi", "bios"])
    p.add_argument("--boot-order", choices=["d", "c", "n", "dc", "cd", "cdn", "dcn"])
    p.add_argument("--no-boot-menu", action="store_true")
    p.add_argument("--boot-delay-ms", type=int)
    p.add_argument("--rtc-base", choices=["utc", "localtime"])
    p.add_argument("--sound", choices=["none", "hda", "ac97"])
    p.add_argument("--balloon", action="store_true")
    p.add_argument("--rng", action="store_true")
    p.add_argument("--desktop", choices=["console", "xfce"])
    p.add_argument("--hostname", help="guest hostname; defaults to a sanitized VM name")
    p.add_argument(
        "--net",
        action="append",
        help="network spec, e.g. user,model=e1000, tap,ifname=tap0, bridge,bridge=br0, or macvtap,ifname=edge-macvtap0",
    )
    p.add_argument("--reconfigure", action="store_true", help="regenerate the architecture kernel configuration")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EdgeOS Virtual Machine Manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a persistent VM")
    c.add_argument("name")
    add_common_create_flags(c)
    c.set_defaults(func=command_create)

    lp = sub.add_parser("list", help="list VMs")
    lp.set_defaults(func=command_list)

    sp = sub.add_parser("show", help="show VM JSON config")
    sp.add_argument("name")
    sp.set_defaults(func=command_show)

    up = sub.add_parser("update-kernel", help="compile latest kernel and boot ISO without modifying rootfs")
    up.add_argument("name")
    up.add_argument("--reconfigure", action="store_true")
    up.add_argument(
        "--jobs",
        type=build_jobs_argument,
        help="parallel build jobs (0 selects all available host CPUs)",
    )
    up.set_defaults(func=command_update_kernel)

    resolution = sub.add_parser(
        "set-resolution",
        help="select the ARM64 ramfb resolution for the next VM start",
    )
    resolution.add_argument("name")
    resolution.add_argument(
        "resolution",
        help="preset or custom WIDTHxHEIGHT, up to 7680x4320 and 128 MiB",
    )
    resolution.set_defaults(func=command_set_resolution)

    refresh = sub.add_parser(
        "set-refresh-rate",
        help="select the guest display refresh rate for the next VM start",
    )
    refresh.add_argument("name")
    refresh.add_argument(
        "refresh_hz",
        type=int,
        help="integer refresh rate from 1 through 10000 Hz",
    )
    refresh.set_defaults(func=command_set_refresh_rate)

    st = sub.add_parser("start", help="start a VM on the serial console")
    st.add_argument("name")
    st.add_argument("--background", action="store_true", help="run QEMU in background and log serial output")
    st.add_argument("--update-kernel", action="store_true", help="rebuild kernel before boot")
    st.add_argument("--no-kvm", action="store_true", help="boot with TCG instead of KVM for hosts without KVM access")
    st.add_argument("--machine", help="temporary QEMU machine override")
    st.add_argument("--cpu-model", help="temporary QEMU CPU model override")
    st.add_argument("--memory", help="temporary QEMU memory override, e.g. 2048M")
    st.add_argument("--accelerator", choices=["auto", "kvm", "hvf", "nvmm", "whpx", "tcg"], help="temporary QEMU accelerator override")
    st.add_argument(
        "--display",
        default="none",
        help="QEMU display backend: none, window, gtk, sdl, curses, etc. Use window for a normal QEMU VM window.",
    )
    st.add_argument("--dry-run", action="store_true")
    st.set_defaults(func=command_start)

    status = sub.add_parser("status", help="query authoritative VM runtime status")
    status.add_argument("name")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=command_status)

    shutdown = sub.add_parser("shutdown", help="request guest shutdown through QMP")
    shutdown.add_argument("name")
    shutdown.add_argument("--timeout", type=float, default=30.0)
    shutdown.add_argument("--force", action="store_true", help="terminate QEMU if the guest does not stop")
    shutdown.set_defaults(func=command_shutdown)

    reset = sub.add_parser("reset", help="reset a running VM through QMP")
    reset.add_argument("name")
    reset.set_defaults(func=command_reset)

    pause = sub.add_parser("pause", help="pause virtual CPUs through QMP")
    pause.add_argument("name")
    pause.set_defaults(func=command_pause)

    resume = sub.add_parser("resume", help="resume a paused VM through QMP")
    resume.add_argument("name")
    resume.set_defaults(func=command_resume)

    suspend = sub.add_parser("suspend", help="save VM memory and device state, then stop QEMU")
    suspend.add_argument("name")
    suspend.add_argument("--timeout", type=float, default=120.0)
    suspend.set_defaults(func=command_suspend)

    x11 = sub.add_parser("x11-run", help="start Xorg in a running VM and launch X11 apps through serial")
    x11.add_argument("name")
    x11.add_argument("--display-num", type=int, default=1, help="X11 display number, default :1")
    x11.add_argument("--vt", type=int, default=1, help="virtual terminal for Xorg, default vt1")
    x11.add_argument("--app", action="append", help="X11 app command to launch; may be repeated; default xterm")
    x11.add_argument("--login", default="root", help="serial login username if the VM is at a getty prompt")
    x11.add_argument("--password", default="root", help="serial login password if the VM is at a getty prompt")
    x11.add_argument("--no-install-deps", action="store_true", help="do not install/check Alpine X11 packages first")
    x11.add_argument("--no-start-server", action="store_true", help="only launch apps against an existing DISPLAY")
    x11.add_argument("--no-default-terminal", action="store_true", help="do not add the default focused xterm when --app is used")
    x11.add_argument(
        "--desktop",
        default="x11",
        help="desktop/session profile to install and launch: x11 or xfce; default x11",
    )
    x11.add_argument(
        "--window-manager",
        default="twm",
        help="window manager command to start before apps; default twm places and focuses X clients",
    )
    x11.add_argument("--timeout", type=float, default=60.0)
    x11.set_defaults(func=command_x11_run)

    stop = sub.add_parser("stop", help="stop a background VM")
    stop.add_argument("name")
    stop.add_argument("--force", action="store_true")
    stop.set_defaults(func=command_stop)

    sn = sub.add_parser("snapshot", help="copy the VM rootfs image to a named snapshot")
    sn.add_argument("name")
    sn.add_argument("snapshot", nargs="?")
    sn.add_argument("--description", default="")
    sn.set_defaults(func=command_snapshot)

    sl = sub.add_parser("snapshot-list", help="list VM snapshots")
    sl.add_argument("name")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=command_snapshot_list)

    rs = sub.add_parser("restore", help="replace VM rootfs with a snapshot")
    rs.add_argument("name")
    rs.add_argument("snapshot")
    rs.set_defaults(func=command_restore)

    sd = sub.add_parser("snapshot-delete", help="delete a VM snapshot")
    sd.add_argument("name")
    sd.add_argument("snapshot")
    sd.set_defaults(func=command_snapshot_delete)

    push = sub.add_parser("guest-push", help="copy a host file into a running guest through serial")
    push.add_argument("name")
    push.add_argument("source")
    push.add_argument("destination")
    push.add_argument("--login", default="root")
    push.add_argument("--password", default="root")
    push.add_argument("--timeout", type=float, default=300.0)
    push.add_argument("--max-size-mb", type=int, default=32)
    push.set_defaults(func=command_guest_push)

    pull = sub.add_parser("guest-pull", help="copy a guest file to the host through serial")
    pull.add_argument("name")
    pull.add_argument("source")
    pull.add_argument("destination")
    pull.add_argument("--login", default="root")
    pull.add_argument("--password", default="root")
    pull.add_argument("--timeout", type=float, default=300.0)
    pull.add_argument("--max-size-mb", type=int, default=32)
    pull.set_defaults(func=command_guest_pull)

    tools = sub.add_parser("install-tools", help="install EdgeOS Workstation Guest Tools through serial")
    tools.add_argument("name")
    tools.add_argument("--login", default="root")
    tools.add_argument("--password", default="root")
    tools.add_argument("--timeout", type=float, default=300.0)
    tools.set_defaults(func=command_install_guest_tools)

    cl = sub.add_parser("clone", help="clone a VM rootfs and boot ISO")
    cl.add_argument("source")
    cl.add_argument("name")
    cl.add_argument("--linked", action="store_true", help="create QCOW2 overlays backed by the source disks")
    cl.set_defaults(func=command_clone)

    de = sub.add_parser("delete", help="delete a VM")
    de.add_argument("name")
    de.add_argument("--yes", action="store_true")
    de.set_defaults(func=command_delete)

    tap = sub.add_parser("tap", help="create/setup a TAP+NAT interface using tools/net")
    tap.add_argument("ifname", nargs="?", default="tap0")
    tap.add_argument("--uplink")
    tap.set_defaults(func=command_tap)

    macvtap = sub.add_parser("macvtap", help="create/setup a macvtap external-network interface")
    macvtap.add_argument("ifname", nargs="?", default="edge-macvtap0")
    macvtap.add_argument("--parent", help="physical parent interface; default is the host default route interface")
    macvtap.add_argument("--mode", default="bridge", help="macvtap mode: bridge, private, vepa, or passthru")
    macvtap.set_defaults(func=command_macvtap)

    t = sub.add_parser("template-list", help="list built-in and saved templates")
    t.set_defaults(func=command_template_list)

    ts = sub.add_parser("template-save", help="save a VM config as a reusable template")
    ts.add_argument("vm")
    ts.add_argument("name")
    ts.set_defaults(func=command_template_save)
    return p


def main() -> int:
    try:
        args = parser().parse_args()
        args.func(args)
        return 0
    except (VmError, subprocess.CalledProcessError, OSError) as exc:
        print(f"edgeos-vm: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
