#!/usr/bin/env python3
"""Regression tests for EdgeOS Workstation infrastructure."""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edgeos_vm import (
    TEMPLATE_DEFAULTS,
    VmError,
    apply_templates,
    build_jobs_argument,
    display_boot_command_line,
    edgeos_xfce_persistent_files,
    edgeos_xorg_config_lines,
    ensure_rootfs_capacity,
    generated_rootfs_needs_login_rewrite,
    normalize_display_resolution,
    normalize_display_refresh_hz,
    qemu_disk_args,
    qemu_shared_folder_args,
    qemu_usb_args,
    resolve_build_jobs,
    run_make,
    x11_application_alive_command,
)
from qmp_client import QmpClient, wait_for_qmp
from task_journal import TaskJournal
from vnc_share import VncShareServer
from vm_schema import (
    CURRENT_CONFIG_VERSION,
    ConfigError,
    MAX_BUILD_JOBS,
    effective_gpu,
    migrate_config,
    supports_boot_display_resolution,
)


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    import edgeos_vm_gui as workstation_gui
    import rfb_widget
except (ImportError, SystemExit):
    workstation_gui = None
    rfb_widget = None


class SchemaTests(unittest.TestCase):
    def test_debian_builder_preserves_shadow_file_metadata(self) -> None:
        self.assertFalse(generated_rootfs_needs_login_rewrite("debian"))
        self.assertTrue(generated_rootfs_needs_login_rewrite("alpine"))

    def test_debian_template_selects_systemd_rootfs_capacity(self) -> None:
        config = apply_templates(["edgeos", "debian"])
        self.assertEqual(config["profile"], "debian")
        self.assertEqual(config["memory"], "4096M")
        self.assertGreaterEqual(config["rootfs_size_mb"], 8192)
        self.assertIn("nomodeset", config["boot_params"])
        self.assertIn("quiet", config["boot_params"])
        self.assertIn("splash", config["boot_params"])
        self.assertIn("plymouth.ignore-serial-consoles", config["boot_params"])
        self.assertIn("console=tty0", config["boot_params"])
        self.assertIn("debian", TEMPLATE_DEFAULTS)

    def test_arm64_template_fits_uefi_fat_transport(self) -> None:
        config = apply_templates(["edgeos", "debian", "arm64"])
        self.assertGreaterEqual(config["rootfs_size_mb"], 8192)
        self.assertGreaterEqual(int(config["memory"].rstrip("M")), 8192)
        self.assertEqual(config["accelerator"], "hvf")
        self.assertEqual(config["cpu_model"], "host")
        self.assertIn("nomodeset", config["boot_params"])
        self.assertIn("quiet", config["boot_params"])
        self.assertIn("splash", config["boot_params"])
        self.assertIn("console=tty1", config["boot_params"])

    def test_migrates_legacy_configuration(self) -> None:
        config, changed = migrate_config(
            {
                "architecture": "x86_64",
                "cpus": 2,
                "memory": "2048M",
                "networks": [{"type": "user"}],
                "storage": [],
            }
        )
        self.assertTrue(changed)
        self.assertEqual(config["config_version"], CURRENT_CONFIG_VERSION)
        self.assertEqual(config["cpu_cores"], 2)
        self.assertEqual(config["shared_folders"], [])
        self.assertEqual(config["display_resolution"], "800x600")
        self.assertEqual(config["display_refresh_hz"], 60)
        self.assertEqual(config["build_jobs"], 0)

    def test_rejects_invalid_usb_identifier(self) -> None:
        with self.assertRaises(ConfigError):
            migrate_config(
                {
                    "config_version": CURRENT_CONFIG_VERSION,
                    "architecture": "x86_64",
                    "cpus": 1,
                    "memory": "1G",
                    "networks": [],
                    "storage": [],
                    "usb_devices": [{"vendor_id": "bad", "product_id": "0001"}],
                    "shared_folders": [],
                }
            )

    def test_rejects_invalid_display_resolution(self) -> None:
        with self.assertRaisesRegex(ConfigError, "display width"):
            migrate_config(
                {
                    "config_version": CURRENT_CONFIG_VERSION,
                    "architecture": "arm64",
                    "cpus": 2,
                    "memory": "2G",
                    "networks": [],
                    "storage": [],
                    "usb_devices": [],
                    "shared_folders": [],
                    "display_resolution": "8000x5000",
                }
            )

    def test_normalizes_supported_display_resolution(self) -> None:
        self.assertEqual(normalize_display_resolution("1024×768"), "1024x768")
        self.assertEqual(normalize_display_resolution("2560x1440"), "2560x1440")
        self.assertEqual(normalize_display_resolution("3440X1440"), "3440x1440")
        with self.assertRaisesRegex(RuntimeError, "display width"):
            normalize_display_resolution("8000x4320")

    def test_accepts_high_display_refresh_rate(self) -> None:
        self.assertEqual(normalize_display_refresh_hz(144), 144)
        self.assertEqual(normalize_display_refresh_hz(1000), 1000)
        with self.assertRaisesRegex(RuntimeError, "display refresh"):
            normalize_display_refresh_hz(10001)

    def test_display_mode_is_added_to_boot_command_line_once(self) -> None:
        command_line = display_boot_command_line(
            {
                "display_resolution": "7680x4320",
                "display_refresh_hz": 1000,
                "boot_params": [
                    "console=tty0",
                    "edgeos.video=800x600@60",
                ],
            }
        )
        self.assertEqual(command_line.count("edgeos.video="), 1)
        self.assertIn("edgeos.video=7680x4320@1000", command_line)

    def test_arm64_uses_effective_ramfb_for_resolution_support(self) -> None:
        config = {
            "architecture": "arm64",
            "gpu": "virtio-gpu-gl-pci",
        }
        self.assertEqual(effective_gpu(config), "ramfb")
        self.assertTrue(supports_boot_display_resolution(config))
        self.assertFalse(
            supports_boot_display_resolution(
                {"architecture": "x86_64", "gpu": "ramfb"}
            )
        )

    def test_build_job_selection_is_bounded_and_resolves_auto(self) -> None:
        with mock.patch("edgeos_vm.os.cpu_count", return_value=12):
            self.assertEqual(resolve_build_jobs({"build_jobs": 0}), 12)
        self.assertEqual(resolve_build_jobs({"build_jobs": 6}), 6)
        self.assertEqual(build_jobs_argument("0"), 0)
        self.assertEqual(build_jobs_argument("24"), 24)
        with self.assertRaises(VmError):
            resolve_build_jobs({"build_jobs": 257})

    def test_make_receives_selected_parallel_job_count(self) -> None:
        with mock.patch("edgeos_vm.platform.system", return_value="Linux"), \
                mock.patch("edgeos_vm.run") as run_command:
            run_make({"build_jobs": 7}, ["OUT=build/out", "kernel"])
        run_command.assert_called_once_with(
            ["make", "-j7", "OUT=build/out", "kernel"]
        )

    def test_macos_prefers_gnu_make_when_available(self) -> None:
        with mock.patch("edgeos_vm.platform.system", return_value="Darwin"), \
                mock.patch("edgeos_vm.shutil.which",
                           return_value="/opt/homebrew/bin/gmake"), \
                mock.patch("edgeos_vm.run") as run_command:
            run_make({"build_jobs": 3}, ["kernel"])
        run_command.assert_called_once_with(
            ["/opt/homebrew/bin/gmake", "-j3", "kernel"]
        )


