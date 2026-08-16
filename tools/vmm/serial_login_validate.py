#!/usr/bin/env python3
"""Boot one EdgeOS VM copy and validate login through QEMU's serial PTY.

This is a developer validation helper. It uses a temporary qcow2 overlay so the
VM's persistent rootfs is not changed during the check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import termios
import threading
import time
import tty
from pathlib import Path

from paths import STATE_ROOT, WORKSTATION_ROOT


REPO_ROOT = WORKSTATION_ROOT
INSTANCES_DIR = STATE_ROOT / "instances"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wait_for(path: Path, needle: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in read_text(path):
            return True
        time.sleep(0.2)
    return needle in read_text(path)


def write_interactive(fd: int, data: bytes, delay: float) -> None:
    def write_all(chunk: bytes) -> None:
        view = memoryview(chunk)
        while view:
            try:
                n = os.write(fd, view)
                if n > 0:
                    view = view[n:]
                    continue
            except BlockingIOError:
                pass
            time.sleep(0.01)
        termios.tcdrain(fd)

    if delay <= 0:
        print(f"WRITE={data!r}")
        write_all(data)
        return
    print(f"WRITE={data!r}")
    for byte in data:
        write_all(bytes([byte]))
        time.sleep(delay)


def drain_pty(fd: int, stop: threading.Event, chunks: list[bytes]) -> None:
    while not stop.is_set():
        try:
            data = os.read(fd, 4096)
            if data:
                chunks.append(data)
        except BlockingIOError:
            time.sleep(0.02)
        except OSError:
            return


def combined_output(path: Path, chunks: list[bytes]) -> str:
    data = read_text(path)
    if chunks:
        data += b"".join(chunks).decode(errors="replace")
    return data


def wait_for_combined(path: Path, chunks: list[bytes], needle: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in combined_output(path, chunks):
            return True
        time.sleep(0.2)
    return needle in combined_output(path, chunks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--no-kvm", action="store_true")
    parser.add_argument("--eol", choices=["cr", "lf", "crlf"], default="cr")
    parser.add_argument("--char-delay", type=float, default=0.1)
    parser.add_argument("--prompt-delay", type=float, default=2.0)
    parser.add_argument("--raw-copy", action="store_true")
    parser.add_argument("--stdio", action="store_true", help="use QEMU stdio serial instead of a PTY")
    parser.add_argument("--vt-keyboard", action="store_true", help="send credentials through the QEMU keyboard monitor")
    parser.add_argument("--keep-work", action="store_true", help="leave the temporary boot directory in place for inspection")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    inst = INSTANCES_DIR / args.name
    cfg_path = inst / "vm.json"
    iso = inst / "build" / "out" / "edgeos-vm.iso"
    rootfs = inst / "rootfs.img"
    if not iso.is_file():
        raise SystemExit(f"missing ISO: {iso}")
    if not rootfs.is_file():
        raise SystemExit(f"missing rootfs: {rootfs}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}

    work = Path(tempfile.mkdtemp(prefix="edgeos-serial-login-"))
    overlay = work / ("rootfs.img" if args.raw_copy else "rootfs.qcow2")
    serial_log = work / "serial.log"
    qemu_log = work / "qemu.log"
    monitor_sock = work / "monitor.sock"
    if args.raw_copy:
        subprocess.run(["cp", "--sparse=always", str(rootfs), str(overlay)], check=True)
        drive_format = "raw"
    else:
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(rootfs), "-F", "raw", str(overlay)], check=True)
        drive_format = "qcow2"

    qemu = [
        "qemu-system-x86_64",
        "-machine",
        "pc,accel=tcg" if args.no_kvm else "pc,accel=kvm",
        "-cpu",
        "qemu64" if args.no_kvm else "host,migratable=off",
        "-smp",
        "4,sockets=1,cores=4,threads=1",
        "-m",
        str(cfg.get("memory", "2048M")),
        "-display",
        "none",
        "-no-reboot",
        "-no-shutdown",
        "-drive",
        f"file={overlay},format={drive_format},if=none,id=rootdisk,cache=none,aio=native,discard=unmap",
        "-device",
        "nvme,drive=rootdisk,serial=edgeosroot",
        "-cdrom",
        str(iso),
        "-netdev",
        "user,id=net0",
        "-device",
        "e1000,netdev=net0",
        "-device",
        "qemu-xhci,id=usb0",
        "-device",
        "usb-mouse,bus=usb0.0",
        "-boot",
        "d",
    ]
    if args.vt_keyboard:
        qemu.extend(["-monitor", f"unix:{monitor_sock},server,nowait"])
    else:
        qemu.extend(["-monitor", "none"])
    if args.stdio:
        qemu.extend(["-serial", "stdio"])
    else:
        qemu.extend([
            "-chardev",
            f"pty,id=serial0,logfile={serial_log},logappend=on",
            "-serial",
            "chardev:serial0",
        ])
    if not args.no_kvm:
        qemu.insert(1, "-enable-kvm")

    qlog = qemu_log.open("wb")
    if args.stdio:
        proc = subprocess.Popen(qemu, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=REPO_ROOT)
    else:
        proc = subprocess.Popen(qemu, stdout=qlog, stderr=subprocess.STDOUT, cwd=REPO_ROOT)
    fd = -1
    try:
        stop_drain = threading.Event()
        pty_chunks: list[bytes] = []
        if args.stdio:
            if proc.stdout is None or proc.stdin is None:
                return 2

            def drain_stdio() -> None:
                while not stop_drain.is_set():
                    data = proc.stdout.read(1)
                    if data:
                        pty_chunks.append(data)
                        qlog.write(data)
                        qlog.flush()
                    elif proc.poll() is not None:
                        return

            drain_thread = threading.Thread(target=drain_stdio, daemon=True)
            drain_thread.start()
        else:
            pty_path: str | None = None
            deadline = time.time() + 10
            while time.time() < deadline:
                qlog.flush()
                match = re.search(r"char device redirected to (/dev/pts/\d+)", read_text(qemu_log))
                if match:
                    pty_path = match.group(1)
                    break
                time.sleep(0.1)
            print(f"PTY={pty_path}")
            if not pty_path:
                print(read_text(qemu_log))
                return 2

            fd = os.open(pty_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            tty.setraw(fd)
            drain_thread = threading.Thread(target=drain_pty, args=(fd, stop_drain, pty_chunks), daemon=True)
            drain_thread.start()

        eol = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}[args.eol]
        monitor_conn: socket.socket | None = None

        def monitor_open() -> socket.socket:
            nonlocal monitor_conn
            if monitor_conn is not None:
                return monitor_conn
            deadline = time.time() + 10
            last_exc: Exception | None = None
            while time.time() < deadline:
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(str(monitor_sock))
                    s.settimeout(1)
                    try:
                        s.recv(4096)
                    except OSError:
                        pass
                    monitor_conn = s
                    return s
                except OSError as exc:
                    last_exc = exc
                    time.sleep(0.1)
            raise RuntimeError(f"monitor connect failed: {last_exc}")

        def monitor_cmd(cmd: str) -> None:
            s = monitor_open()
            s.sendall((cmd + "\n").encode("ascii"))
            try:
                s.recv(4096)
            except OSError:
                pass

        def write_vt_keys(data: bytes) -> None:
            print(f"VT_KEYS={data!r}")
            keymap = {
                ord("\r"): "ret",
                ord("\n"): "ret",
                ord(" "): "spc",
                ord("-"): "minus",
                ord("_"): "shift-minus",
                ord("/"): "slash",
                ord("."): "dot",
            }
            for byte in data:
                key = keymap.get(byte)
                if key is None:
                    ch = chr(byte)
                    if ch.isalnum():
                        key = ch.lower()
                    else:
                        raise RuntimeError(f"unsupported key byte for monitor sendkey: {byte!r}")
                monitor_cmd(f"sendkey {key}")
                time.sleep(args.char_delay if args.char_delay > 0 else 0.03)

        def write_stdio(data: bytes) -> None:
            assert proc.stdin is not None
            print(f"WRITE={data!r}")
            if args.char_delay <= 0:
                proc.stdin.write(data)
                proc.stdin.flush()
                return
            for byte in data:
                proc.stdin.write(bytes([byte]))
                proc.stdin.flush()
                time.sleep(args.char_delay)

        has_login = wait_for_combined(serial_log, pty_chunks, "login:", args.timeout)
        print(f"HAS_LOGIN={has_login}")
        time.sleep(args.prompt_delay)
        if args.vt_keyboard:
            write_vt_keys(b"root" + eol)
        elif args.stdio:
            write_stdio(b"root" + eol)
        else:
            write_interactive(fd, b"root" + eol, args.char_delay)

        has_password = wait_for_combined(serial_log, pty_chunks, "Password:", 60)
        print(f"HAS_PASSWORD={has_password}")
        time.sleep(args.prompt_delay)
        if args.vt_keyboard:
            write_vt_keys(b"root" + eol)
        elif args.stdio:
            write_stdio(b"root" + eol)
        else:
            write_interactive(fd, b"root" + eol, args.char_delay)

        has_shell = wait_for_combined(serial_log, pty_chunks, ":~#", 60)
        print(f"HAS_SHELL={has_shell}")
        has_id = False
        if has_shell:
            if args.vt_keyboard:
                write_vt_keys(b"id" + eol)
            elif args.stdio:
                write_stdio(b"id" + eol)
            else:
                write_interactive(fd, b"id" + eol, args.char_delay)
            has_id = wait_for_combined(serial_log, pty_chunks, "uid=0(root)", 5)
        print(f"HAS_ID={has_id}")
        if monitor_conn is not None:
            monitor_conn.close()
        print(f"WORKDIR={work}")
        print("---SERIAL TAIL---")
        print(combined_output(serial_log, pty_chunks)[-30000:])
        return 0 if has_login and has_password and has_shell and has_id else 1
    finally:
        if 'stop_drain' in locals():
            stop_drain.set()
        if fd >= 0:
            os.close(fd)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        qlog.close()
        if args.keep_work:
            print(f"kept workdir: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