class DeviceArgumentTests(unittest.TestCase):
    def test_managed_devices_generate_qemu_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = {
                "architecture": "x86_64",
                "disk_controller": "virtio-blk",
                "usb": "off",
                "usb_devices": [{"vendor_id": "05ac", "product_id": "12a8"}],
                "shared_folders": [{"tag": "workspace", "path": directory, "read_only": True}],
            }
            disk = qemu_disk_args(
                cfg,
                root / "data.qcow2",
                1,
                {"format": "qcow2", "controller": "virtio-blk", "read_only": True},
            )
            self.assertIn("format=qcow2", " ".join(disk))
            self.assertIn("readonly=on", " ".join(disk))
            self.assertIn("vendorid=0x05ac", " ".join(qemu_usb_args(cfg)))
            self.assertIn("mount_tag=workspace", " ".join(qemu_shared_folder_args(cfg)))

    def test_x86_default_uses_shared_virtio_input_transport(self) -> None:
        config = apply_templates(["edgeos"])
        arguments = qemu_usb_args(config)

        self.assertEqual(config["usb"], "virtio-input")
        self.assertIn(
            "virtio-tablet-pci,disable-modern=off,disable-legacy=on",
            arguments,
        )
        self.assertIn(
            "virtio-keyboard-pci,disable-modern=off,disable-legacy=on",
            arguments,
        )
        self.assertNotIn("qemu-xhci,id=usb0", arguments)

    def test_virtio_input_keeps_controller_for_usb_passthrough(self) -> None:
        config = {
            "usb": "virtio-input",
            "usb_devices": [{"vendor_id": "05ac", "product_id": "12a8"}],
        }
        arguments = qemu_usb_args(config)

        self.assertIn("qemu-xhci,id=usb0", arguments)
        self.assertIn("vendorid=0x05ac", " ".join(arguments))

    def test_shared_xorg_profile_keeps_virtio_tablet_absolute(self) -> None:
        config = "\n".join(edgeos_xorg_config_lines())
        self.assertIn('Option "Device" "/dev/input/event0"', config)
        self.assertIn('Option "Device" "/dev/input/event1"', config)
        self.assertIn('Option "Mode" "Absolute"', config)
        self.assertIn('Driver "fbdev"', config)
        self.assertNotIn('Disable "glx"', config)
        self.assertEqual(config.count('Section "ServerLayout"'), 1)

    def test_xfce_defaults_select_thunar_and_black_background(self) -> None:
        files = edgeos_xfce_persistent_files()
        helpers = "\n".join(files["/etc/xdg/xfce4/helpers.rc"])
        mimeapps = "\n".join(files["/etc/xdg/mimeapps.list"])
        chromium = "\n".join(files["/etc/chromium.d/edgeos-renderer"])
        desktop = "\n".join(files[
            "/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml"
        ])
        self.assertIn("FileManager=Thunar", helpers)
        self.assertIn("inode/directory=thunar.desktop;", mimeapps)
        self.assertIn("--use-gl=angle", chromium)
        self.assertIn("--use-angle=swiftshader", chromium)
        self.assertIn('<property name="image-style" type="int" value="0"/>', desktop)
        self.assertGreaterEqual(desktop.count('<value type="double" value="0.0"/>'), 6)
        self.assertEqual(
            files["/root/.config/xfce4/helpers.rc"],
            files["/etc/xdg/xfce4/helpers.rc"],
        )

    def test_x11_application_probe_preserves_serial_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pidfile = root / "app.pid"
            statusfile = root / "app.status"
            command = x11_application_alive_command(str(pidfile), str(statusfile))
            self.assertNotIn("exit", command)

            pidfile.write_text(f"{os.getpid()}\n", encoding="ascii")
            self.assertEqual(subprocess.run(["sh", "-c", command]).returncode, 0)

            pidfile.write_text("99999999\n", encoding="ascii")
            statusfile.write_text("0\n", encoding="ascii")
            self.assertEqual(subprocess.run(["sh", "-c", command]).returncode, 0)

            statusfile.write_text("1\n", encoding="ascii")
            self.assertNotEqual(subprocess.run(["sh", "-c", command]).returncode, 0)


class RootfsCapacityTests(unittest.TestCase):
    def test_raw_ext_rootfs_is_grown_to_configured_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "rootfs.img"
            with image.open("wb") as rootfs:
                rootfs.truncate(4 * 1024 * 1024)
            cfg = {
                "_dir": directory,
                "rootfs_path": "rootfs.img",
                "rootfs_format": "raw",
                "rootfs_size_mb": 8,
            }
            with mock.patch("edgeos_vm.need_tool", side_effect=lambda name: name), \
                 mock.patch("edgeos_vm.ext_filesystem_size_bytes", return_value=4 * 1024 * 1024), \
                 mock.patch("edgeos_vm.run") as run_command:
                run_command.return_value.returncode = 0
                self.assertTrue(ensure_rootfs_capacity(cfg))
            self.assertEqual(image.stat().st_size, 8 * 1024 * 1024)
            self.assertEqual(
                [call.args[0][0] for call in run_command.call_args_list],
                ["e2fsck", "resize2fs", "e2fsck"],
            )

    def test_non_raw_rootfs_growth_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "rootfs.qcow2"
            image.write_bytes(b"small")
            cfg = {
                "_dir": directory,
                "rootfs_path": image.name,
                "rootfs_format": "qcow2",
                "rootfs_size_mb": 8,
            }
            with self.assertRaisesRegex(RuntimeError, "requires a raw rootfs"):
                ensure_rootfs_capacity(cfg)


class JournalTests(unittest.TestCase):
    def test_task_lifecycle_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            journal = TaskJournal(path)
            task = journal.create("Test", ["true"], "vm")
            journal.update(task["id"], state="running", pid=99999999)
            recovered = TaskJournal(path).recover_interrupted()
            self.assertEqual(recovered[0]["state"], "interrupted")
            self.assertEqual(TaskJournal(path).clear_finished(), 1)

    def test_output_can_be_batched_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            journal = TaskJournal(path)
            task = journal.create("Build", ["make"], "vm")
            journal.append_output(task["id"], "first\n", persist=False)
            journal.append_output(task["id"], "second\n", persist=False)

            self.assertEqual(TaskJournal(path).get(task["id"])["output"], "")
            journal.flush()
            self.assertEqual(
                TaskJournal(path).get(task["id"])["output"],
                "first\nsecond\n",
            )


class VncShareTests(unittest.TestCase):
    def test_external_listener_forwards_bidirectional_vnc_bytes(self) -> None:
        target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        target_port = target.getsockname()[1]
        target_errors: list[BaseException] = []

        def echo_server() -> None:
            try:
                connection, _address = target.accept()
                with connection:
                    payload = connection.recv(4096)
                    connection.sendall(b"target:" + payload)
            except BaseException as exc:
                target_errors.append(exc)
            finally:
                target.close()

        target_thread = threading.Thread(target=echo_server, daemon=True)
        target_thread.start()
        share = VncShareServer("127.0.0.1", target_port, 0)
        share.start()
        try:
            with socket.create_connection(("127.0.0.1", share.listen_port), timeout=2.0) as client:
                client.sendall(b"RFB 003.008\n")
                self.assertEqual(client.recv(4096), b"target:RFB 003.008\n")
        finally:
            share.stop()
            target_thread.join(timeout=2.0)
        self.assertEqual(target_errors, [])
        self.assertFalse(share.is_running)


@unittest.skipIf(workstation_gui is None, "Qt bindings unavailable")
class GuiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = workstation_gui.QApplication.instance()
        if cls.app is None:
            cls.app = workstation_gui.QApplication([])

    def test_host_terminal_is_not_an_implicit_serial_console(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_instances = workstation_gui.INSTANCES_DIR
            original_journal = workstation_gui.TASK_JOURNAL_PATH
            workstation_gui.INSTANCES_DIR = Path(directory) / "instances"
            workstation_gui.TASK_JOURNAL_PATH = Path(directory) / "tasks.json"
            window = workstation_gui.MainWindow()
            try:
                self.assertFalse(hasattr(window, "stdin_notifier"))
                terminal_stdout = mock.Mock()
                terminal_stdout.isatty.return_value = True
                with (
                    mock.patch.object(
                        workstation_gui.sys, "stdout", terminal_stdout
                    ),
                    mock.patch("builtins.print") as print_output,
                ):
                    window.append_output("\x1b[6n")
                print_output.assert_not_called()

                window.serial_fd = 123
                window.serial_input.setText("root")
                with mock.patch.object(
                    window, "write_serial_bytes", return_value=True
                ) as write_serial:
                    window.send_serial_input()
                write_serial.assert_called_once_with(b"root\n")
            finally:
                window.serial_fd = None
                window.close()
                self.app.processEvents()
                workstation_gui.INSTANCES_DIR = original_instances
                workstation_gui.TASK_JOURNAL_PATH = original_journal

    def test_build_output_is_coalesced_before_ui_and_disk_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_instances = workstation_gui.INSTANCES_DIR
            original_journal = workstation_gui.TASK_JOURNAL_PATH
            workstation_gui.INSTANCES_DIR = Path(directory) / "instances"
            workstation_gui.TASK_JOURNAL_PATH = Path(directory) / "tasks.json"
            window = workstation_gui.MainWindow()
            try:
                task = window.task_journal.create("Build", ["make"], "vm")
                window.active_task_id = str(task["id"])
                process = mock.Mock()
                process.readAllStandardOutput.return_value = b"compiler output\n"
                window.process = process
                with mock.patch.object(window, "append_output") as append_output:
                    for _ in range(50):
                        window.read_task_output()
                    append_output.assert_not_called()
                    self.assertEqual(
                        TaskJournal(workstation_gui.TASK_JOURNAL_PATH)
                        .get(task["id"])["output"],
                        "",
                    )
                    window.task_output_timer.stop()
                    window.flush_task_output()
                    append_output.assert_called_once()
                captured = TaskJournal(workstation_gui.TASK_JOURNAL_PATH).get(
                    task["id"]
                )["output"]
                self.assertEqual(captured.count("compiler output"), 50)
            finally:
                window.process = None
                window.active_task_id = None
                window.close()
                self.app.processEvents()
                workstation_gui.INSTANCES_DIR = original_instances
                workstation_gui.TASK_JOURNAL_PATH = original_journal

    def test_ansi_terminal_plain_text_batching_preserves_overwrite(self) -> None:
        terminal = workstation_gui.AnsiTerminal()
        terminal.write("abc\rxy\n\x1b[31mred\x1b[0m\n")
        self.assertEqual(terminal.toPlainText(), "xyc\nred\n")
        terminal.close()

    def test_compact_display_controls_and_left_aligned_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_instances = workstation_gui.INSTANCES_DIR
            original_journal = workstation_gui.TASK_JOURNAL_PATH
            workstation_gui.INSTANCES_DIR = Path(directory) / "instances"
            workstation_gui.TASK_JOURNAL_PATH = Path(directory) / "tasks.json"
            try:
                window = workstation_gui.MainWindow()
                window.dark_mode = True
                window._apply_style()
                window.resize(1100, 760)
                for name in ("layout-one", "layout-two", "layout-three"):
                    window.ensure_vm_tab(name)
                window.show()
                self.app.processEvents()

                buttons = window.findChildren(
                    workstation_gui.QToolButton, "DisplayToolButton"
                )
                self.assertEqual(len(buttons), 8)
                self.assertEqual(
                    window.display_resolution.itemData(
                        window.display_resolution.count() - 1
                    ),
                    "custom",
                )
                self.assertGreaterEqual(window.display_resolution.count(), 12)
                self.assertTrue(
                    all(
                        button.width() == 34
                        and button.height() == 32
                        and button.toolTip()
                        for button in buttons
                    )
                )
                self.assertEqual(window.vm_tabs.geometry().x(), 0)
                self.assertEqual(
                    window.vm_tabs.width(), window.vm_tabs_container.width()
                )
                self.assertEqual(window.vm_tabs.tabRect(0).x(), 0)

                tabs_image = window.vm_tabs.grab().toImage()
                inactive_rect = window.vm_tabs.tabRect(0)
                selected_rect = window.vm_tabs.tabRect(2)
                inactive_background = tabs_image.pixelColor(
                    inactive_rect.right() - 8, inactive_rect.top() + 8
                )
                selected_background = tabs_image.pixelColor(
                    selected_rect.right() - 8, selected_rect.top() + 8
                )
                self.assertLess(inactive_background.lightness(), 100)
                self.assertLess(selected_background.lightness(), 100)
                self.assertNotEqual(inactive_background, selected_background)

                settings_button = window.main_toolbar.widgetForAction(
                    window.settings_action
                )
                settings_button.setDown(True)
                self.app.processEvents()
                pressed_image = settings_button.grab().toImage()
                pressed_background = pressed_image.pixelColor(4, 4)
                self.assertLess(pressed_background.lightness(), 160)
            finally:
                if "window" in locals():
                    window.close()
                    self.app.processEvents()
                workstation_gui.INSTANCES_DIR = original_instances
                workstation_gui.TASK_JOURNAL_PATH = original_journal

    def test_arm64_effective_ramfb_enables_and_preserves_resolution(self) -> None:
        config = {
            "architecture": "arm64",
            "gpu": "virtio-gpu-gl-pci",
            "display_resolution": "800x600",
            "profile": "debian",
            "networks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            original_instances = workstation_gui.INSTANCES_DIR
            original_journal = workstation_gui.TASK_JOURNAL_PATH
            workstation_gui.INSTANCES_DIR = Path(directory) / "instances"
            workstation_gui.TASK_JOURNAL_PATH = Path(directory) / "tasks.json"
            window = workstation_gui.MainWindow()
            try:
                with (
                    mock.patch.object(
                        window,
                        "selected_name",
                        return_value="arm-test",
                    ),
                    mock.patch.object(
                        window,
                        "config_for_name",
                        return_value=config,
                    ),
                    mock.patch.object(
                        window,
                        "vm_runtime_status",
                        return_value={"running": False, "status": "shutdown"},
                    ),
                    mock.patch.object(window, "ensure_vm_tab"),
                    mock.patch.object(window, "attach_selected_vm"),
                ):
                    window.update_actions()
                    self.assertTrue(window.display_resolution.isEnabled())
                    self.assertTrue(window.apply_resolution_btn.isEnabled())
                    self.assertEqual(window.detail_labels["gpu"].text(), "ramfb")

                    selected = window.display_resolution.findData("1920x1080")
                    window.display_resolution.setCurrentIndex(selected)
                    window.update_vm_summary("arm-test", config)
                    self.assertEqual(
                        window.display_resolution.currentData(),
                        "1920x1080",
                    )
            finally:
                window.close()
                self.app.processEvents()
                workstation_gui.INSTANCES_DIR = original_instances
                workstation_gui.TASK_JOURNAL_PATH = original_journal

    def test_dark_wizard_step_rail(self) -> None:
        parent = workstation_gui.QWidget()
        parent.setStyleSheet(
            "QListWidget#WizardSteps { background: #20272d; color: #cbd5dc; }"
        )
        wizard = workstation_gui.CreateVmDialog(parent)
        wizard.show()
        self.app.processEvents()
        try:
            image = wizard.steps.viewport().grab().toImage()
            background = image.pixelColor(
                wizard.steps.viewport().width() - 8,
                wizard.steps.viewport().height() - 8,
            )
            self.assertLess(background.lightness(), 100)
            self.assertGreaterEqual(wizard.steps.width(), 285)
            self.assertEqual(wizard.build_jobs.minimum(), 0)
            self.assertEqual(wizard.build_jobs.maximum(), MAX_BUILD_JOBS)
            self.assertIn("Automatic", wizard.build_jobs.text())
        finally:
            wizard.close()
            parent.close()

    def test_rfb_recovers_from_blank_white_boot_frame(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3.0)
        port = listener.getsockname()[1]
        server_errors: list[BaseException] = []
        requests: list[bytes] = []

        def receive_exact(connection: socket.socket, length: int) -> bytes:
            data = bytearray()
            while len(data) < length:
                chunk = connection.recv(length - len(data))
                if not chunk:
                    raise ConnectionError("RFB test client disconnected")
                data.extend(chunk)
            return bytes(data)

        def send_frame(connection: socket.socket, pixel: bytes) -> None:
            connection.sendall(struct.pack(">BBH", 0, 0, 1))
            connection.sendall(struct.pack(">HHHHi", 0, 0, 4, 4, 0))
            connection.sendall(pixel * 16)

        def serve() -> None:
            try:
                connection, _address = listener.accept()
                with connection:
                    connection.settimeout(3.0)
                    connection.sendall(b"RFB 003.008\n")
                    receive_exact(connection, 12)
                    connection.sendall(b"\x01\x01")
                    receive_exact(connection, 1)
                    connection.sendall(struct.pack(">I", 0))
                    receive_exact(connection, 1)
                    server_format = struct.pack(
                        ">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0
                    )
                    name = b"Boot recovery test"
                    connection.sendall(
                        struct.pack(">HH", 4, 4)
                        + server_format
                        + struct.pack(">I", len(name))
                        + name
                    )
                    receive_exact(connection, 20)
                    encoding_header = receive_exact(connection, 4)
                    encoding_count = struct.unpack(">H", encoding_header[2:])[0]
                    receive_exact(connection, encoding_count * 4)

                    first_request = receive_exact(connection, 10)
                    requests.append(first_request)
                    send_frame(connection, b"\xff\xff\xff\x00")
                    recovery_request = receive_exact(connection, 10)
                    requests.append(recovery_request)
                    send_frame(connection, b"\x00\x00\x00\x00")
                    time.sleep(0.1)
            except BaseException as exc:
                server_errors.append(exc)
            finally:
                listener.close()

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        client = rfb_widget.RfbClient("127.0.0.1", port)
        frames = []
        client.frame_ready.connect(lambda image: frames.append(image.copy()))
        client.start()
        deadline = time.monotonic() + 4.0
        while len(frames) < 2 and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        client.stop()
        client.wait(1500)
        server.join(timeout=1.0)

        self.assertEqual(server_errors, [])
        self.assertGreaterEqual(len(frames), 2)
        self.assertEqual(requests[0][0:2], b"\x03\x00")
        self.assertEqual(requests[1][0:2], b"\x03\x00")
        self.assertEqual(frames[0].pixelColor(2, 2).name(), "#ffffff")
        self.assertEqual(frames[1].pixelColor(2, 2).name(), "#000000")

    def test_qmp_frame_overrides_rfb_frame_until_cleared(self) -> None:
        view = rfb_widget.RfbView()
        rfb_frame = workstation_gui.QImage(8, 6, workstation_gui.QImage.Format.Format_RGB32)
        rfb_frame.fill(workstation_gui.Qt.GlobalColor.black)
        qmp_frame = workstation_gui.QImage(8, 6, workstation_gui.QImage.Format.Format_RGB32)
        qmp_frame.fill(workstation_gui.Qt.GlobalColor.red)
        view._image = rfb_frame

        view.set_authoritative_frame(qmp_frame)
        self.assertEqual(view._visible_image().pixelColor(3, 2).name(), "#ff0000")
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "display.png"
            self.assertTrue(view.save_screenshot(str(screenshot)))
            saved = workstation_gui.QImage(str(screenshot))
            self.assertEqual(saved.pixelColor(3, 2).name(), "#ff0000")

        view.clear_authoritative_frame()
        self.assertEqual(view._visible_image().pixelColor(3, 2).name(), "#000000")

    def test_blank_rfb_frames_are_not_treated_as_desktop_frames(self) -> None:
        blank = workstation_gui.QImage(800, 600, workstation_gui.QImage.Format.Format_RGB32)
        blank.fill(workstation_gui.Qt.GlobalColor.white)
        self.assertFalse(rfb_widget.RfbView.frame_has_visual_detail(blank))

        detailed = workstation_gui.QImage(32, 24, workstation_gui.QImage.Format.Format_RGB32)
        for y in range(detailed.height()):
            for x in range(detailed.width()):
                red = (x * 17) % 256
                green = (y * 29) % 256
                detailed.setPixel(x, y, 0xFF000000 | (red << 16) | (green << 8) | 90)
        self.assertTrue(rfb_widget.RfbView.frame_has_visual_detail(detailed))

    def test_retina_scaling_prepares_exact_unfiltered_device_pixels(self) -> None:
        source = workstation_gui.QImage(2, 1, workstation_gui.QImage.Format.Format_RGB32)
        source.setPixel(0, 0, 0xFF000000)
        source.setPixel(1, 0, 0xFFFFFFFF)
        rendered = rfb_widget.RfbView.scale_for_device_pixels(source, 2, 1, 2.0)
        self.assertEqual((rendered.width(), rendered.height()), (4, 2))
        self.assertEqual(rendered.devicePixelRatio(), 2.0)
        self.assertEqual(rendered.pixelColor(0, 0).name(), "#000000")
        self.assertEqual(rendered.pixelColor(1, 0).name(), "#000000")
        self.assertEqual(rendered.pixelColor(2, 0).name(), "#ffffff")
        self.assertEqual(rendered.pixelColor(3, 0).name(), "#ffffff")

    def test_fit_scaling_uses_smooth_filter_only_when_needed(self) -> None:
        source = workstation_gui.QImage(8, 6, workstation_gui.QImage.Format.Format_RGB32)
        self.assertEqual(
            rfb_widget.RfbView.transformation_mode_for_target(source, 4, 3, "fit"),
            workstation_gui.Qt.TransformationMode.SmoothTransformation,
        )
        self.assertEqual(
            rfb_widget.RfbView.transformation_mode_for_target(source, 16, 12, "fit"),
            workstation_gui.Qt.TransformationMode.FastTransformation,
        )
        self.assertEqual(
            rfb_widget.RfbView.transformation_mode_for_target(source, 13, 10, "fit"),
            workstation_gui.Qt.TransformationMode.SmoothTransformation,
        )

    def test_integer_fit_uses_exact_scale_steps(self) -> None:
        source = workstation_gui.QSize(1920, 1080)
        compact = rfb_widget.RfbView.integer_fit_size(
            source,
            workstation_gui.QSize(700, 500),
        )
        expanded = rfb_widget.RfbView.integer_fit_size(
            workstation_gui.QSize(320, 200),
            workstation_gui.QSize(1000, 700),
        )
        self.assertEqual((compact.width(), compact.height()), (640, 360))
        self.assertEqual((expanded.width(), expanded.height()), (960, 600))

    def test_clear_fit_never_enlarges_guest_pixels(self) -> None:
        native = rfb_widget.RfbView.clear_fit_size(
            workstation_gui.QSize(800, 600),
            workstation_gui.QSize(1600, 1000),
        )
        reduced = rfb_widget.RfbView.clear_fit_size(
            workstation_gui.QSize(1920, 1080),
            workstation_gui.QSize(700, 500),
        )
        self.assertEqual((native.width(), native.height()), (800, 600))
        self.assertEqual((reduced.width(), reduced.height()), (700, 394))


@unittest.skipUnless(subprocess.run(["sh", "-c", "command -v qemu-system-x86_64"], check=False).returncode == 0, "QEMU unavailable")
class QmpIntegrationTests(unittest.TestCase):
    def test_real_qmp_status_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command_socket = root / "qmp.sock"
            event_socket = root / "events.sock"
            process = subprocess.Popen(
                [
                    "qemu-system-x86_64",
                    "-M", "none",
                    "-nodefaults",
                    "-display", "none",
                    "-S",
                    "-qmp", f"unix:{command_socket},server=on,wait=off",
                    "-qmp", f"unix:{event_socket},server=on,wait=off",
                ]
            )
            try:
                status = wait_for_qmp(command_socket)
                self.assertEqual(status["status"], "prelaunch")
                with QmpClient(event_socket) as events, QmpClient(command_socket) as commands:
                    commands.execute("cont")
                    event = events.next_event(timeout=3.0)
                    self.assertIsNotNone(event)
                    self.assertEqual(event["event"], "RESUME")
                    commands.execute("quit")
                process.wait(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
