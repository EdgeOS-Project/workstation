#!/usr/bin/env python3
"""Qt GUI for the EdgeOS Virtual Machine Manager."""

from __future__ import annotations

import json
import os
import select
import shlex
import shutil
import socket
import subprocess
import sys
import termios
import time
import tty
import webbrowser
from pathlib import Path


if sys.platform == "darwin":
    # Homebrew keeps PyQt keg-only; developer Python installations managed by
    # mise/pyenv do not automatically search the Homebrew site-packages tree.
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for prefix in (Path("/opt/homebrew/opt/pyqt"), Path("/usr/local/opt/pyqt")):
        site_packages = prefix / "lib" / version / "site-packages"
        if site_packages.is_dir() and str(site_packages) not in sys.path:
            sys.path.insert(0, str(site_packages))


try:
    from PyQt6.QtCore import QCoreApplication, QLocale, QSize, QProcess, QSettings, QSocketNotifier, QThread, QTimer, QTranslator, Qt, pyqtSignal as Signal
    from PyQt6.QtGui import QAction, QFont, QImage, QKeySequence, QTextCursor
    from PyQt6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPushButton,
        QProgressBar,
        QProxyStyle,
        QPlainTextEdit,
        QRadioButton,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QStyle,
        QTabBar,
        QTabWidget,
        QToolBar,
        QToolButton,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    try:
        from PySide6.QtCore import QCoreApplication, QLocale, QSize, QProcess, QSettings, QSocketNotifier, QThread, QTimer, QTranslator, Qt, Signal
        from PySide6.QtGui import QAction, QFont, QImage, QKeySequence, QTextCursor
        from PySide6.QtWidgets import (
            QApplication,
            QButtonGroup,
            QCheckBox,
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QFileDialog,
            QFormLayout,
            QFrame,
            QGridLayout,
            QGroupBox,
            QHBoxLayout,
            QHeaderView,
            QInputDialog,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMenu,
            QMessageBox,
            QPushButton,
            QProgressBar,
            QProxyStyle,
            QPlainTextEdit,
            QRadioButton,
            QSpinBox,
            QSplitter,
            QStackedWidget,
            QStyle,
            QTabBar,
            QTabWidget,
            QToolBar,
            QToolButton,
            QTreeWidget,
            QTreeWidgetItem,
            QVBoxLayout,
            QWidget,
        )
    except ImportError:
        print("EdgeOS VM Manager GUI requires PyQt6 or PySide6.")
        print("Install one binding, for example: python3 -m pip install PyQt6")
        raise SystemExit(1)


from paths import STATE_ROOT, WORKSTATION_ROOT


REPO_ROOT = WORKSTATION_ROOT
CLI = REPO_ROOT / "tools/vmm/edgeos_vm.py"
WORKSTATION_SERVER = REPO_ROOT / "tools/vmm/workstation_server.py"
STATE_DIR = STATE_ROOT
INSTANCES_DIR = STATE_DIR / "instances"
TASK_JOURNAL_PATH = STATE_DIR / "workstation-tasks.json"
TRANSLATIONS_DIR = Path(__file__).resolve().with_name("translations")

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from rfb_widget import RfbView
from ansi_terminal import AnsiTerminal
from qmp_client import QmpClient, QmpError
from task_journal import TaskJournal
from vnc_share import (
    VncShareError,
    VncShareServer,
    find_available_share_port,
    local_network_addresses,
)
from vm_schema import (
    ConfigError,
    MAX_BUILD_JOBS,
    SUPPORTED_DISPLAY_RESOLUTIONS,
    effective_gpu,
    load_config,
    normalize_display_resolution,
    save_config,
    supports_boot_display_resolution,
)


class LeftAlignedTabStyle(QProxyStyle):
    """Keep document tabs left aligned on platforms that center them."""

    def styleHint(self, hint, option=None, widget=None, return_data=None):
        if hint == QStyle.StyleHint.SH_TabBar_Alignment:
            return int(Qt.AlignmentFlag.AlignLeft)
        return super().styleHint(hint, option, widget, return_data)


class QmpWatcher(QThread):
    """Maintain one event-driven QMP subscription for a running VM."""

    state_changed = Signal(str, str)
    event_received = Signal(str, str)

    def __init__(self, name: str, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.path = path

    def run(self) -> None:  # type: ignore[override]
        while not self.isInterruptionRequested():
            try:
                with QmpClient(self.path, timeout=1.0) as client:
                    self._emit_status(client)
                    while not self.isInterruptionRequested():
                        event = client.next_event(timeout=0.5)
                        if event is None:
                            continue
                        event_name = str(event.get("event", "UNKNOWN"))
                        self.event_received.emit(self.name, event_name)
                        if event_name in {
                            "STOP",
                            "RESUME",
                            "RESET",
                            "SHUTDOWN",
                            "POWERDOWN",
                            "SUSPEND",
                            "WAKEUP",
                            "GUEST_PANICKED",
                        }:
                            self._emit_status(client)
            except (OSError, QmpError):
                if not self.isInterruptionRequested():
                    self.msleep(300)

    def _emit_status(self, client: QmpClient) -> None:
        try:
            status = str(client.query_status().get("status", "unknown"))
        except (OSError, QmpError):
            status = "disconnected"
        self.state_changed.emit(self.name, status)


class TaskCenterDialog(QDialog):
    """Display persistent VM operations and their captured output."""

    def __init__(self, journal: TaskJournal, cancel_callback, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.journal = journal
        self.cancel_callback = cancel_callback
        self.setWindowTitle("Task Center")
        self.setMinimumSize(780, 480)
        self.resize(900, 560)

        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel("Task Center")
        title.setObjectName("WizardTitle")
        heading.addWidget(title)
        heading.addStretch(1)
        self.cancel_button = QPushButton("Cancel Task")
        self.cancel_button.clicked.connect(self._cancel_selected)
        heading.addWidget(self.cancel_button)
        clear_button = QPushButton("Clear Finished")
        clear_button.clicked.connect(self._clear_finished)
        heading.addWidget(clear_button)
        layout.addLayout(heading)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.task_list = QTreeWidget()
        self.task_list.setHeaderLabels(["TASK", "VIRTUAL MACHINE", "STATE", "STARTED"])
        self.task_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.task_list.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.task_list.itemSelectionChanged.connect(self._selection_changed)
        splitter.addWidget(self.task_list)

        self.output = QPlainTextEdit()
        self.output.setObjectName("Console")
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(12000)
        console_font = QFont("Menlo")
        console_font.setStyleHint(QFont.StyleHint.Monospace)
        console_font.setPointSize(10)
        self.output.setFont(console_font)
        splitter.addWidget(self.output)
        splitter.setSizes([260, 220])
        layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self) -> None:
        selected = self.selected_task_id()
        self.task_list.clear()
        selected_item: QTreeWidgetItem | None = None
        for task in reversed(self.journal.tasks):
            started = task.get("started_at") or task.get("created_at")
            started_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if started else "—"
            item = QTreeWidgetItem(
                [
                    str(task.get("title", "Task")),
                    str(task.get("vm_name") or "—"),
                    str(task.get("state", "unknown")).replace("_", " ").title(),
                    started_text,
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, task.get("id"))
            self.task_list.addTopLevelItem(item)
            if task.get("id") == selected:
                selected_item = item
        if selected_item is not None:
            self.task_list.setCurrentItem(selected_item)
        elif self.task_list.topLevelItemCount():
            self.task_list.setCurrentItem(self.task_list.topLevelItem(0))
        else:
            self.output.clear()
            self.cancel_button.setEnabled(False)

    def selected_task_id(self) -> str | None:
        selected = self.task_list.selectedItems()
        if not selected:
            return None
        value = selected[0].data(0, Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _selection_changed(self) -> None:
        task_id = self.selected_task_id()
        task = self.journal.get(task_id) if task_id else None
        if task is None:
            self.output.clear()
            self.cancel_button.setEnabled(False)
            return
        command = " ".join(shlex.quote(str(part)) for part in task.get("command", []))
        captured = str(task.get("output", ""))
        self.output.setPlainText(f"$ {command}\n\n{captured}".rstrip())
        self.output.moveCursor(QTextCursor.MoveOperation.End)
        self.cancel_button.setEnabled(task.get("state") in {"queued", "running", "cancelling"})

    def append_task_output(self, task_id: str, text: str) -> None:
        """Append live output without rebuilding the task list or full document."""
        if not self.isVisible() or self.selected_task_id() != task_id or not text:
            return
        self.output.moveCursor(QTextCursor.MoveOperation.End)
        self.output.insertPlainText(text)
        self.output.moveCursor(QTextCursor.MoveOperation.End)

    def _cancel_selected(self) -> None:
        task_id = self.selected_task_id()
        if task_id:
            self.cancel_callback(task_id)

    def _clear_finished(self) -> None:
        self.journal.clear_finished()
        self.refresh()


class StorageDeviceDialog(QDialog):
    """Edit one additional virtual disk attachment."""

    def __init__(self, device: dict[str, object] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        value = device or {}
        self.setWindowTitle("Virtual Disk")
        self.setMinimumWidth(620)
        self.name = QLineEdit(str(value.get("name", "Hard Disk")))
        self.path = QLineEdit(str(value.get("path", "")))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.path, 1)
        path_layout.addWidget(browse)
        self.format = QComboBox()
        self.format.addItems(["qcow2", "raw"])
        self.format.setCurrentText(str(value.get("format", "qcow2")))
        self.controller = QComboBox()
        self.controller.addItems(["virtio-blk", "nvme", "ide"])
        self.controller.setCurrentText(str(value.get("controller", "virtio-blk")))
        self.cache = QComboBox()
        self.cache.addItems(["none", "writeback", "writethrough", "unsafe"])
        self.cache.setCurrentText(str(value.get("cache", "none")))
        self.aio = QComboBox()
        self.aio.addItems(["native", "threads", "io_uring"])
        self.aio.setCurrentText(str(value.get("aio", "native")))
        self.discard = QComboBox()
        self.discard.addItems(["unmap", "ignore"])
        self.discard.setCurrentText(str(value.get("discard", "unmap")))
        self.read_only = QCheckBox("Attach read-only")
        self.read_only.setChecked(bool(value.get("read_only", False)))
        form = QFormLayout()
        form.addRow("Device name", self.name)
        form.addRow("Image path", path_row)
        form.addRow("Image format", self.format)
        form.addRow("Controller", self.controller)
        form.addRow("Cache mode", self.cache)
        form.addRow("Asynchronous I/O", self.aio)
        form.addRow("Discard/TRIM", self.discard)
        form.addRow("Access", self.read_only)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self._validate)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select virtual disk", str(REPO_ROOT), "Virtual disks (*.qcow2 *.img *.raw);;All files (*)")
        if path:
            self.path.setText(path)
            self.format.setCurrentText("qcow2" if path.endswith(".qcow2") else "raw")

    def _validate(self) -> None:
        if not self.path.text().strip():
            QMessageBox.warning(self, "Disk image required", "Select or create a virtual disk image.")
            return
        self.accept()

    def value(self) -> dict[str, object]:
        return {
            "name": self.name.text().strip() or "Hard Disk",
            "path": self.path.text().strip(),
            "format": self.format.currentText(),
            "controller": self.controller.currentText(),
            "cache": self.cache.currentText(),
            "aio": self.aio.currentText(),
            "discard": self.discard.currentText(),
            "read_only": self.read_only.isChecked(),
        }


class NetworkDeviceDialog(QDialog):
    """Edit one virtual network adapter."""

    def __init__(self, device: dict[str, object] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        value = device or {}
        self.setWindowTitle("Network Adapter")
        self.setMinimumWidth(560)
        self.mode = QComboBox()
        self.mode.addItems(["user", "tap", "bridge", "macvtap", "socket", "none"])
        self.mode.setCurrentText(str(value.get("type", "user")))
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems(["e1000", "vmxnet3", "virtio-net-pci", "virtio-net-device", "rtl8139"])
        self.model.setCurrentText(str(value.get("model", "e1000")))
        self.ifname = QLineEdit(str(value.get("ifname", "tap0")))
        self.bridge = QLineEdit(str(value.get("bridge", "br0")))
        self.mac = QLineEdit(str(value.get("mac", "")))
        self.mac.setPlaceholderText("Automatic")
        forwards = value.get("hostfwd", [])
        self.hostfwd = QLineEdit(",".join(str(item) for item in forwards) if isinstance(forwards, list) else str(forwards))
        self.hostfwd.setPlaceholderText("tcp::2222-:22,tcp::8080-:80")
        self.connected = QCheckBox("Connected at power on")
        self.connected.setChecked(bool(value.get("connected", True)))
        form = QFormLayout()
        form.addRow("Connection type", self.mode)
        form.addRow("Adapter model", self.model)
        form.addRow("Host interface", self.ifname)
        form.addRow("Bridge", self.bridge)
        form.addRow("MAC address", self.mac)
        form.addRow("Port forwarding", self.hostfwd)
        form.addRow("Connection", self.connected)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.mode.currentTextChanged.connect(self._update_enabled)
        self._update_enabled()

    def _update_enabled(self) -> None:
        mode = self.mode.currentText()
        self.model.setEnabled(mode != "none")
        self.ifname.setEnabled(mode in ("tap", "macvtap"))
        self.bridge.setEnabled(mode == "bridge")
        self.hostfwd.setEnabled(mode == "user")
        self.connected.setEnabled(mode != "none")

    def value(self) -> dict[str, object]:
        mode = self.mode.currentText()
        if mode == "none":
            return {"type": "none"}
        value: dict[str, object] = {
            "type": mode,
            "model": self.model.currentText().strip() or "e1000",
            "connected": self.connected.isChecked(),
        }
        if self.mac.text().strip():
            value["mac"] = self.mac.text().strip()
        if mode in ("tap", "macvtap"):
            value["ifname"] = self.ifname.text().strip() or ("tap0" if mode == "tap" else "edge-macvtap0")
        elif mode == "bridge":
            value["bridge"] = self.bridge.text().strip() or "br0"
        elif mode == "user" and self.hostfwd.text().strip():
            value["hostfwd"] = [item.strip() for item in self.hostfwd.text().split(",") if item.strip()]
        return value


class UsbDeviceDialog(QDialog):
    """Describe a host USB device for QEMU passthrough."""

    def __init__(self, device: dict[str, object] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        value = device or {}
        self.setWindowTitle("USB Passthrough Device")
        self.name = QLineEdit(str(value.get("name", "USB Device")))
        self.vendor_id = QLineEdit(str(value.get("vendor_id", "")))
        self.vendor_id.setPlaceholderText("05ac")
        self.product_id = QLineEdit(str(value.get("product_id", "")))
        self.product_id.setPlaceholderText("12a8")
        self.serial = QLineEdit(str(value.get("serial", "")))
        self.serial.setPlaceholderText("Optional")
        form = QFormLayout()
        form.addRow("Device name", self.name)
        form.addRow("Vendor ID", self.vendor_id)
        form.addRow("Product ID", self.product_id)
        form.addRow("Serial number", self.serial)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self._validate)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        for label, value in (("Vendor ID", self.vendor_id.text()), ("Product ID", self.product_id.text())):
            if len(value.strip()) != 4 or any(character not in "0123456789abcdefABCDEF" for character in value.strip()):
                QMessageBox.warning(self, "Invalid USB identifier", f"{label} must contain exactly four hexadecimal digits.")
                return
        self.accept()

    def value(self) -> dict[str, object]:
        value: dict[str, object] = {
            "name": self.name.text().strip() or "USB Device",
            "vendor_id": self.vendor_id.text().strip().lower(),
            "product_id": self.product_id.text().strip().lower(),
        }
        if self.serial.text().strip():
            value["serial"] = self.serial.text().strip()
        return value


class SharedFolderDialog(QDialog):
    """Configure one host directory shared with the guest through virtio-9p."""

    def __init__(self, folder: dict[str, object] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        value = folder or {}
        self.setWindowTitle("Shared Folder")
        self.setMinimumWidth(620)
        self.tag = QLineEdit(str(value.get("tag", "shared")))
        self.path = QLineEdit(str(value.get("path", "")))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.path, 1)
        path_layout.addWidget(browse)
        self.mount_path = QLineEdit(str(value.get("mount_path", "/mnt/hgfs/shared")))
        self.read_only = QCheckBox("Read-only")
        self.read_only.setChecked(bool(value.get("read_only", False)))
        form = QFormLayout()
        form.addRow("Share name", self.tag)
        form.addRow("Host folder", path_row)
        form.addRow("Guest mount point", self.mount_path)
        form.addRow("Access", self.read_only)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self._validate)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Shared Folder", str(Path.home()))
        if path:
            self.path.setText(path)
            if self.tag.text().strip() in ("", "shared"):
                self.tag.setText(Path(path).name.replace(" ", "-") or "shared")

    def _validate(self) -> None:
        tag = self.tag.text().strip()
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        if not tag or any(character not in allowed for character in tag):
            QMessageBox.warning(self, "Invalid share name", "Use letters, numbers, periods, underscores, and hyphens only.")
            return
        path = Path(self.path.text().strip()).expanduser()
        if not path.is_dir() or "," in str(path):
            QMessageBox.warning(self, "Invalid host folder", "Choose an existing folder whose path does not contain a comma.")
            return
        self.accept()

    def value(self) -> dict[str, object]:
        tag = self.tag.text().strip()
        return {
            "tag": tag,
            "path": str(Path(self.path.text().strip()).expanduser().resolve()),
            "mount_path": self.mount_path.text().strip() or f"/mnt/hgfs/{tag}",
            "read_only": self.read_only.isChecked(),
        }


class CreateVmDialog(QDialog):
    """Guided virtual machine creation workflow."""

    STEP_NAMES = [
        "Welcome",
        "Guest Operating System",
        "Name the Virtual Machine",
        "Processor Configuration",
        "Memory for the Virtual Machine",
        "Virtual Machine Hardware",
        "Ready to Create",
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("VmWizard")
        self.setWindowTitle("New Virtual Machine Wizard")
        self.setMinimumSize(1120, 700)
        self.resize(1240, 760)

        self.steps = QListWidget()
        self.steps.setObjectName("WizardSteps")
        self.steps.setFixedWidth(285)
        self.steps.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.steps.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for number, title in enumerate(self.STEP_NAMES, 1):
            item = QListWidgetItem(f"{number}   {title}")
            self.steps.addItem(item)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._welcome_page())
        self.pages.addWidget(self._guest_page())
        self.pages.addWidget(self._name_page())
        self.pages.addWidget(self._processor_page())
        self.pages.addWidget(self._memory_page())
        self.pages.addWidget(self._hardware_page())
        self.pages.addWidget(self._summary_page())
        self.steps.itemPressed.connect(
            lambda _item: QTimer.singleShot(
                0, lambda: self.steps.setCurrentRow(self.pages.currentIndex())
            )
        )

        self.back_btn = QPushButton("< Back")
        self.next_btn = QPushButton("Next >")
        self.finish_btn = QPushButton("Finish")
        self.finish_btn.setObjectName("PrimaryButton")
        cancel_btn = QPushButton("Cancel")
        self.back_btn.clicked.connect(self.previous_page)
        self.next_btn.clicked.connect(self.next_page)
        self.finish_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.steps)
        body.addWidget(self.pages, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.back_btn)
        buttons.addWidget(self.next_btn)
        buttons.addWidget(self.finish_btn)
        buttons.addWidget(cancel_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 14)
        layout.addLayout(body, 1)
        layout.addLayout(buttons)
        self.update_architecture_defaults()
        self.show_page(0)

    def _page(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("WizardPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(44, 36, 44, 30)
        layout.setSpacing(14)
        heading = QLabel(title)
        heading.setObjectName("WizardTitle")
        detail = QLabel(subtitle)
        detail.setObjectName("WizardSubtitle")
        detail.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(detail)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)
        return page, layout

    def _welcome_page(self) -> QWidget:
        page, layout = self._page(
            "Welcome to the New Virtual Machine Wizard",
            "This wizard will help you create a complete EdgeOS virtual machine.",
        )
        self.typical = QRadioButton("Typical (recommended)")
        self.custom = QRadioButton("Custom (advanced)")
        self.typical.setChecked(True)
        typical_help = QLabel("Uses recommended virtual hardware while still letting you choose CPU, memory, disk, network, and display settings.")
        custom_help = QLabel("Exposes firmware, storage I/O, graphics acceleration, boot, and device options.")
        for widget in (typical_help, custom_help):
            widget.setObjectName("FieldHelp")
            widget.setWordWrap(True)
        layout.addSpacing(18)
        layout.addWidget(self.typical)
        layout.addWidget(typical_help)
        layout.addSpacing(10)
        layout.addWidget(self.custom)
        layout.addWidget(custom_help)
        layout.addStretch(1)
        return page

    def _guest_page(self) -> QWidget:
        page, layout = self._page(
            "Select a Guest Operating System",
            "Choose a generated EdgeOS environment or import an existing Linux root filesystem image.",
        )
        self.profile = QComboBox()
        self.profile.addItem("EdgeOS with Alpine userspace", "edgeos")
        self.profile.addItem("Alpine Linux root filesystem", "alpine")
        self.profile.addItem("Debian 13 with systemd", "debian")
        self.profile.addItem("Use an existing root filesystem image", "imported")
        self.architecture = QComboBox()
        self.architecture.addItem("x86-64", "x86_64")
        self.architecture.addItem("ARM 64-bit", "arm64")
        if sys.platform == "darwin" and os.uname().machine in ("arm64", "aarch64"):
            self.architecture.setCurrentIndex(self.architecture.findData("arm64"))
        self.import_rootfs = QLineEdit()
        self.import_rootfs.setPlaceholderText("Select an ext2/ext4 raw disk image")
        self.import_browse = QPushButton("Browse…")
        self.import_browse.clicked.connect(self.pick_rootfs)
        image_row = QHBoxLayout()
        image_row.addWidget(self.import_rootfs, 1)
        image_row.addWidget(self.import_browse)
        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.setVerticalSpacing(16)
        form.addRow("Guest operating system", self.profile)
        form.addRow("Architecture", self.architecture)
        form.addRow("Existing disk image", image_row)
        layout.addLayout(form)
        layout.addStretch(1)
        self.profile.currentIndexChanged.connect(self.update_enabled)
        self.architecture.currentIndexChanged.connect(self.update_architecture_defaults)
        return page

    def _name_page(self) -> QWidget:
        page, layout = self._page(
            "Name the Virtual Machine",
            "Choose a unique library name and the hostname presented inside the guest.",
        )
        self.name = QLineEdit()
        self.name.setPlaceholderText("Example: edgeos-development")
        self.hostname = QLineEdit()
        self.hostname.setPlaceholderText("Defaults to the virtual machine name")
        self.description = QLineEdit()
        self.description.setPlaceholderText("Optional description")
        location = QLineEdit(str(INSTANCES_DIR))
        location.setReadOnly(True)
        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.setVerticalSpacing(16)
        form.addRow("Virtual machine name", self.name)
        form.addRow("Guest hostname", self.hostname)
        form.addRow("Description", self.description)
        form.addRow("Location", location)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _processor_page(self) -> QWidget:
        page, layout = self._page(
            "Processor Configuration",
            "Allocate virtual processors and select the host acceleration strategy.",
        )
        self.sockets = QSpinBox()
        self.sockets.setRange(1, 16)
        self.sockets.setValue(1)
        self.cores = QSpinBox()
        self.cores.setRange(1, 64)
        self.cores.setValue(4)
        self.threads = QSpinBox()
        self.threads.setRange(1, 8)
        self.threads.setValue(1)
        self.cpu_total = QLabel()
        self.cpu_total.setObjectName("Recommendation")
        self.accelerator = QComboBox()
        self.accelerator.addItems(["auto", "hvf", "kvm", "whpx", "nvmm", "tcg"])
        self.cpu_model = QLineEdit()
        self.cpu_model.setPlaceholderText("Automatic host-compatible CPU model")
        self.build_jobs = QSpinBox()
        self.build_jobs.setRange(0, MAX_BUILD_JOBS)
        self.build_jobs.setSpecialValueText(
            f"Automatic ({min(MAX_BUILD_JOBS, max(1, os.cpu_count() or 1))})"
        )
        self.build_jobs.setValue(0)
        self.build_jobs.setToolTip(
            "Maximum parallel compiler jobs; Automatic uses all available host CPUs"
        )
        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.setVerticalSpacing(14)
        form.addRow("Number of processors", self.sockets)
        form.addRow("Number of cores per processor", self.cores)
        form.addRow("Threads per core", self.threads)
        form.addRow("Total processor cores", self.cpu_total)
        form.addRow("Virtualization engine", self.accelerator)
        form.addRow("CPU model override", self.cpu_model)
        form.addRow("Parallel build jobs", self.build_jobs)
        layout.addLayout(form)
        layout.addStretch(1)
        for control in (self.sockets, self.cores, self.threads):
            control.valueChanged.connect(self.update_cpu_total)
        self.update_cpu_total()
        return page

    def _memory_page(self) -> QWidget:
        page, layout = self._page(
            "Memory for the Virtual Machine",
            "Specify guest memory. EdgeOS requires at least 2 GB for the current boot image.",
        )
        self.memory_mb = QSpinBox()
        self.memory_mb.setRange(2048, 262144)
        self.memory_mb.setSingleStep(512)
        self.memory_mb.setValue(4096)
        self.memory_mb.setSuffix(" MB")
        recommended = QLabel("Recommended: 4096 MB   •   Minimum supported: 2048 MB")
        recommended.setObjectName("Recommendation")
        layout.addSpacing(24)
        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.addRow("Memory for this virtual machine", self.memory_mb)
        layout.addLayout(form)
        layout.addWidget(recommended)
        layout.addStretch(1)
        return page

    def _hardware_page(self) -> QWidget:
        page, layout = self._page(
            "Configure Virtual Machine Hardware",
            "Review storage, display, network, input, firmware, and boot devices.",
        )
        hardware = QWidget()
        hardware_layout = QGridLayout(hardware)
        hardware_layout.setHorizontalSpacing(28)
        hardware_layout.setVerticalSpacing(10)
        hardware_layout.setColumnStretch(1, 1)
        hardware_layout.setColumnStretch(3, 1)
        self.rootfs_size = QSpinBox()
        self.rootfs_size.setRange(64, 1024 * 1024)
        self.rootfs_size.setValue(2048)
        self.rootfs_size.setSuffix(" MB")
        self.disk_controller = QComboBox()
        self.disk_controller.addItems(["nvme", "virtio-blk", "ide"])
        self.disk_cache = QComboBox()
        self.disk_cache.addItems(["none", "writeback", "writethrough", "unsafe"])
        self.disk_aio = QComboBox()
        self.disk_aio.addItems(["native", "threads", "io_uring"])
        self.disk_discard = QComboBox()
        self.disk_discard.addItems(["unmap", "ignore"])
        self.disk_read_only = QCheckBox("Make the primary disk read-only")
        self.network = QComboBox()
        self.network.addItems(["user", "tap", "bridge", "macvtap", "none"])
        self.nic_model = QComboBox()
        self.nic_model.addItems(["e1000", "vmxnet3", "virtio-net-pci", "virtio-net-device", "rtl8139"])
        self.network_name = QLineEdit()
        self.network_name.setPlaceholderText("tap0, br0, or edge-macvtap0")
        self.hostfwd = QLineEdit()
        self.hostfwd.setPlaceholderText("tcp::2222-:22")
        self.network_connected = QCheckBox("Connect at power on")
        self.network_connected.setChecked(True)
        self.gpu = QComboBox()
        self.gpu.addItems(["std", "virtio-gpu", "virtio-vga", "virtio-gpu-gl-pci", "virtio-vga-gl", "qxl", "bochs-display", "none"])
        self.virgl = QCheckBox("Enable accelerated 3D graphics")
        self.display_backend = QComboBox()
        self.display_backend.addItems(["default", "gtk", "gtk,gl=on", "sdl"])
        self.usb = QComboBox()
        self.usb.addItems(["virtio-input", "xhci-input", "xhci-kbd-uhci-mouse", "xhci-mouse", "xhci-keyboard", "uhci-mouse", "off"])
        self.sound = QComboBox()
        self.sound.addItems(["none", "hda", "ac97"])
        self.firmware_mode = QComboBox()
        self.firmware_mode.addItems(["auto", "uefi", "bios"])
        self.boot_order = QComboBox()
        self.boot_order.addItem("CD/DVD, then hard disk", "dc")
        self.boot_order.addItem("Hard disk, then CD/DVD", "cd")
        self.boot_order.addItem("CD/DVD only", "d")
        self.boot_order.addItem("Hard disk only", "c")
        self.boot_menu = QCheckBox("Show the firmware boot menu")
        self.boot_menu.setChecked(True)
        self.balloon = QCheckBox("Enable memory ballooning")
        self.rng = QCheckBox("Add a virtual random number generator")
        self.rng.setChecked(True)
        self.desktop = QComboBox()
        self.desktop.addItem("Serial console", "console")
        self.desktop.addItem("XFCE desktop", "xfce")
        self.reconfigure = QCheckBox("Regenerate the kernel configuration")
        fields = [
            ("Primary disk capacity", self.rootfs_size), ("Disk controller", self.disk_controller),
            ("Disk cache", self.disk_cache), ("Asynchronous I/O", self.disk_aio),
            ("Discard/TRIM", self.disk_discard), ("", self.disk_read_only),
            ("Network connection", self.network), ("Adapter model", self.nic_model),
            ("Network name", self.network_name), ("Port forwarding", self.hostfwd),
            ("", self.network_connected), ("Display adapter", self.gpu), ("", self.virgl),
            ("Window display backend", self.display_backend), ("USB controller and input", self.usb),
            ("Sound card", self.sound), ("Firmware type", self.firmware_mode),
            ("Boot order", self.boot_order), ("", self.boot_menu), ("", self.balloon),
            ("", self.rng), ("Default boot experience", self.desktop), ("", self.reconfigure),
        ]
        rows_per_column = (len(fields) + 1) // 2
        for index, (label, widget) in enumerate(fields):
            column_group = index // rows_per_column
            row = index % rows_per_column
            label_widget = QLabel(label)
            label_widget.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            widget.setMinimumWidth(230)
            hardware_layout.addWidget(label_widget, row, column_group * 2)
            hardware_layout.addWidget(widget, row, column_group * 2 + 1)
        layout.addWidget(hardware, 1)
        self.network.currentTextChanged.connect(self.update_enabled)
        return page

    def _summary_page(self) -> QWidget:
        page, layout = self._page(
            "Ready to Create Virtual Machine",
            "Click Finish to create the disk image, build the EdgeOS kernel, and add the machine to the library.",
        )
        self.summary = QLabel()
        self.summary.setObjectName("WizardSummary")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary, 1)
        return page

    def show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.steps.setCurrentRow(index)
        self.back_btn.setEnabled(index > 0)
        self.next_btn.setVisible(index < self.pages.count() - 1)
        self.finish_btn.setVisible(index == self.pages.count() - 1)
        if index == self.pages.count() - 1:
            self.update_summary()
        self.update_enabled()

    def previous_page(self) -> None:
        self.show_page(max(0, self.pages.currentIndex() - 1))

    def next_page(self) -> None:
        if self.validate_page(self.pages.currentIndex()):
            self.show_page(min(self.pages.count() - 1, self.pages.currentIndex() + 1))

    def validate_page(self, index: int) -> bool:
        if index == 1 and self.profile.currentData() == "imported":
            path = self.import_rootfs.text().strip()
            if not path or not Path(path).is_file():
                QMessageBox.warning(self, "Root filesystem required", "Select an existing root filesystem image.")
                return False
        if index == 2:
            name = self.name.text().strip()
            if not name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in name):
                QMessageBox.warning(self, "Invalid virtual machine name", "Use letters, numbers, periods, underscores, and hyphens only.")
                return False
            if (INSTANCES_DIR / name).exists():
                QMessageBox.warning(self, "Virtual machine already exists", f"A virtual machine named '{name}' already exists.")
                return False
        return True

    def update_cpu_total(self) -> None:
        total = self.sockets.value() * self.cores.value() * self.threads.value()
        self.cpu_total.setText(f"{total} virtual CPU{'s' if total != 1 else ''}")

    def update_architecture_defaults(self) -> None:
        arm64 = self.architecture.currentData() == "arm64"
        if arm64:
            if self.profile.currentData() == "edgeos":
                self.profile.setCurrentIndex(self.profile.findData("alpine"))
            self.accelerator.setCurrentText("auto")
            self.disk_controller.setCurrentText("virtio-blk")
            self.nic_model.setCurrentText("virtio-net-device")
            self.gpu.setCurrentText("none")
            self.sound.setCurrentText("none")
            self.firmware_mode.setCurrentText("uefi")
            self.usb.setCurrentText("off")
        else:
            self.accelerator.setCurrentText("auto")
            self.disk_controller.setCurrentText("nvme")
            self.nic_model.setCurrentText("e1000")
            self.gpu.setCurrentText("std")

    def pick_rootfs(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select root filesystem image", str(REPO_ROOT), "Disk images (*.img *.raw);;All files (*)")
        if path:
            self.import_rootfs.setText(path)

    def update_enabled(self) -> None:
        if not hasattr(self, "profile") or not hasattr(self, "network"):
            return
        imported = self.profile.currentData() == "imported"
        self.import_rootfs.setEnabled(imported)
        self.import_browse.setEnabled(imported)
        self.rootfs_size.setEnabled(not imported)
        mode = self.network.currentText()
        self.network_name.setEnabled(mode in ("tap", "bridge", "macvtap"))
        self.hostfwd.setEnabled(mode == "user")
        self.nic_model.setEnabled(mode != "none")
        self.network_connected.setEnabled(mode != "none")
        advanced = self.custom.isChecked()
        for widget in (self.disk_cache, self.disk_aio, self.disk_discard, self.disk_read_only,
                       self.display_backend, self.firmware_mode, self.boot_order, self.boot_menu,
                       self.balloon, self.rng, self.reconfigure):
            widget.setEnabled(advanced)

    def update_summary(self) -> None:
        source = self.profile.currentText()
        total = self.sockets.value() * self.cores.value() * self.threads.value()
        network = self.network.currentText()
        summary = [
            f"<h3>{self.name.text().strip()}</h3>",
            "<table cellspacing='8'>",
            f"<tr><td width='190'>Guest operating system</td><td><b>{source}</b></td></tr>",
            f"<tr><td>Architecture</td><td><b>{self.architecture.currentText()}</b></td></tr>",
            f"<tr><td>Processors</td><td><b>{total} vCPU ({self.sockets.value()} socket × {self.cores.value()} cores × {self.threads.value()} threads)</b></td></tr>",
            f"<tr><td>Build parallelism</td><td><b>{self.build_jobs.text()}</b></td></tr>",
            f"<tr><td>Memory</td><td><b>{self.memory_mb.value()} MB</b></td></tr>",
            f"<tr><td>Primary disk</td><td><b>{'Imported image' if self.profile.currentData() == 'imported' else str(self.rootfs_size.value()) + ' MB'} on {self.disk_controller.currentText()}</b></td></tr>",
            f"<tr><td>Network adapter</td><td><b>{network} / {self.nic_model.currentText()}</b></td></tr>",
            f"<tr><td>Display</td><td><b>{self.gpu.currentText()}</b></td></tr>",
            f"<tr><td>Firmware</td><td><b>{self.firmware_mode.currentText()}</b></td></tr>",
            f"<tr><td>Default experience</td><td><b>{self.desktop.currentText()}</b></td></tr>",
            "</table>",
        ]
        self.summary.setText("".join(summary))

    def command_args(self) -> list[str] | None:
        for index in (1, 2):
            if not self.validate_page(index):
                return None
        name = self.name.text().strip()
        total = self.sockets.value() * self.cores.value() * self.threads.value()
        args = [
            "create", name, "--architecture", str(self.architecture.currentData()),
            "--description", self.description.text().strip(),
            "--hostname", self.hostname.text().strip() or name,
            "--cpus", str(total), "--cpu-sockets", str(self.sockets.value()),
            "--cpu-cores", str(self.cores.value()), "--cpu-threads", str(self.threads.value()),
            "--jobs", str(self.build_jobs.value()),
            "--memory", f"{self.memory_mb.value()}M", "--accelerator", self.accelerator.currentText(),
            "--gpu", self.gpu.currentText(), "--display-backend", self.display_backend.currentText(),
            "--usb", self.usb.currentText(), "--disk-controller", self.disk_controller.currentText(),
            "--disk-cache", self.disk_cache.currentText(), "--disk-aio", self.disk_aio.currentText(),
            "--disk-discard", self.disk_discard.currentText(), "--firmware-mode", self.firmware_mode.currentText(),
            "--boot-order", str(self.boot_order.currentData()), "--rtc-base", "utc",
            "--sound", self.sound.currentText(), "--desktop", str(self.desktop.currentData()),
        ]
        profile = str(self.profile.currentData())
        if profile == "imported":
            args += ["--import-rootfs", self.import_rootfs.text().strip()]
        else:
            args += ["--template", profile, "--rootfs-size-mb", str(self.rootfs_size.value())]
        mode = self.network.currentText()
        if mode == "none":
            net = "none"
        else:
            net = f"{mode},model={self.nic_model.currentText()},connected={'true' if self.network_connected.isChecked() else 'false'}"
            name_value = self.network_name.text().strip()
            if mode in ("tap", "macvtap"):
                net += f",ifname={name_value or ('tap0' if mode == 'tap' else 'edge-macvtap0')}"
            elif mode == "bridge":
                net += f",bridge={name_value or 'br0'}"
            elif mode == "user" and self.hostfwd.text().strip():
                net += f",hostfwd={self.hostfwd.text().strip()}"
        args += ["--net", net]
        for checked, flag in [
            (self.virgl.isChecked(), "--virgl"), (self.disk_read_only.isChecked(), "--disk-read-only"),
            (not self.boot_menu.isChecked(), "--no-boot-menu"), (self.balloon.isChecked(), "--balloon"),
            (self.rng.isChecked(), "--rng"), (self.reconfigure.isChecked(), "--reconfigure"),
        ]:
            if checked:
                args.append(flag)
        if self.cpu_model.text().strip():
            args += ["--cpu-model", self.cpu_model.text().strip()]
        return args


class ConfigDialog(QDialog):
    def __init__(self, name: str, cfg: dict[str, object], cfg_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("VmSettings")
        self.setWindowTitle(f"Virtual Machine Settings — {name}")
        self.setMinimumSize(980, 680)
        self.resize(1040, 720)
        self.cfg = dict(cfg)
        self.cfg_path = cfg_path

        self.cpus = QSpinBox()
        self.cpus.setRange(1, 128)
        self.cpus.setValue(int(cfg.get("cpus", 4)))
        self.cpu_sockets = QSpinBox()
        self.cpu_sockets.setRange(1, 16)
        self.cpu_sockets.setValue(int(cfg.get("cpu_sockets", 1)))
        self.cpu_cores = QSpinBox()
        self.cpu_cores.setRange(1, 64)
        self.cpu_cores.setValue(int(cfg.get("cpu_cores", cfg.get("cpus", 4))))
        self.cpu_threads = QSpinBox()
        self.cpu_threads.setRange(1, 8)
        self.cpu_threads.setValue(int(cfg.get("cpu_threads", 1)))
        self.cpu_total = QLabel()
        self.cpu_total.setObjectName("Recommendation")
        self.build_jobs = QSpinBox()
        self.build_jobs.setRange(0, MAX_BUILD_JOBS)
        self.build_jobs.setSpecialValueText(
            f"Automatic ({min(MAX_BUILD_JOBS, max(1, os.cpu_count() or 1))})"
        )
        self.build_jobs.setValue(int(cfg.get("build_jobs", 0)))
        self.build_jobs.setToolTip(
            "Maximum parallel compiler jobs; Automatic uses all available host CPUs"
        )
        self.architecture = QComboBox()
        self.architecture.addItems(["x86_64", "arm64"])
        self.architecture.setCurrentText(str(cfg.get("architecture", "x86_64")))
        self.architecture.setEnabled(False)
        self.description = QLineEdit(str(cfg.get("description", "")))
        self.hostname = QLineEdit(str(cfg.get("hostname", name)))
        self.memory = QComboBox()
        self.memory.setEditable(True)
        self.memory.addItems(["2048M", "4096M", "8192M", "16384M", "512M", "1024M"])
        self.memory.setCurrentText(str(cfg.get("memory", "2048M")))
        self.accelerator = QComboBox()
        self.accelerator.addItems(["auto", "tcg", "hvf", "kvm", "nvmm", "whpx"])
        self.accelerator.setCurrentText(str(cfg.get("accelerator", "auto")))
        self.machine = QLineEdit(str(cfg.get("machine", "pc,accel=kvm")))
        self.cpu_model = QLineEdit(str(cfg.get("cpu_model", "host,migratable=off")))
        self.boot_params = QLineEdit(" ".join(str(x) for x in cfg.get("boot_params", ["console=ttyS0"]) if isinstance(x, str)))
        self.boot_experience = QComboBox()
        self.boot_experience.addItem("Console", "console")
        self.boot_experience.addItem("XFCE Desktop", "xfce")
        desktop_mode = str(cfg.get("desktop", "console"))
        self.boot_experience.setCurrentIndex(max(0, self.boot_experience.findData(desktop_mode)))

        self.gpu = QComboBox()
        self.gpu.setEditable(True)
        self.gpu.addItems(["std", "virtio-gpu", "virtio-vga", "virtio-gpu-gl-pci", "virtio-vga-gl", "qxl", "bochs-display", "none"])
        self.gpu.setCurrentText(str(cfg.get("gpu", "std")))
        self.virgl = QCheckBox("Enable VirGL/GL")
        self.virgl.setChecked(bool(cfg.get("virgl", False)))
        self.display_backend = QComboBox()
        self.display_backend.setEditable(True)
        self.display_backend.addItems(["default", "gtk", "gtk,gl=on", "sdl"])
        self.display_backend.setCurrentText(str(cfg.get("display_backend", "default")))
        self.usb = QComboBox()
        self.usb.addItems(["virtio-input", "xhci-input", "xhci-kbd-uhci-mouse", "xhci-mouse", "xhci-keyboard", "uhci-mouse", "off"])
        self.usb.setCurrentText(str(cfg.get("usb", "virtio-input")))
        self.nodefaults = QCheckBox("Disable QEMU default devices")
        self.nodefaults.setChecked(bool(cfg.get("nodefaults", str(cfg.get("usb", "virtio-input")).startswith("xhci"))))

        self.disk_controller = QComboBox()
        self.disk_controller.setEditable(True)
        self.disk_controller.addItems(["nvme", "virtio-blk", "ide"])
        self.disk_controller.setCurrentText(str(cfg.get("disk_controller", "nvme")))
        self.disk_cache = QComboBox()
        self.disk_cache.setEditable(True)
        self.disk_cache.addItems(["none", "writeback", "writethrough", "unsafe"])
        self.disk_cache.setCurrentText(str(cfg.get("disk_cache", "none")))
        self.disk_aio = QComboBox()
        self.disk_aio.setEditable(True)
        self.disk_aio.addItems(["native", "threads", "io_uring"])
        self.disk_aio.setCurrentText(str(cfg.get("disk_aio", "native")))
        self.disk_discard = QComboBox()
        self.disk_discard.addItems(["unmap", "ignore"])
        self.disk_discard.setCurrentText(str(cfg.get("disk_discard", "unmap")))
        self.disk_read_only = QCheckBox("Read-only")
        self.disk_read_only.setChecked(bool(cfg.get("disk_read_only", False)))
        storage = cfg.get("storage", [])
        self.storage_devices = [dict(item) for item in storage if isinstance(item, dict)] if isinstance(storage, list) else []

        networks = cfg.get("networks", [])
        first_net = networks[0] if isinstance(networks, list) and networks and isinstance(networks[0], dict) else {"type": "none", "model": "e1000"}
        self.network = QComboBox()
        self.network.addItems(["user", "tap", "bridge", "macvtap", "none"])
        self.network.setCurrentText(str(first_net.get("type", "user")))
        self.nic_model = QComboBox()
        self.nic_model.setEditable(True)
        self.nic_model.addItems(["e1000", "vmxnet3", "virtio-net-pci", "virtio-net-device", "rtl8139"])
        self.nic_model.setCurrentText(str(first_net.get("model", "e1000")))
        self.tap_name = QLineEdit(str(first_net.get("ifname", "tap0")))
        self.bridge_name = QLineEdit(str(first_net.get("bridge", "br0")))
        hostfwd = first_net.get("hostfwd", [])
        if isinstance(hostfwd, list):
            hostfwd_text = ",".join(str(x) for x in hostfwd)
        else:
            hostfwd_text = str(hostfwd)
        self.hostfwd = QLineEdit(hostfwd_text)
        self.mac_address = QLineEdit(str(first_net.get("mac", "")))
        self.mac_address.setPlaceholderText("Automatic")
        self.network_connected = QCheckBox("Connected at power on")
        self.network_connected.setChecked(bool(first_net.get("connected", True)))
        self.network_devices = [dict(item) for item in networks if isinstance(item, dict)] if isinstance(networks, list) else []

        self.sound = QComboBox()
        self.sound.addItems(["none", "hda", "ac97"])
        self.sound.setCurrentText(str(cfg.get("sound", "none")))
        self.balloon = QCheckBox("Enable memory ballooning")
        self.balloon.setChecked(bool(cfg.get("balloon", False)))
        self.rng = QCheckBox("Add virtual random number generator")
        self.rng.setChecked(bool(cfg.get("rng", False)))
        self.firmware_mode = QComboBox()
        self.firmware_mode.addItems(["auto", "uefi", "bios"])
        self.firmware_mode.setCurrentText(str(cfg.get("firmware_mode", "auto")))
        self.firmware = QLineEdit(str(cfg.get("firmware", "")))
        self.firmware.setPlaceholderText("Automatic EDK2 firmware path")
        self.boot_order = QComboBox()
        for title, value in [
            ("CD/DVD, then hard disk", "dc"), ("Hard disk, then CD/DVD", "cd"),
            ("CD/DVD only", "d"), ("Hard disk only", "c"), ("Network first", "ncd"),
        ]:
            self.boot_order.addItem(title, value)
        boot_value = str(cfg.get("boot_order", "d"))
        self.boot_order.setCurrentIndex(max(0, self.boot_order.findData(boot_value)))
        self.boot_menu = QCheckBox("Show firmware boot menu")
        self.boot_menu.setChecked(bool(cfg.get("boot_menu", True)))
        self.boot_delay = QSpinBox()
        self.boot_delay.setRange(0, 30000)
        self.boot_delay.setValue(int(cfg.get("boot_delay_ms", 0)))
        self.boot_delay.setSuffix(" ms")
        self.rtc_base = QComboBox()
        self.rtc_base.addItems(["utc", "localtime"])
        self.rtc_base.setCurrentText(str(cfg.get("rtc_base", "utc")))
        self.cdrom_connected = QCheckBox("Connect the EdgeOS boot image at power on")
        self.cdrom_connected.setChecked(bool(cfg.get("cdrom_connected", True)))
        usb_devices = cfg.get("usb_devices", [])
        self.usb_devices = [dict(item) for item in usb_devices if isinstance(item, dict)] if isinstance(usb_devices, list) else []
        shared_folders = cfg.get("shared_folders", [])
        self.shared_folders = [dict(item) for item in shared_folders if isinstance(item, dict)] if isinstance(shared_folders, list) else []

        self.qemu_args = QLineEdit(shlex.join([str(x) for x in cfg.get("qemu_args", []) if isinstance(x, str)]))

        def page(title: str, subtitle: str, rows: list[tuple[str, QWidget]]) -> QWidget:
            widget = QWidget()
            outer = QVBoxLayout(widget)
            outer.setContentsMargins(30, 24, 30, 24)
            heading = QLabel(title)
            heading.setObjectName("SettingsTitle")
            detail = QLabel(subtitle)
            detail.setObjectName("SettingsSubtitle")
            detail.setWordWrap(True)
            outer.addWidget(heading)
            outer.addWidget(detail)
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            outer.addWidget(line)
            form = QFormLayout()
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            )
            form.setVerticalSpacing(13)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            for label, control in rows:
                form.addRow(label, control)
            outer.addLayout(form)
            outer.addStretch(1)
            return widget

        pages = [
            ("General", page("General", "Identity and guest operating system defaults.", [
                ("Virtual machine", QLabel(name)), ("Description", self.description),
                ("Guest hostname", self.hostname), ("Architecture", self.architecture),
                ("Default boot experience", self.boot_experience),
            ])),
            ("Processors", page("Processors", "Configure the virtual CPU topology and execution engine.", [
                ("Processors", self.cpu_sockets), ("Cores per processor", self.cpu_cores),
                ("Threads per core", self.cpu_threads), ("Total virtual CPUs", self.cpu_total),
                ("Virtualization engine", self.accelerator), ("CPU model", self.cpu_model),
                ("Machine type", self.machine), ("Parallel build jobs", self.build_jobs),
            ])),
            ("Memory", page("Memory", "Allocate RAM and optional dynamic memory devices.", [
                ("Memory", self.memory), ("", self.balloon), ("", self.rng),
            ])),
            ("Display", page("Display", "Choose the virtual graphics adapter and host rendering backend.", [
                ("Graphics adapter", self.gpu), ("", self.virgl),
                ("Display backend", self.display_backend),
            ])),
            ("Hard Disk", self.build_storage_page()),
            ("CD/DVD", page("CD/DVD", "Control the EdgeOS boot image presented to the guest.", [
                ("Connection", self.cdrom_connected),
            ])),
            ("Network Adapter", self.build_network_page()),
            ("USB Controller", self.build_usb_page()),
            ("Shared Folders", self.build_shared_folders_page()),
            ("Sound Card", page("Sound Card", "Select an emulated audio controller.", [
                ("Audio device", self.sound),
            ])),
            ("Boot Options", page("Boot Options", "Configure firmware, device priority, clock, and kernel parameters.", [
                ("Firmware type", self.firmware_mode), ("Firmware image", self.firmware),
                ("Boot order", self.boot_order), ("", self.boot_menu),
                ("Boot menu delay", self.boot_delay), ("Hardware clock", self.rtc_base),
                ("Kernel parameters", self.boot_params),
            ])),
            ("Advanced", page("Advanced", "Pass expert-level arguments directly to QEMU.", [
                ("Extra QEMU arguments", self.qemu_args),
            ])),
        ]
        categories = QTreeWidget()
        categories.setObjectName("SettingsCategories")
        categories.setFixedWidth(210)
        categories.setHeaderHidden(True)
        stack = QStackedWidget()
        hardware = QTreeWidgetItem(["Hardware"])
        options = QTreeWidgetItem(["Options"])
        categories.addTopLevelItem(hardware)
        categories.addTopLevelItem(options)
        option_names = {"Boot Options", "Advanced"}
        first_item: QTreeWidgetItem | None = None
        for index, (title, widget) in enumerate(pages):
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            (options if title in option_names else hardware).addChild(item)
            if first_item is None:
                first_item = item
            stack.addWidget(widget)
        hardware.setExpanded(True)
        options.setExpanded(True)
        categories.currentItemChanged.connect(
            lambda current, _previous: stack.setCurrentIndex(int(current.data(0, Qt.ItemDataRole.UserRole)))
            if current is not None and current.data(0, Qt.ItemDataRole.UserRole) is not None else None
        )
        if first_item is not None:
            categories.setCurrentItem(first_item)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        header = QLabel(f"Hardware and options for <b>{name}</b>")
        header.setObjectName("SettingsHeader")
        layout.addWidget(header)
        content = QHBoxLayout()
        content.addWidget(categories)
        content.addWidget(stack, 1)
        layout.addLayout(content, 1)
        layout.addWidget(buttons)
        for control in (self.cpu_sockets, self.cpu_cores, self.cpu_threads):
            control.valueChanged.connect(self.update_cpu_total)
        self.update_cpu_total()

    @staticmethod
    def device_page_header(layout: QVBoxLayout, title: str, subtitle: str) -> None:
        heading = QLabel(title)
        heading.setObjectName("SettingsTitle")
        detail = QLabel(subtitle)
        detail.setObjectName("SettingsSubtitle")
        detail.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(detail)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)

    def build_storage_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 24, 30, 24)
        self.device_page_header(layout, "Hard Disks", "Manage the primary system disk and additional raw or QCOW2 virtual disks.")
        primary = QGroupBox("Primary System Disk")
        form = QFormLayout(primary)
        form.addRow("Controller", self.disk_controller)
        form.addRow("Cache mode", self.disk_cache)
        form.addRow("Asynchronous I/O", self.disk_aio)
        form.addRow("Discard/TRIM", self.disk_discard)
        form.addRow("Access mode", self.disk_read_only)
        layout.addWidget(primary)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Additional disks"))
        toolbar.addStretch(1)
        add_existing = QPushButton("Add Existing…")
        add_existing.clicked.connect(self.add_storage_device)
        create = QPushButton("Create Disk…")
        create.clicked.connect(self.create_storage_device)
        edit = QPushButton("Edit…")
        edit.clicked.connect(self.edit_storage_device)
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_storage_device)
        for button in (add_existing, create, edit, remove):
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.storage_list = QTreeWidget()
        self.storage_list.setHeaderLabels(["DEVICE", "IMAGE", "FORMAT", "CONTROLLER"])
        self.storage_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.storage_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.storage_list.itemDoubleClicked.connect(lambda _item, _column: self.edit_storage_device())
        layout.addWidget(self.storage_list, 1)
        self.refresh_storage_devices()
        return page

    def refresh_storage_devices(self) -> None:
        self.storage_list.clear()
        for index, device in enumerate(self.storage_devices):
            item = QTreeWidgetItem([
                str(device.get("name", f"Hard Disk {index + 2}")),
                str(device.get("path", "")),
                str(device.get("format", "raw")).upper(),
                str(device.get("controller", "virtio-blk")),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            self.storage_list.addTopLevelItem(item)

    def selected_device_index(self, widget: QTreeWidget) -> int | None:
        items = widget.selectedItems()
        if not items:
            return None
        value = items[0].data(0, Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    def add_storage_device(self) -> None:
        dialog = StorageDeviceDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.storage_devices.append(dialog.value())
            self.refresh_storage_devices()

    def create_storage_device(self) -> None:
        qemu_img = shutil.which("qemu-img")
        if qemu_img is None:
            QMessageBox.warning(self, "qemu-img unavailable", "Install QEMU to create virtual disk images.")
            return
        default_path = str(self.cfg_path.parent / f"disk-{len(self.storage_devices) + 2}.qcow2")
        path, _ = QFileDialog.getSaveFileName(self, "Create virtual disk", default_path, "QCOW2 disk (*.qcow2);;Raw disk (*.raw *.img)")
        if not path:
            return
        size_mb, accepted = QInputDialog.getInt(self, "Virtual disk capacity", "Capacity in MB", 16384, 16, 2097152, 16)
        if not accepted:
            return
        image_format = "qcow2" if path.endswith(".qcow2") else "raw"
        result = subprocess.run(
            [qemu_img, "create", "-f", image_format, path, f"{size_mb}M"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            QMessageBox.warning(self, "Disk creation failed", (result.stderr or result.stdout).strip())
            return
        self.storage_devices.append({
            "name": f"Hard Disk {len(self.storage_devices) + 2}",
            "path": path,
            "format": image_format,
            "controller": "virtio-blk",
            "cache": "none",
            "aio": "native",
            "discard": "unmap",
            "read_only": False,
        })
        self.refresh_storage_devices()

    def edit_storage_device(self) -> None:
        index = self.selected_device_index(self.storage_list)
        if index is None:
            return
        dialog = StorageDeviceDialog(self.storage_devices[index], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.storage_devices[index] = dialog.value()
            self.refresh_storage_devices()

    def remove_storage_device(self) -> None:
        index = self.selected_device_index(self.storage_list)
        if index is not None:
            del self.storage_devices[index]
            self.refresh_storage_devices()

    def build_network_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 24, 30, 24)
        self.device_page_header(layout, "Network Adapters", "Add multiple NAT, TAP, bridge, macvtap, or socket-backed adapters.")
        toolbar = QHBoxLayout()
        toolbar.addStretch(1)
        add = QPushButton("Add Adapter…")
        add.clicked.connect(self.add_network_device)
        edit = QPushButton("Edit…")
        edit.clicked.connect(self.edit_network_device)
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_network_device)
        for button in (add, edit, remove):
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.network_list = QTreeWidget()
        self.network_list.setHeaderLabels(["ADAPTER", "TYPE", "MODEL", "CONNECTION"])
        self.network_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.network_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.network_list.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.network_list.itemDoubleClicked.connect(lambda _item, _column: self.edit_network_device())
        layout.addWidget(self.network_list, 1)
        self.refresh_network_devices()
        return page

    def refresh_network_devices(self) -> None:
        self.network_list.clear()
        for index, device in enumerate(self.network_devices):
            item = QTreeWidgetItem([
                f"Network Adapter {index + 1}",
                str(device.get("type", "user")),
                str(device.get("model", "—")),
                "Connected" if bool(device.get("connected", True)) and device.get("type") != "none" else "Disconnected",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            self.network_list.addTopLevelItem(item)

    def add_network_device(self) -> None:
        dialog = NetworkDeviceDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.network_devices.append(dialog.value())
            self.refresh_network_devices()

    def edit_network_device(self) -> None:
        index = self.selected_device_index(self.network_list)
        if index is None:
            return
        dialog = NetworkDeviceDialog(self.network_devices[index], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.network_devices[index] = dialog.value()
            self.refresh_network_devices()

    def remove_network_device(self) -> None:
        index = self.selected_device_index(self.network_list)
        if index is not None:
            del self.network_devices[index]
            self.refresh_network_devices()

    def build_usb_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 24, 30, 24)
        self.device_page_header(layout, "USB Controller", "Configure integrated input and host USB passthrough devices.")
        form = QFormLayout()
        form.addRow("USB and input mode", self.usb)
        form.addRow("QEMU defaults", self.nodefaults)
        layout.addLayout(form)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Host USB passthrough"))
        toolbar.addStretch(1)
        add = QPushButton("Add Device…")
        add.clicked.connect(self.add_usb_device)
        edit = QPushButton("Edit…")
        edit.clicked.connect(self.edit_usb_device)
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_usb_device)
        for button in (add, edit, remove):
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.usb_list = QTreeWidget()
        self.usb_list.setHeaderLabels(["DEVICE", "VENDOR ID", "PRODUCT ID", "SERIAL"])
        self.usb_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.usb_list.itemDoubleClicked.connect(lambda _item, _column: self.edit_usb_device())
        layout.addWidget(self.usb_list, 1)
        self.refresh_usb_devices()
        return page

    def refresh_usb_devices(self) -> None:
        self.usb_list.clear()
        for index, device in enumerate(self.usb_devices):
            item = QTreeWidgetItem([
                str(device.get("name", "USB Device")),
                str(device.get("vendor_id", "")),
                str(device.get("product_id", "")),
                str(device.get("serial", "—")),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            self.usb_list.addTopLevelItem(item)

    def add_usb_device(self) -> None:
        dialog = UsbDeviceDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.usb_devices.append(dialog.value())
            self.refresh_usb_devices()

    def edit_usb_device(self) -> None:
        index = self.selected_device_index(self.usb_list)
        if index is None:
            return
        dialog = UsbDeviceDialog(self.usb_devices[index], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.usb_devices[index] = dialog.value()
            self.refresh_usb_devices()

    def remove_usb_device(self) -> None:
        index = self.selected_device_index(self.usb_list)
        if index is not None:
            del self.usb_devices[index]
            self.refresh_usb_devices()

    def build_shared_folders_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 24, 30, 24)
        self.device_page_header(layout, "Shared Folders", "Expose host directories through virtio-9p and mount them with EdgeOS Workstation Guest Tools.")
        toolbar = QHBoxLayout()
        toolbar.addStretch(1)
        add = QPushButton("Add Share…")
        add.clicked.connect(self.add_shared_folder)
        edit = QPushButton("Edit…")
        edit.clicked.connect(self.edit_shared_folder)
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_shared_folder)
        for button in (add, edit, remove):
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.shared_folder_list = QTreeWidget()
        self.shared_folder_list.setHeaderLabels(["NAME", "HOST FOLDER", "GUEST MOUNT", "ACCESS"])
        self.shared_folder_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.shared_folder_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.shared_folder_list.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.shared_folder_list.itemDoubleClicked.connect(lambda _item, _column: self.edit_shared_folder())
        layout.addWidget(self.shared_folder_list, 1)
        self.refresh_shared_folders()
        return page

    def refresh_shared_folders(self) -> None:
        self.shared_folder_list.clear()
        for index, folder in enumerate(self.shared_folders):
            item = QTreeWidgetItem([
                str(folder.get("tag", "shared")),
                str(folder.get("path", "")),
                str(folder.get("mount_path", f"/mnt/hgfs/{folder.get('tag', 'shared')}")),
                "Read-only" if bool(folder.get("read_only", False)) else "Read/write",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            self.shared_folder_list.addTopLevelItem(item)

    def add_shared_folder(self) -> None:
        dialog = SharedFolderDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.shared_folders.append(dialog.value())
            self.refresh_shared_folders()

    def edit_shared_folder(self) -> None:
        index = self.selected_device_index(self.shared_folder_list)
        if index is None:
            return
        dialog = SharedFolderDialog(self.shared_folders[index], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.shared_folders[index] = dialog.value()
            self.refresh_shared_folders()

    def remove_shared_folder(self) -> None:
        index = self.selected_device_index(self.shared_folder_list)
        if index is not None:
            del self.shared_folders[index]
            self.refresh_shared_folders()

    def update_cpu_total(self) -> None:
        total = self.cpu_sockets.value() * self.cpu_cores.value() * self.cpu_threads.value()
        self.cpu_total.setText(f"{total} virtual CPU{'s' if total != 1 else ''}")

    def save(self) -> None:
        self.cfg["cpu_sockets"] = int(self.cpu_sockets.value())
        self.cfg["cpu_cores"] = int(self.cpu_cores.value())
        self.cfg["cpu_threads"] = int(self.cpu_threads.value())
        self.cfg["cpus"] = self.cfg["cpu_sockets"] * self.cfg["cpu_cores"] * self.cfg["cpu_threads"]
        self.cfg["build_jobs"] = int(self.build_jobs.value())
        self.cfg["architecture"] = self.architecture.currentText()
        self.cfg["description"] = self.description.text().strip()
        self.cfg["hostname"] = self.hostname.text().strip()
        self.cfg["memory"] = self.memory.currentText().strip()
        self.cfg["accelerator"] = self.accelerator.currentText()
        self.cfg["kvm"] = self.accelerator.currentText() in ("auto", "kvm")
        self.cfg["machine"] = self.machine.text().strip() or "pc,accel=kvm"
        self.cfg["cpu_model"] = self.cpu_model.text().strip() or "host,migratable=off"
        self.cfg["boot_params"] = [x for x in self.boot_params.text().split() if x]
        self.cfg["desktop"] = str(self.boot_experience.currentData())
        self.cfg["gpu"] = self.gpu.currentText().strip() or "std"
        self.cfg["virgl"] = self.virgl.isChecked()
        self.cfg["display_backend"] = self.display_backend.currentText().strip() or "default"
        self.cfg["usb"] = self.usb.currentText().strip() or "virtio-input"
        self.cfg["nodefaults"] = self.nodefaults.isChecked()
        self.cfg["disk_controller"] = self.disk_controller.currentText().strip() or "nvme"
        self.cfg["disk_cache"] = self.disk_cache.currentText().strip() or "none"
        self.cfg["disk_aio"] = self.disk_aio.currentText().strip() or "native"
        self.cfg["disk_discard"] = self.disk_discard.currentText()
        self.cfg["disk_read_only"] = self.disk_read_only.isChecked()
        self.cfg["sound"] = self.sound.currentText()
        self.cfg["balloon"] = self.balloon.isChecked()
        self.cfg["rng"] = self.rng.isChecked()
        self.cfg["firmware_mode"] = self.firmware_mode.currentText()
        self.cfg["firmware"] = self.firmware.text().strip()
        self.cfg["boot_order"] = str(self.boot_order.currentData())
        self.cfg["boot_menu"] = self.boot_menu.isChecked()
        self.cfg["boot_delay_ms"] = int(self.boot_delay.value())
        self.cfg["rtc_base"] = self.rtc_base.currentText()
        self.cfg["cdrom_connected"] = self.cdrom_connected.isChecked()
        self.cfg["storage"] = [dict(item) for item in self.storage_devices]
        self.cfg["networks"] = [dict(item) for item in self.network_devices]
        self.cfg["usb_devices"] = [dict(item) for item in self.usb_devices]
        self.cfg["shared_folders"] = [dict(item) for item in self.shared_folders]
        try:
            self.cfg["qemu_args"] = shlex.split(self.qemu_args.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid QEMU arguments", str(exc))
            return
        try:
            save_config(self.cfg_path, self.cfg)
        except (OSError, ConfigError) as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return
        self.accept()


class MainWindow(QMainWindow):
    vnc_share_error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EdgeOS Workstation")
        self.setMinimumSize(980, 680)
        self.resize(1380, 860)
        self.settings = QSettings("EdgeOS", "Workstation")
        self.dark_mode = self.settings.value("darkMode", False, type=bool)
        self.favorites = set(self.settings.value("favorites", [], type=list))
        self.language = str(self.settings.value("language", "system"))
        self.translator = QTranslator(self)
        self.install_language_pack()
        self.process: QProcess | None = None
        self.current_cli_command: str | None = None
        self.current_cli_vm_name: str | None = None
        self.task_journal = TaskJournal(TASK_JOURNAL_PATH)
        self.recovered_tasks = self.task_journal.recover_interrupted()
        self.active_task_id: str | None = None
        self.task_cancel_requested = False
        self.task_title = ""
        self.task_started_monotonic: float | None = None
        self.task_last_output_monotonic: float | None = None
        self.pending_task_output: list[str] = []
        self.task_output_timer = QTimer(self)
        self.task_output_timer.setSingleShot(True)
        self.task_output_timer.setInterval(100)
        self.task_output_timer.timeout.connect(self.flush_task_output)
        self.task_status_timer = QTimer(self)
        self.task_status_timer.setInterval(1000)
        self.task_status_timer.timeout.connect(self.update_task_status)
        self.closing = False
        self.task_dialog: TaskCenterDialog | None = None
        self.serial_fd: int | None = None
        self.serial_connected_name: str | None = None
        self.serial_is_socket = False
        self.serial_read_notifier: QSocketNotifier | None = None
        self.serial_tail_name: str | None = None
        self.serial_tail_pos = 0
        self.pending_serial_name: str | None = None
        self.pending_vnc_port: int | None = None
        self.display_connected_name: str | None = None
        self.display_connected_port: int | None = None
        self.display_views: dict[str, RfbView] = {}
        self.display_ports: dict[str, int] = {}
        self.display_dimensions: dict[str, tuple[int, int]] = {}
        self.display_scale_percent: dict[str, int] = {}
        self.pending_display_resolutions: dict[str, str] = {}
        self.vnc_share_server: VncShareServer | None = None
        self.vnc_share_name: str | None = None
        self.qmp_watchers: dict[str, QmpWatcher] = {}
        self.qmp_states: dict[str, str] = {}
        self.display_maximized = False
        self.display_fullscreen = False
        self.authoritative_display_name: str | None = None
        self.authoritative_display_persistent = False
        self.serial_timer = QTimer(self)
        self.serial_timer.setInterval(500)
        self.serial_timer.timeout.connect(self.poll_serial_log)
        self.serial_timer.start()
        self.runtime_timer = QTimer(self)
        self.runtime_timer.setInterval(2500)
        self.runtime_timer.timeout.connect(self.refresh_runtime_state)
        self.runtime_timer.start()
        self.authoritative_display_timer = QTimer(self)
        self.authoritative_display_timer.setInterval(200)
        self.authoritative_display_timer.timeout.connect(
            self.capture_authoritative_display_frame
        )
        self.vnc_share_error.connect(self.handle_vnc_share_error)
        self.log = AnsiTerminal()
        self.log.setObjectName("Console")
        console_font = QFont("Menlo")
        console_font.setStyleHint(QFont.StyleHint.Monospace)
        console_font.setPointSize(11)
        self.log.setFont(console_font)
        self.serial_input = QLineEdit()
        self.serial_input.setPlaceholderText("Send input to the selected VM serial console")
        self.serial_input.setEnabled(False)
        self.serial_send_btn = QPushButton("Send")
        self.serial_send_btn.setEnabled(False)
        self.serial_input.returnPressed.connect(self.send_serial_input)
        self.serial_send_btn.clicked.connect(self.send_serial_input)

        self.refresh_btn = QPushButton("Refresh")
        self.create_btn = QPushButton("Create VM")
        self.start_btn = QPushButton("Start Window")
        self.start_headless_btn = QPushButton("Start Headless")
        self.start_no_kvm_btn = QPushButton("Start Window TCG")
        self.start_xfce_btn = QPushButton("Start Xfce4")
        self.stop_btn = QPushButton("Stop")
        self.update_btn = QPushButton("Update Kernel")
        self.snapshot_btn = QPushButton("Snapshot")
        self.clone_btn = QPushButton("Clone")
        self.config_btn = QPushButton("Config")
        self.delete_btn = QPushButton("Delete")
        self.tap_btn = QPushButton("Setup TAP")
        self.macvtap_btn = QPushButton("Setup Macvtap")

        self.refresh_btn.clicked.connect(self.refresh)
        self.create_btn.clicked.connect(self.create_vm)
        self.start_btn.clicked.connect(lambda: self.start_vm(force_tcg=False, window=True))
        self.start_headless_btn.clicked.connect(lambda: self.start_vm(force_tcg=False, window=False))
        self.start_no_kvm_btn.clicked.connect(lambda: self.start_vm(force_tcg=True, window=True))
        self.start_xfce_btn.clicked.connect(self.start_xfce4)
        self.stop_btn.clicked.connect(self.stop_vm)
        self.update_btn.clicked.connect(self.update_kernel)
        self.snapshot_btn.clicked.connect(self.snapshot_vm)
        self.clone_btn.clicked.connect(self.clone_vm)
        self.config_btn.clicked.connect(self.show_config)
        self.delete_btn.clicked.connect(self.delete_vm)
        self.tap_btn.clicked.connect(self.setup_tap)
        self.macvtap_btn.clicked.connect(self.setup_macvtap)

        self._create_actions()
        self._create_menu_bar()
        self._create_tool_bar()

        self.library = QTreeWidget()
        self.library.setObjectName("Library")
        self.library.setHeaderLabels(["VIRTUAL MACHINE", "STATE"])
        self.library.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.library.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.library.setRootIsDecorated(False)
        self.library.setAlternatingRowColors(False)
        self.library.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.library.itemSelectionChanged.connect(self.update_actions)
        self.library.itemDoubleClicked.connect(lambda _item, _column: self.open_or_start_console())
        self.library.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.library.customContextMenuRequested.connect(self.show_library_menu)

        library_panel = QWidget()
        library_panel.setObjectName("LibraryPanel")
        library_layout = QVBoxLayout(library_panel)
        library_layout.setContentsMargins(12, 12, 12, 12)
        library_layout.setSpacing(9)
        library_title = QLabel("LIBRARY")
        library_title.setObjectName("SectionTitle")
        library_layout.addWidget(library_title)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter virtual machines")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_library)
        library_layout.addWidget(self.search)
        self.library_filter = QComboBox()
        self.library_filter.addItems(["All Virtual Machines", "Running", "Powered Off", "Suspended", "Favorites"])
        self.library_filter.currentIndexChanged.connect(lambda _index: self.filter_library(self.search.text()))
        library_layout.addWidget(self.library_filter)
        library_layout.addWidget(self.library, 1)
        add_vm = QPushButton("＋  Create a New Virtual Machine")
        add_vm.setObjectName("CreateButton")
        add_vm.clicked.connect(self.create_vm)
        library_layout.addWidget(add_vm)

        self.workspace = QStackedWidget()
        self.workspace.addWidget(self._build_welcome_page())
        self.workspace.addWidget(self._build_vm_page())
        self.vm_tabs = QTabBar()
        self.vm_tabs.setObjectName("VmTabs")
        self.vm_tabs.setMovable(True)
        self.vm_tabs.setTabsClosable(True)
        self.vm_tabs.setExpanding(False)
        self.vm_tab_style = LeftAlignedTabStyle()
        self.vm_tab_style.setParent(self.vm_tabs)
        self.vm_tabs.setStyle(self.vm_tab_style)
        self.vm_tabs.currentChanged.connect(self.vm_tab_changed)
        self.vm_tabs.tabCloseRequested.connect(self.close_vm_tab)
        self.vm_tabs.setUsesScrollButtons(True)
        self.vm_tabs_container = QWidget()
        self.vm_tabs_container.setObjectName("VmTabsContainer")
        vm_tabs_layout = QHBoxLayout(self.vm_tabs_container)
        vm_tabs_layout.setContentsMargins(0, 0, 0, 0)
        vm_tabs_layout.setSpacing(0)
        vm_tabs_layout.addWidget(self.vm_tabs)
        self.vm_tabs_container.hide()
        workspace_shell = QWidget()
        workspace_shell_layout = QVBoxLayout(workspace_shell)
        workspace_shell_layout.setContentsMargins(0, 0, 0, 0)
        workspace_shell_layout.setSpacing(0)
        workspace_shell_layout.addWidget(self.vm_tabs_container)
        workspace_shell_layout.addWidget(self.workspace, 1)

        console_box = QWidget()
        console_layout = QVBoxLayout(console_box)
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_layout.setSpacing(0)
        console_layout.addWidget(self.log)
        input_row = QHBoxLayout()
        input_row.setContentsMargins(8, 7, 8, 7)
        input_row.addWidget(self.serial_input)
        input_row.addWidget(self.serial_send_btn)
        console_layout.addLayout(input_row)

        self.console_tabs = QTabWidget()
        self.console_tabs.setObjectName("ConsoleTabs")
        self.console_tabs.addTab(console_box, "Serial Console")

        self.qemu_log = QPlainTextEdit()
        self.qemu_log.setObjectName("Console")
        self.qemu_log.setReadOnly(True)
        self.qemu_log.setFont(console_font)
        self.console_tabs.addTab(self.qemu_log, "QEMU Log")

        snapshots_page = QWidget()
        snapshots_layout = QVBoxLayout(snapshots_page)
        snapshots_layout.setContentsMargins(8, 8, 8, 8)
        snapshots_toolbar = QHBoxLayout()
        snapshots_toolbar.addWidget(QLabel("Disk snapshots for the selected virtual machine"))
        snapshots_toolbar.addStretch(1)
        take_snapshot = QPushButton("Take Snapshot…")
        take_snapshot.clicked.connect(self.snapshot_vm)
        snapshots_toolbar.addWidget(take_snapshot)
        restore_snapshot = QPushButton("Restore")
        restore_snapshot.clicked.connect(self.restore_snapshot)
        snapshots_toolbar.addWidget(restore_snapshot)
        delete_snapshot = QPushButton("Delete")
        delete_snapshot.clicked.connect(self.delete_snapshot)
        snapshots_toolbar.addWidget(delete_snapshot)
        snapshots_layout.addLayout(snapshots_toolbar)
        self.snapshots = QTreeWidget()
        self.snapshots.setHeaderLabels(["NAME", "DESCRIPTION", "DISKS", "SIZE", "CREATED"])
        self.snapshots.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.snapshots.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4):
            self.snapshots.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.snapshots.itemDoubleClicked.connect(lambda _item, _column: self.restore_snapshot())
        snapshots_layout.addWidget(self.snapshots)
        self.console_tabs.addTab(snapshots_page, "Snapshots")

        transfer_page = QWidget()
        transfer_layout = QVBoxLayout(transfer_page)
        transfer_layout.setContentsMargins(18, 16, 18, 16)
        transfer_title = QLabel("File Transfer")
        transfer_title.setObjectName("CardTitle")
        transfer_layout.addWidget(transfer_title)
        transfer_help = QLabel("Copy files over the supervised serial channel. The guest must be running with a shell available on its serial console.")
        transfer_help.setWordWrap(True)
        transfer_layout.addWidget(transfer_help)
        transfer_form = QFormLayout()
        self.transfer_host_path = QLineEdit()
        browse_host = QPushButton("Browse…")
        browse_host.clicked.connect(self.choose_transfer_host_file)
        host_row = QWidget()
        host_row_layout = QHBoxLayout(host_row)
        host_row_layout.setContentsMargins(0, 0, 0, 0)
        host_row_layout.addWidget(self.transfer_host_path, 1)
        host_row_layout.addWidget(browse_host)
        self.transfer_guest_path = QLineEdit("/tmp/")
        transfer_form.addRow("Host path", host_row)
        transfer_form.addRow("Guest path", self.transfer_guest_path)
        transfer_layout.addLayout(transfer_form)
        transfer_buttons = QHBoxLayout()
        transfer_buttons.addStretch(1)
        push_button = QPushButton("Send to Guest")
        push_button.clicked.connect(self.push_file_to_guest)
        pull_button = QPushButton("Receive from Guest")
        pull_button.clicked.connect(self.pull_file_from_guest)
        transfer_buttons.addWidget(push_button)
        transfer_buttons.addWidget(pull_button)
        transfer_layout.addLayout(transfer_buttons)
        transfer_layout.addStretch(1)
        self.console_tabs.addTab(transfer_page, "File Transfer")

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.setObjectName("WorkspaceSplitter")
        right_splitter.addWidget(workspace_shell)
        right_splitter.addWidget(self.console_tabs)
        right_splitter.setSizes([570, 240])
        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 1)

        self.library_panel = library_panel
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("MainSplitter")
        self.main_splitter.addWidget(library_panel)
        self.main_splitter.addWidget(right_splitter)
        self.main_splitter.setSizes([320, 1060])
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.setCentralWidget(self.main_splitter)

        self.status_name = QLabel("No virtual machine selected")
        self.statusBar().addWidget(self.status_name, 1)
        self.task_progress = QProgressBar()
        self.task_progress.setRange(0, 0)
        self.task_progress.setFixedWidth(110)
        self.task_progress.setTextVisible(False)
        self.task_progress.hide()
        self.statusBar().addPermanentWidget(self.task_progress)
        self.status_task = QLabel("Ready")
        self.statusBar().addPermanentWidget(self.status_task)
        self.status_tasks_button = QPushButton("Tasks")
        self.status_tasks_button.setFlat(True)
        self.status_tasks_button.clicked.connect(self.show_task_center)
        self.statusBar().addPermanentWidget(self.status_tasks_button)
        self.library.setAccessibleName("Virtual machine library")
        self.library.setAccessibleDescription("Select, search, and manage virtual machines")
        self.search.setAccessibleName("Search virtual machines")
        self.library_filter.setAccessibleName("Virtual machine state filter")
        self.rfb_view.setAccessibleName("Virtual machine display")
        self.serial_input.setAccessibleName("Serial console input")
        self.serial_send_btn.setAccessibleName("Send serial console input")
        self.status_tasks_button.setAccessibleDescription("Open persistent operation history and task output")
        self._apply_style()
        self.translate_widget_tree()

        self.refresh()
        self.update_actions()
        if self.recovered_tasks:
            self.status_task.setText(f"{len(self.recovered_tasks)} interrupted task(s)")
            QTimer.singleShot(0, self.show_task_center)

    def standard_icon(self, icon: QStyle.StandardPixmap):
        return self.style().standardIcon(icon)

    def _create_actions(self) -> None:
        self.new_action = QAction(self.standard_icon(QStyle.StandardPixmap.SP_FileDialogNewFolder), "New Virtual Machine…", self)
        self.new_action.setShortcut(QKeySequence.StandardKey.New)
        self.new_action.triggered.connect(self.create_vm)
        self.refresh_action = QAction(self.standard_icon(QStyle.StandardPixmap.SP_BrowserReload), "Refresh", self)
        self.refresh_action.setShortcut(QKeySequence.StandardKey.Refresh)
        self.refresh_action.triggered.connect(self.refresh)
        self.start_action = QAction(self.standard_icon(QStyle.StandardPixmap.SP_MediaPlay), "Power On", self)
        self.start_action.setShortcut("Ctrl+B")
        self.start_action.triggered.connect(self.open_or_start_console)
        self.stop_action = QAction(self.standard_icon(QStyle.StandardPixmap.SP_MediaStop), "Power Off", self)
        self.stop_action.setShortcut("Ctrl+E")
        self.stop_action.triggered.connect(self.shutdown_vm)
        self.force_stop_action = QAction("Force Power Off", self)
        self.force_stop_action.triggered.connect(self.stop_vm)
        self.reset_action = QAction("Reset", self)
        self.reset_action.triggered.connect(self.reset_vm)
        self.pause_action = QAction("Pause", self)
        self.pause_action.setShortcut("Ctrl+Shift+P")
        self.pause_action.triggered.connect(self.pause_vm)
        self.resume_action = QAction("Resume", self)
        self.resume_action.setShortcut("Ctrl+Shift+R")
        self.resume_action.triggered.connect(self.resume_vm)
        self.suspend_action = QAction("Suspend", self)
        self.suspend_action.setShortcut("Ctrl+Shift+S")
        self.suspend_action.triggered.connect(self.suspend_vm)
        self.install_tools_action = QAction("Install Guest Tools…", self)
        self.install_tools_action.triggered.connect(self.install_guest_tools)
        self.settings_action = QAction(self.standard_icon(QStyle.StandardPixmap.SP_FileDialogDetailedView), "Virtual Machine Settings", self)
        self.settings_action.setShortcut("Ctrl+D")
        self.settings_action.triggered.connect(self.show_config)
        self.maximize_display_action = QAction("Maximize Display", self)
        self.maximize_display_action.setShortcut("Ctrl+Shift+M")
        self.maximize_display_action.setCheckable(True)
        self.maximize_display_action.triggered.connect(self.set_display_maximized)
        self.fullscreen_action = QAction("Full Screen", self)
        self.fullscreen_action.setShortcut("Ctrl+Alt+Return")
        self.fullscreen_action.setCheckable(True)
        self.fullscreen_action.triggered.connect(self.toggle_fullscreen)
        self.exit_fullscreen_action = QAction("Exit Full Screen", self)
        self.exit_fullscreen_action.setShortcut("Esc")
        self.exit_fullscreen_action.triggered.connect(lambda: self.toggle_fullscreen(False))
        self.addAction(self.exit_fullscreen_action)
        self.about_action = QAction("About EdgeOS Workstation", self)
        self.about_action.triggered.connect(self.show_about)
        self.tasks_action = QAction("Task Center", self)
        self.tasks_action.setShortcut("Ctrl+Shift+T")
        self.tasks_action.triggered.connect(self.show_task_center)
        self.dark_mode_action = QAction("Dark Mode", self)
        self.dark_mode_action.setCheckable(True)
        self.dark_mode_action.setChecked(self.dark_mode)
        self.dark_mode_action.triggered.connect(self.set_dark_mode)
        self.web_console_action = QAction("Open Web Console", self)
        self.web_console_action.triggered.connect(self.open_web_console)
        self.quit_action = QAction("Quit", self)
        self.quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.quit_action.triggered.connect(QApplication.instance().quit)

    def _create_menu_bar(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(self.new_action)
        file_menu.addAction(self.refresh_action)
        file_menu.addSeparator()
        file_menu.addAction(self.quit_action)

        vm_menu = self.menuBar().addMenu("Virtual Machine")
        vm_menu.addAction(self.start_action)
        vm_menu.addAction(self.stop_action)
        vm_menu.addAction(self.force_stop_action)
        vm_menu.addAction(self.reset_action)
        vm_menu.addSeparator()
        vm_menu.addAction(self.pause_action)
        vm_menu.addAction(self.resume_action)
        vm_menu.addAction(self.suspend_action)
        vm_menu.addSeparator()
        vm_menu.addAction("Start Headless", lambda: self.start_vm(False, False))
        vm_menu.addAction("Start with TCG in Embedded Console", lambda: self.start_vm(True, True))
        vm_menu.addAction("Start in Separate Window", lambda: self.start_vm(False, True, embedded=False))
        vm_menu.addAction("Start Xfce Desktop", self.start_xfce4)
        vm_menu.addSeparator()
        vm_menu.addAction(self.install_tools_action)
        vm_menu.addSeparator()
        vm_menu.addAction(self.settings_action)
        vm_menu.addAction("Update Kernel", self.update_kernel)
        vm_menu.addAction("Take Snapshot…", self.snapshot_vm)
        vm_menu.addAction("Clone…", self.clone_vm)
        vm_menu.addSeparator()
        vm_menu.addAction("Delete from Disk…", self.delete_vm)

        network_menu = self.menuBar().addMenu("Network")
        network_menu.addAction("Configure TAP…", self.setup_tap)
        network_menu.addAction("Configure Macvtap…", self.setup_macvtap)

        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(self.maximize_display_action)
        view_menu.addAction(self.fullscreen_action)
        view_menu.addSeparator()
        view_menu.addAction(self.tasks_action)
        view_menu.addAction(self.dark_mode_action)
        view_menu.addAction(self.web_console_action)
        language_menu = view_menu.addMenu("Language")
        language_menu.addAction("System Default", lambda: self.set_language("system"))
        language_menu.addAction("English", lambda: self.set_language("en"))
        for catalog in sorted(TRANSLATIONS_DIR.glob("edgeos_workstation_*.qm")) if TRANSLATIONS_DIR.is_dir() else []:
            locale_name = catalog.stem.removeprefix("edgeos_workstation_")
            label = QLocale(locale_name).nativeLanguageName() or locale_name
            language_menu.addAction(label, lambda checked=False, value=locale_name: self.set_language(value))
        help_menu = self.menuBar().addMenu("Help")
        help_menu.addAction(self.about_action)

    def _create_tool_bar(self) -> None:
        self.main_toolbar = QToolBar("Main Toolbar", self)
        self.main_toolbar.setObjectName("MainToolbar")
        self.main_toolbar.setIconSize(QSize(20, 20))
        self.main_toolbar.setMovable(False)
        self.main_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.main_toolbar.addAction(self.new_action)
        self.main_toolbar.addSeparator()
        self.main_toolbar.addAction(self.start_action)
        self.main_toolbar.addAction(self.stop_action)
        self.main_toolbar.addAction(self.settings_action)
        self.main_toolbar.addSeparator()
        self.main_toolbar.addAction(self.refresh_action)
        self.main_toolbar.addAction(self.tasks_action)
        self.addToolBar(self.main_toolbar)

    def _build_welcome_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("WelcomePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(64, 54, 64, 54)
        layout.addStretch(1)
        product = QLabel("EdgeOS")
        product.setObjectName("WelcomeProduct")
        layout.addWidget(product)
        title = QLabel("Virtual machines, organized for development")
        title.setObjectName("WelcomeTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        subtitle = QLabel("Create a persistent EdgeOS or Alpine machine, then build, boot, inspect, and snapshot it from one workspace.")
        subtitle.setObjectName("WelcomeSubtitle")
        subtitle.setWordWrap(True)
        subtitle.setMaximumWidth(720)
        layout.addWidget(subtitle)
        layout.addSpacing(24)
        create = QPushButton("Create a New Virtual Machine")
        create.setObjectName("PrimaryButton")
        create.setMinimumWidth(240)
        create.clicked.connect(self.create_vm)
        layout.addWidget(create, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(2)
        return page

    def _create_display_tool_button(
        self,
        label: str,
        icon: QStyle.StandardPixmap,
        callback,
    ) -> QToolButton:
        """Create a compact display control with a persistent hover label."""
        button = QToolButton()
        button.setObjectName("DisplayToolButton")
        button.setIcon(self.standard_icon(icon))
        button.setIconSize(QSize(17, 17))
        button.setFixedSize(34, 32)
        button.setToolTip(label)
        button.setStatusTip(label)
        button.setAccessibleName(label)
        button.clicked.connect(lambda _checked=False: callback())
        return button

    def _build_vm_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("VmPage")
        layout = QVBoxLayout(page)
        self.vm_page_layout = layout
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(18)

        self.vm_header = QWidget()
        header = QHBoxLayout(self.vm_header)
        header.setContentsMargins(0, 0, 0, 0)
        title_column = QVBoxLayout()
        title_column.setSpacing(3)
        self.vm_title = QLabel("Virtual Machine")
        self.vm_title.setObjectName("VmTitle")
        self.vm_subtitle = QLabel("Powered off")
        self.vm_subtitle.setObjectName("VmSubtitle")
        title_column.addWidget(self.vm_title)
        title_column.addWidget(self.vm_subtitle)
        header.addLayout(title_column, 1)
        self.header_start = QPushButton("▶  Power On")
        self.header_start.setObjectName("PrimaryButton")
        self.header_start.clicked.connect(self.open_or_start_console)
        self.header_settings = QPushButton("Edit Settings")
        self.header_settings.clicked.connect(self.show_config)
        header.addWidget(self.header_start)
        header.addWidget(self.header_settings)
        layout.addWidget(self.vm_header)

        self.vm_divider = QFrame()
        self.vm_divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(self.vm_divider)

        content = QHBoxLayout()
        content.setSpacing(28)
        preview = QFrame()
        self.display_preview = preview
        preview.setObjectName("DisplayPreview")
        preview.setMinimumSize(400, 250)
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)
        display_toolbar = QWidget()
        display_toolbar.setObjectName("DisplayToolbar")
        display_toolbar_layout = QHBoxLayout(display_toolbar)
        display_toolbar_layout.setContentsMargins(10, 6, 8, 6)
        self.display_state = QLabel("DISPLAY DISCONNECTED")
        self.display_state.setObjectName("DisplayState")
        display_toolbar_layout.addWidget(self.display_state)
        display_toolbar_layout.addStretch(1)
        self.desktop_btn = self._create_display_tool_button(
            "Start Desktop",
            QStyle.StandardPixmap.SP_ComputerIcon,
            self.start_xfce4,
        )
        display_toolbar_layout.addWidget(self.desktop_btn)
        self.display_scaling = QComboBox()
        self.display_scaling.setObjectName("DisplayScaling")
        self.display_scaling.addItem("Fit (Clear)", "fit")
        self.display_scaling.addItem("Integer Fit", "integer")
        self.display_scaling.addItem("100% Pixels", "actual")
        self.display_scaling.addItem("Stretch", "stretch")
        self.display_scaling.setFixedWidth(116)
        self.display_scaling.setToolTip(
            "Fit uses high-quality downscaling. Integer Fit avoids fractional "
            "sampling. 100% Pixels is the sharpest option."
        )
        self.display_scaling.setAccessibleName("Display scaling")
        self.display_scaling.currentIndexChanged.connect(self.change_display_scaling)
        display_toolbar_layout.addWidget(self.display_scaling)
        self.display_resolution = QComboBox()
        self.display_resolution.setObjectName("DisplayResolution")
        for resolution in SUPPORTED_DISPLAY_RESOLUTIONS:
            self.display_resolution.addItem(resolution.replace("x", "×"), resolution)
        self.display_resolution.addItem("Custom…", "custom")
        self.display_resolution.setFixedWidth(120)
        self.display_resolution.setToolTip("Guest resolution for the next VM start")
        self.display_resolution.setAccessibleName("Guest display resolution")
        self.display_resolution.currentIndexChanged.connect(
            self.display_resolution_selected
        )
        display_toolbar_layout.addWidget(self.display_resolution)
        self.apply_resolution_btn = self._create_display_tool_button(
            "Apply Resolution on Next Start",
            QStyle.StandardPixmap.SP_BrowserReload,
            self.apply_display_resolution,
        )
        display_toolbar_layout.addWidget(self.apply_resolution_btn)
        screenshot = self._create_display_tool_button(
            "Take Screenshot",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            self.save_display_screenshot,
        )
        display_toolbar_layout.addWidget(screenshot)
        clipboard = self._create_display_tool_button(
            "Send Host Clipboard",
            QStyle.StandardPixmap.SP_FileDialogContentsView,
            self.send_host_clipboard,
        )
        display_toolbar_layout.addWidget(clipboard)
        self.vnc_share_btn = self._create_display_tool_button(
            "Share VNC on Local Network",
            QStyle.StandardPixmap.SP_DriveNetIcon,
            self.toggle_vnc_share,
        )
        self.vnc_share_btn.setCheckable(True)
        display_toolbar_layout.addWidget(self.vnc_share_btn)
        self.display_maximize_btn = self._create_display_tool_button(
            "Maximize Display",
            QStyle.StandardPixmap.SP_TitleBarMaxButton,
            lambda: self.set_display_maximized(not self.display_maximized),
        )
        display_toolbar_layout.addWidget(self.display_maximize_btn)
        send_keys = self._create_display_tool_button(
            "Send Ctrl+Alt+Del",
            QStyle.StandardPixmap.SP_CommandLink,
            self.send_ctrl_alt_delete,
        )
        display_toolbar_layout.addWidget(send_keys)
        self.display_fullscreen_btn = self._create_display_tool_button(
            "Enter Full Screen",
            QStyle.StandardPixmap.SP_TitleBarNormalButton,
            lambda: self.toggle_fullscreen(not self.display_fullscreen),
        )
        display_toolbar_layout.addWidget(self.display_fullscreen_btn)
        preview_layout.addWidget(display_toolbar)
        self.display_stack = QStackedWidget()
        self.rfb_view = RfbView()
        self.rfb_view.setObjectName("RfbView")
        self.display_stack.addWidget(self.rfb_view)
        preview_layout.addWidget(self.display_stack, 1)
        content.addWidget(preview, 3)

        summary = QFrame()
        self.summary_card = summary
        summary.setObjectName("SummaryCard")
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(22, 20, 22, 20)
        summary_layout.setSpacing(10)
        summary_title = QLabel("Hardware")
        summary_title.setObjectName("CardTitle")
        summary_layout.addWidget(summary_title)
        self.detail_labels: dict[str, QLabel] = {}
        for key, label in [
            ("architecture", "Architecture"),
            ("cpus", "Processors"),
            ("memory", "Memory"),
            ("disk", "Hard Disk"),
            ("network", "Network Adapter"),
            ("gpu", "Display"),
        ]:
            row = QHBoxLayout()
            row_label = QLabel(label)
            row_label.setObjectName("HardwareLabel")
            value = QLabel("—")
            value.setObjectName("HardwareValue")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(row_label)
            row.addWidget(value, 1)
            summary_layout.addLayout(row)
            self.detail_labels[key] = value
        summary_layout.addStretch(1)
        self.vm_path_label = QLabel()
        self.vm_path_label.setObjectName("VmPath")
        self.vm_path_label.setWordWrap(True)
        summary_layout.addWidget(self.vm_path_label)
        content.addWidget(summary, 2)
        layout.addLayout(content, 1)
        return page

    def _apply_style(self) -> None:
        style = """
            QMainWindow, QWidget#VmPage, QWidget#WelcomePage { background: #f4f6f8; color: #17212b; }
            QMenuBar { background: #ffffff; border-bottom: 1px solid #d7dde3; padding: 2px; }
            QMenuBar::item:selected, QMenu::item:selected { background: #e7f0fb; color: #174f82; }
            QToolBar#MainToolbar { background: #ffffff; border: 0; border-bottom: 1px solid #cfd6dc; spacing: 5px; padding: 7px 10px; }
            QToolBar#MainToolbar QToolButton { background: transparent; color: #17212b; border: 1px solid transparent; padding: 6px 10px; border-radius: 4px; }
            QToolBar#MainToolbar QToolButton:hover { background: #edf2f7; border-color: #d6dee5; }
            QToolBar#MainToolbar QToolButton:pressed, QToolBar#MainToolbar QToolButton:checked { background: #dce8f3; color: #174f82; border-color: #a9c4d9; }
            QToolBar#MainToolbar QToolButton:focus { border-color: #4b91c4; }
            QWidget#VmTabsContainer { background: #e9edf1; border-bottom: 1px solid #c7ced5; }
            QTabBar#VmTabs { background: transparent; }
            QTabBar#VmTabs::tab { background: #e1e6eb; color: #53616d; padding: 7px 16px; border: 0; border-top: 2px solid transparent; border-right: 1px solid #c4ccd3; min-width: 130px; }
            QTabBar#VmTabs::tab:hover { background: #edf2f6; color: #22313d; }
            QTabBar#VmTabs::tab:selected { background: #f4f6f8; color: #155b8c; border-top-color: #2585c4; font-weight: 600; }
            QWidget#LibraryPanel { background: #eef1f4; border-right: 1px solid #cbd2d9; }
            QLabel#SectionTitle { color: #697784; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
            QTreeWidget#Library { background: transparent; border: 0; outline: 0; font-size: 13px; }
            QTreeWidget#Library::item { min-height: 37px; padding: 2px 5px; border-radius: 4px; }
            QTreeWidget#Library::item:selected { background: #256ea8; color: white; }
            QTreeWidget#Library QHeaderView::section { background: transparent; color: #7b8791; border: 0; border-bottom: 1px solid #d1d7dc; padding: 7px 5px; font-size: 10px; font-weight: 600; }
            QLineEdit, QComboBox, QSpinBox { background: #ffffff; border: 1px solid #bfc8d1; border-radius: 4px; padding: 7px; selection-background-color: #256ea8; }
            QPushButton { background: #ffffff; border: 1px solid #aeb9c3; border-radius: 4px; padding: 7px 13px; }
            QPushButton:hover { border-color: #2678b8; background: #f6faff; }
            QPushButton:disabled { color: #9ca6ae; background: #edf0f2; border-color: #d6dce1; }
            QPushButton#PrimaryButton, QPushButton#CreateButton { background: #1f6fa8; color: #ffffff; border-color: #185b8b; font-weight: 600; }
            QPushButton#PrimaryButton:hover, QPushButton#CreateButton:hover { background: #185f93; }
            QLabel#WelcomeProduct { color: #1f6fa8; font-size: 22px; font-weight: 700; }
            QLabel#WelcomeTitle { color: #17212b; font-size: 32px; font-weight: 650; }
            QLabel#WelcomeSubtitle { color: #5e6d79; font-size: 15px; }
            QLabel#VmTitle { color: #17212b; font-size: 27px; font-weight: 650; }
            QLabel#VmSubtitle { color: #667580; font-size: 13px; }
            QFrame#DisplayPreview { background: #18222c; border: 1px solid #0f171e; border-radius: 7px; }
            QWidget#DisplayToolbar { background: #242f38; border-bottom: 1px solid #3a4650; }
            QLabel#DisplayState { color: #aab8c3; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
            QToolButton#DisplayToolButton { color: #dce5eb; background: #303c46; border: 1px solid #4b5964; border-radius: 5px; padding: 5px; }
            QToolButton#DisplayToolButton:hover { background: #3b4a56; border-color: #7193aa; }
            QToolButton#DisplayToolButton:pressed { background: #1d2730; border-color: #8ab0c8; }
            QToolButton#DisplayToolButton:disabled { background: #28323a; color: #71808b; border-color: #3a4650; }
            QComboBox#DisplayScaling { color: #dce5eb; background: #303c46; border-color: #4b5964; padding: 5px 8px; }
            QLabel#PreviewTitle { color: #f5f8fa; font-size: 32px; font-weight: 700; }
            QLabel#PreviewStatus { color: #8fa1af; font-size: 11px; font-weight: 700; letter-spacing: 2px; }
            QFrame#SummaryCard { background: #ffffff; border: 1px solid #d2d9df; border-radius: 7px; }
            QLabel#CardTitle { font-size: 17px; font-weight: 650; padding-bottom: 7px; }
            QLabel#HardwareLabel { color: #60707d; }
            QLabel#HardwareValue { color: #17212b; font-weight: 600; }
            QLabel#VmPath { color: #7b8791; font-size: 11px; border-top: 1px solid #e4e8eb; padding-top: 10px; }
            QTabWidget#ConsoleTabs::pane { border: 0; border-top: 1px solid #adb7c0; }
            QTabWidget#ConsoleTabs QTabBar::tab { background: #e1e6ea; padding: 7px 18px; border-right: 1px solid #c5cdd3; }
            QTabWidget#ConsoleTabs QTabBar::tab:selected { background: #222b33; color: #ffffff; }
            QPlainTextEdit#Console, QTextEdit#Console { background: #151b20; color: #d9e2e8; border: 0; padding: 8px; selection-background-color: #365b73; }
            QSplitter::handle { background: #cbd2d8; }
            QSplitter::handle:vertical { height: 2px; }
            QSplitter::handle:horizontal { width: 2px; }
            QStatusBar { background: #eef1f4; color: #5f6d78; border-top: 1px solid #cad2d8; }
            QDialog#VmWizard, QDialog#VmSettings { background: #f3f5f7; color: #17212b; }
            QWidget#WizardPage { background: #ffffff; }
            QListWidget#WizardSteps { background: #e7eaee; border: 0; border-right: 1px solid #c3c9cf; padding-top: 24px; }
            QListWidget#WizardSteps::item { min-height: 42px; padding: 0 16px; color: #53616d; border-left: 4px solid transparent; }
            QListWidget#WizardSteps::item:selected { background: #d7e5f1; color: #154e7a; border-left: 4px solid #2678b8; font-weight: 600; }
            QLabel#WizardTitle, QLabel#SettingsTitle { color: #17212b; font-size: 23px; font-weight: 650; }
            QLabel#WizardSubtitle, QLabel#SettingsSubtitle { color: #62717d; font-size: 13px; }
            QLabel#FieldHelp { color: #6b7781; margin-left: 25px; }
            QLabel#Recommendation { color: #22689d; background: #edf6fc; border: 1px solid #c8e0f1; border-radius: 4px; padding: 8px; }
            QLabel#WizardSummary { background: #f7f9fa; border: 1px solid #d4dbe1; border-radius: 5px; padding: 18px; }
            QLabel#SettingsHeader { background: #ffffff; border: 1px solid #d1d7dc; padding: 12px 16px; font-size: 14px; }
            QListWidget#SettingsCategories, QTreeWidget#SettingsCategories { background: #ffffff; border: 1px solid #c7ced5; outline: 0; }
            QListWidget#SettingsCategories::item, QTreeWidget#SettingsCategories::item { min-height: 34px; padding: 2px 8px; border-bottom: 1px solid #edf0f2; }
            QListWidget#SettingsCategories::item:selected, QTreeWidget#SettingsCategories::item:selected { background: #276fa6; color: #ffffff; }
            QDialog#VmSettings QStackedWidget { background: #ffffff; border: 1px solid #c7ced5; }
        """
        if self.dark_mode:
            style += """
                QMainWindow, QWidget, QWidget#VmPage, QWidget#WelcomePage, QDialog#VmWizard, QDialog#VmSettings { background: #1e242a; color: #e7edf2; }
                QMenuBar, QMenu, QToolBar#MainToolbar { background: #252c33; color: #e7edf2; border-color: #3c4650; }
                QMenuBar::item:selected, QMenu::item:selected { background: #314b61; color: #ffffff; }
                QToolBar#MainToolbar QToolButton { background: transparent; color: #e7edf2; border-color: transparent; }
                QToolBar#MainToolbar QToolButton:hover { background: #35414b; color: #ffffff; border-color: #4d5c68; }
                QToolBar#MainToolbar QToolButton:pressed, QToolBar#MainToolbar QToolButton:checked { background: #17212a; color: #8bc5ef; border-color: #527b99; }
                QToolBar#MainToolbar QToolButton:focus { border-color: #568fba; }
                QWidget#LibraryPanel, QStatusBar { background: #242b32; color: #c6d0d8; border-color: #3d4750; }
                QTreeWidget#Library, QTreeWidget, QListWidget { background: #252c33; color: #dce4ea; alternate-background-color: #2a3239; }
                QTreeWidget QHeaderView::section, QHeaderView::section { background: #303840; color: #b9c4cc; border-color: #47515a; }
                QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: #171c21; color: #e5ebef; border-color: #4b5660; }
                QPushButton { background: #303840; color: #e5ebef; border-color: #56626c; }
                QPushButton:hover { background: #394550; border-color: #568fba; }
                QPushButton:disabled { background: #282f35; color: #75808a; border-color: #3d464e; }
                QFrame#SummaryCard, QWidget#WizardPage, QLabel#SettingsHeader, QListWidget#SettingsCategories, QTreeWidget#SettingsCategories, QDialog#VmSettings QStackedWidget { background: #252c33; color: #e5ebef; border-color: #46515a; }
                QListWidget#WizardSteps { background: #20272d; color: #cbd5dc; border: 0; border-right: 1px solid #46515a; }
                QListWidget#WizardSteps::item { color: #aebbc5; border-left-color: transparent; }
                QListWidget#WizardSteps::item:hover { background: #29343d; color: #e7edf2; }
                QListWidget#WizardSteps::item:selected { background: #263f52; color: #9fd3f5; border-left-color: #4b9bd3; }
                QLabel#WizardSummary { background: #20272d; color: #e5ebef; border-color: #46515a; }
                QLabel#Recommendation { background: #223746; color: #9fd3f5; border-color: #365d78; }
                QLabel#WelcomeTitle, QLabel#VmTitle, QLabel#CardTitle, QLabel#WizardTitle, QLabel#SettingsTitle, QLabel#HardwareValue { color: #edf3f7; }
                QLabel#WelcomeSubtitle, QLabel#VmSubtitle, QLabel#WizardSubtitle, QLabel#SettingsSubtitle, QLabel#HardwareLabel, QLabel#VmPath { color: #aebbc5; }
                QWidget#VmTabsContainer { background: #20272d; border-color: #3b4650; }
                QTabBar#VmTabs { background: transparent; }
                QTabBar#VmTabs::tab { background: #283139; color: #aab6bf; border: 0; border-top: 2px solid transparent; border-right: 1px solid #3d4851; }
                QTabBar#VmTabs::tab:hover { background: #303c45; color: #e1e8ed; }
                QTabBar#VmTabs::tab:selected { background: #1e242a; color: #8bcaf2; border-top-color: #3698d1; font-weight: 600; }
                QTabWidget#ConsoleTabs QTabBar::tab { background: #252c33; color: #cbd5dc; border-color: #46515a; }
                QTabWidget#ConsoleTabs QTabBar::tab:selected { background: #17212a; color: #8bc5ef; }
                QSplitter::handle { background: #46515a; }
            """
        self.setStyleSheet(style)

    def set_dark_mode(self, enabled: bool) -> None:
        self.dark_mode = enabled
        self.settings.setValue("darkMode", enabled)
        self._apply_style()

    def install_language_pack(self) -> None:
        locale_name = QLocale.system().name() if self.language == "system" else self.language
        if locale_name.startswith("en"):
            return
        catalog = TRANSLATIONS_DIR / f"edgeos_workstation_{locale_name}.qm"
        if catalog.is_file() and self.translator.load(str(catalog)):
            QApplication.instance().installTranslator(self.translator)

    def translate_widget_tree(self) -> None:
        context = "EdgeOSWorkstation"
        for widget_type in (QLabel, QPushButton, QCheckBox, QGroupBox):
            for widget in self.findChildren(widget_type):
                source = widget.text()
                if source:
                    widget.setText(QCoreApplication.translate(context, source))
        for action in self.findChildren(QAction):
            source = action.text()
            if source:
                action.setText(QCoreApplication.translate(context, source))

    def set_language(self, language: str) -> None:
        if language == self.language:
            return
        self.settings.setValue("language", language)
        QMessageBox.information(self, "Language Changed", "Restart EdgeOS Workstation to apply the selected language pack.")

    def open_web_console(self) -> None:
        address = "http://127.0.0.1:8765"
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            running = probe.connect_ex(("127.0.0.1", 8765)) == 0
        finally:
            probe.close()
        if not running:
            QProcess.startDetached(sys.executable, [str(WORKSTATION_SERVER)], str(REPO_ROOT))
            QTimer.singleShot(700, lambda: webbrowser.open(address))
        else:
            webbrowser.open(address)

    def show_library_menu(self, position) -> None:
        if not self.selected_name():
            return
        menu = QMenu(self)
        menu.addAction(self.start_action)
        menu.addAction(self.stop_action)
        menu.addAction(self.force_stop_action)
        menu.addAction(self.reset_action)
        menu.addAction(self.pause_action)
        menu.addAction(self.resume_action)
        menu.addAction(self.suspend_action)
        menu.addSeparator()
        menu.addAction(self.install_tools_action)
        menu.addSeparator()
        favorite_label = "Remove from Favorites" if self.selected_name() in self.favorites else "Add to Favorites"
        menu.addAction(favorite_label, self.toggle_favorite)
        menu.addAction(self.settings_action)
        menu.addAction("Take Snapshot…", self.snapshot_vm)
        menu.addAction("Clone…", self.clone_vm)
        menu.addSeparator()
        menu.addAction("Delete from Disk…", self.delete_vm)
        menu.exec(self.library.viewport().mapToGlobal(position))

    def set_display_maximized(self, enabled: bool) -> None:
        self.display_maximized = enabled
        self.maximize_display_action.setChecked(enabled)
        self._apply_display_mode()

    def toggle_fullscreen(self, enabled: bool) -> None:
        self.display_fullscreen = enabled
        self.fullscreen_action.setChecked(enabled)
        self._apply_display_mode()
        if enabled:
            self.showFullScreen()
            self.rfb_view.setFocus(Qt.FocusReason.ShortcutFocusReason)
        else:
            self.showNormal()

    def _apply_display_mode(self) -> None:
        focused = self.display_maximized or self.display_fullscreen
        self.summary_card.setVisible(not focused)
        self.console_tabs.setVisible(not focused)
        self.vm_header.setVisible(not self.display_fullscreen)
        self.vm_divider.setVisible(not self.display_fullscreen)
        self.library_panel.setVisible(not self.display_fullscreen)
        self.menuBar().setVisible(not self.display_fullscreen)
        self.main_toolbar.setVisible(not self.display_fullscreen)
        self.statusBar().setVisible(not self.display_fullscreen)
        self.vm_tabs_container.setVisible(
            not self.display_fullscreen and self.vm_tabs.count() > 0
        )
        if self.display_fullscreen:
            self.vm_page_layout.setContentsMargins(0, 0, 0, 0)
            self.vm_page_layout.setSpacing(0)
        else:
            self.vm_page_layout.setContentsMargins(30, 26, 30, 24)
            self.vm_page_layout.setSpacing(18)
        self.display_maximize_btn.setVisible(not self.display_fullscreen)
        maximize_label = (
            "Restore Workstation Layout"
            if self.display_maximized
            else "Maximize Display"
        )
        self.display_maximize_btn.setToolTip(maximize_label)
        self.display_maximize_btn.setStatusTip(maximize_label)
        self.display_maximize_btn.setAccessibleName(maximize_label)
        fullscreen_label = (
            "Exit Full Screen" if self.display_fullscreen else "Enter Full Screen"
        )
        self.display_fullscreen_btn.setToolTip(fullscreen_label)
        self.display_fullscreen_btn.setStatusTip(fullscreen_label)
        self.display_fullscreen_btn.setAccessibleName(fullscreen_label)

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "About EdgeOS Workstation",
            "<b>EdgeOS Workstation</b><br><br>A desktop interface for building, running, and managing persistent EdgeOS virtual machines.",
        )

    def filter_library(self, text: str) -> None:
        query = text.strip().lower()
        mode = self.library_filter.currentText() if hasattr(self, "library_filter") else "All Virtual Machines"
        for index in range(self.library.topLevelItemCount()):
            item = self.library.topLevelItem(index)
            name = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            state = item.text(1)
            visible = query in name.lower()
            if mode == "Running":
                visible = visible and state in ("Running", "Paused")
            elif mode == "Powered Off":
                visible = visible and state == "Off"
            elif mode == "Suspended":
                visible = visible and state == "Suspended"
            elif mode == "Favorites":
                visible = visible and name in self.favorites
            item.setHidden(not visible)

    def toggle_favorite(self) -> None:
        name = self.selected_name()
        if not name:
            return
        if name in self.favorites:
            self.favorites.remove(name)
        else:
            self.favorites.add(name)
        self.settings.setValue("favorites", sorted(self.favorites))
        self.refresh()

    def append_log(self, text: str) -> None:
        text = text.rstrip()
        if not text:
            return
        self.log.append_plain_line(text)
        print(text, flush=True)

    def append_output(self, text: str) -> None:
        if not text:
            return
        self.log.write(text)
        if not sys.stdout.isatty():
            print(text, end="", flush=True)

    def show_task_center(self) -> None:
        if self.task_dialog is None:
            self.task_dialog = TaskCenterDialog(self.task_journal, self.cancel_task, self)
        self.task_dialog.refresh()
        self.task_dialog.show()
        self.task_dialog.raise_()
        self.task_dialog.activateWindow()

    @staticmethod
    def task_vm_name(args: list[str]) -> str | None:
        if not args:
            return None
        if args[0] == "create" and len(args) >= 3:
            return args[2]
        if args[0] in {"tap", "macvtap", "list"}:
            return None
        if len(args) >= 2 and not args[1].startswith("-"):
            return args[1]
        return None

    def write_serial_bytes(self, data: bytes) -> bool:
        if not data:
            return True
        if self.serial_fd is not None:
            view = memoryview(data)
            while view:
                _, writable, _ = select.select([], [self.serial_fd], [], 1.0)
                if not writable:
                    self.append_log("[serial] timed out waiting for PTY to accept input")
                    return False
                try:
                    n = os.write(self.serial_fd, view)
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                if n <= 0:
                    self.append_log("[serial] PTY accepted zero bytes")
                    return False
                view = view[n:]
            if os.isatty(self.serial_fd):
                termios.tcdrain(self.serial_fd)
            return True
        if self.process is not None:
            self.process.write(data)
            return True
        return False

    def send_serial_input(self) -> None:
        if self.serial_fd is None and self.process is None:
            return
        text = self.serial_input.text()
        if text == "":
            return
        data = (text + "\n").encode()
        self.write_serial_bytes(data)
        self.serial_input.clear()

    def close_serial_pty(self) -> None:
        if self.serial_read_notifier is not None:
            self.serial_read_notifier.setEnabled(False)
            self.serial_read_notifier.deleteLater()
            self.serial_read_notifier = None
        if self.serial_fd is not None:
            os.close(self.serial_fd)
            self.serial_fd = None
        self.serial_connected_name = None
        self.serial_is_socket = False

    def install_serial_reader(self) -> None:
        if self.serial_fd is None:
            return
        self.serial_read_notifier = QSocketNotifier(
            self.serial_fd,
            QSocketNotifier.Type.Read,
            self,
        )
        self.serial_read_notifier.activated.connect(self.drain_serial_output)

    def drain_serial_output(self) -> None:
        if self.serial_fd is None:
            return
        chunks: list[bytes] = []
        while True:
            try:
                data = os.read(self.serial_fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                self.close_serial_pty()
                return
            if not data:
                self.close_serial_pty()
                return
            chunks.append(data)
        if chunks and not self.serial_is_socket:
            self.append_output(b"".join(chunks).decode("utf-8", errors="replace"))

    def connect_serial_pty(self, name: str, attempts: int = 30) -> None:
        if self.serial_fd is not None and self.serial_connected_name == name:
            return
        serial_socket = INSTANCES_DIR / name / "serial.sock"
        if serial_socket.exists():
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(serial_socket))
                self.close_serial_pty()
                self.serial_fd = client.detach()
                self.serial_connected_name = name
                self.serial_is_socket = True
                os.set_blocking(self.serial_fd, False)
            except OSError as exc:
                if attempts > 0:
                    QTimer.singleShot(250, lambda: self.connect_serial_pty(name, attempts - 1))
                else:
                    self.append_log(f"[serial] failed to connect to {serial_socket.relative_to(REPO_ROOT)}: {exc}")
                return
            self.append_log(f"[serial] connected {serial_socket.relative_to(REPO_ROOT)}")
            self.install_serial_reader()
            self.serial_input.setEnabled(True)
            self.serial_send_btn.setEnabled(True)
            return
        pty_path_file = INSTANCES_DIR / name / "serial.pty"
        if not pty_path_file.exists():
            if attempts > 0:
                QTimer.singleShot(250, lambda: self.connect_serial_pty(name, attempts - 1))
            return
        try:
            pty_path = pty_path_file.read_text(encoding="ascii").strip()
            self.close_serial_pty()
            fd = os.open(pty_path, os.O_RDWR | os.O_NOCTTY)
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            attrs[3] &= ~(termios.ECHO | termios.ICANON)
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            os.set_blocking(fd, False)
        except OSError as exc:
            if attempts > 0:
                QTimer.singleShot(250, lambda: self.connect_serial_pty(name, attempts - 1))
            else:
                self.append_log(f"[serial] failed to open {pty_path_file.relative_to(REPO_ROOT)}: {exc}")
            return
        self.serial_fd = fd
        self.serial_connected_name = name
        self.serial_is_socket = False
        self.install_serial_reader()
        self.append_log(f"[serial] connected {pty_path}")
        self.serial_input.setEnabled(True)
        self.serial_send_btn.setEnabled(True)

    def start_serial_tail(self, name: str) -> None:
        serial_log = INSTANCES_DIR / name / "serial.log"
        self.serial_tail_name = name
        try:
            self.serial_tail_pos = serial_log.stat().st_size
        except OSError:
            self.serial_tail_pos = 0
        self.append_log(f"[serial] tailing {serial_log.relative_to(REPO_ROOT)}")

    def poll_serial_log(self) -> None:
        if not self.serial_tail_name:
            return
        if self.process is not None and self.pending_serial_name == self.serial_tail_name:
            return
        instance = INSTANCES_DIR / self.serial_tail_name
        pid_path = instance / "qemu.pid"
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            qemu_log = instance / "qemu.log"
            try:
                lines = qemu_log.read_text(encoding="utf-8", errors="replace").splitlines()
                detail = "\n".join(lines[-30:])
            except OSError:
                detail = ""
            self.append_log(f"[VM failed] {self.serial_tail_name} QEMU process exited")
            if detail:
                self.append_log(detail)
            self.serial_tail_name = None
            self.serial_input.setEnabled(False)
            self.serial_send_btn.setEnabled(False)
            return
        serial_log = INSTANCES_DIR / self.serial_tail_name / "serial.log"
        try:
            size = serial_log.stat().st_size
            if size < self.serial_tail_pos:
                self.serial_tail_pos = 0
            if size == self.serial_tail_pos:
                return
            with serial_log.open("rb") as f:
                f.seek(self.serial_tail_pos)
                data = f.read(65536)
                self.serial_tail_pos = f.tell()
        except OSError:
            return
        if data:
            self.append_output(data.decode(errors="replace"))

    def run_cli(self, args: list[str], title: str) -> None:
        if self.process is not None:
            self.show_task_center()
            return
        if args and args[0] in {"x11-run", "guest-push", "guest-pull", "install-tools"}:
            # The QEMU serial chardev accepts one client. Release the GUI
            # connection while the CLI performs its supervised guest session.
            self.close_serial_pty()
        self.append_log(f"\n[{title}] python3 {CLI.relative_to(REPO_ROOT)} {' '.join(args)}")
        proc = QProcess(self)
        proc.setProgram(sys.executable)
        # Keep stage messages visible even when the Python CLI writes to a pipe.
        proc.setArguments(["-u", str(CLI), *args])
        proc.setWorkingDirectory(str(REPO_ROOT))
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        task = self.task_journal.create(
            title,
            [sys.executable, "-u", str(CLI), *args],
            self.task_vm_name(args),
        )
        self.active_task_id = str(task["id"])
        self.task_cancel_requested = False
        self.task_title = title
        self.task_started_monotonic = time.monotonic()
        self.task_last_output_monotonic = self.task_started_monotonic
        self.pending_task_output.clear()
        proc.started.connect(self.task_started)
        proc.readyReadStandardOutput.connect(self.read_task_output)
        proc.finished.connect(lambda code, _status: self.command_finished(code))
        proc.errorOccurred.connect(self.task_process_error)
        self.process = proc
        self.current_cli_command = args[0] if args else None
        self.current_cli_vm_name = self.task_vm_name(args)
        self.status_task.setText(title)
        self.task_progress.show()
        self.task_status_timer.start()
        self.set_actions_enabled(False)
        self.serial_input.setEnabled(args[0] == "start" and "--background" not in args)
        self.serial_send_btn.setEnabled(args[0] == "start" and "--background" not in args)
        proc.start()

    def task_started(self) -> None:
        if self.process is None or self.active_task_id is None:
            return
        self.task_journal.update(
            self.active_task_id,
            state="running",
            started_at=int(time.time()),
            pid=int(self.process.processId()),
        )
        if self.task_dialog is not None:
            self.task_dialog.refresh()

    def read_task_output(self) -> None:
        if self.process is None:
            return
        text = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        if not text:
            return
        self.pending_task_output.append(text)
        self.task_last_output_monotonic = time.monotonic()
        if not self.task_output_timer.isActive():
            self.task_output_timer.start()

    def flush_task_output(self) -> None:
        if not self.pending_task_output:
            return
        text = "".join(self.pending_task_output)
        self.pending_task_output.clear()
        self.append_output(text)
        if self.active_task_id is not None:
            self.task_journal.append_output(
                self.active_task_id,
                text,
                persist=False,
            )
            self.task_journal.flush()
            if self.task_dialog is not None:
                self.task_dialog.append_task_output(self.active_task_id, text)

    def update_task_status(self) -> None:
        if self.process is None or self.task_started_monotonic is None:
            return
        now = time.monotonic()
        elapsed = max(0, int(now - self.task_started_monotonic))
        minutes, seconds = divmod(elapsed, 60)
        activity = "running"
        if self.task_last_output_monotonic is not None:
            quiet = max(0, int(now - self.task_last_output_monotonic))
            if quiet >= 3:
                activity = f"last output {quiet}s ago"
        self.status_task.setText(
            f"{self.task_title} • {minutes:02d}:{seconds:02d} • {activity}"
        )

    def task_process_error(self, error) -> None:
        if self.process is None:
            return
        if error == QProcess.ProcessError.FailedToStart:
            self.append_log(f"[task] failed to start: {self.process.errorString()}")
            self.command_finished(-1)

    def cancel_task(self, task_id: str) -> None:
        if self.process is None or task_id != self.active_task_id:
            return
        self.task_cancel_requested = True
        self.task_journal.update(task_id, state="cancelling")
        self.status_task.setText("Cancelling task")
        self.process.terminate()
        QTimer.singleShot(3000, lambda: self.force_cancel_task(task_id))
        if self.task_dialog is not None:
            self.task_dialog.refresh()

    def force_cancel_task(self, task_id: str) -> None:
        if self.process is not None and self.active_task_id == task_id:
            self.process.kill()

    def command_finished(self, code: int) -> None:
        if self.process is None:
            return
        self.read_task_output()
        self.task_output_timer.stop()
        self.flush_task_output()
        pending = self.pending_serial_name
        pending_vnc_port = self.pending_vnc_port
        command = self.current_cli_command
        command_vm_name = self.current_cli_vm_name
        if command == "start" and pending and code == 0:
            self.append_log("[launcher] exit=0; QEMU is running in the background")
        else:
            self.append_log(f"[done] exit={code}")
        self.pending_serial_name = None
        self.pending_vnc_port = None
        task_id = self.active_task_id
        if self.closing:
            task_state = "interrupted"
        elif self.task_cancel_requested:
            task_state = "cancelled"
        else:
            task_state = "completed" if code == 0 else "failed"
        if task_id is not None:
            self.task_journal.update(
                task_id,
                state=task_state,
                finished_at=int(time.time()),
                exit_code=code,
                pid=None,
            )
        self.process = None
        self.current_cli_command = None
        self.current_cli_vm_name = None
        self.active_task_id = None
        self.task_cancel_requested = False
        self.task_status_timer.stop()
        self.task_title = ""
        self.task_started_monotonic = None
        self.task_last_output_monotonic = None
        self.task_progress.hide()
        if command == "set-resolution" and code == 0 and command_vm_name:
            self.pending_display_resolutions.pop(command_vm_name, None)
        if task_state == "cancelled":
            self.status_task.setText("Task cancelled")
        else:
            self.status_task.setText("Ready" if code == 0 else f"Task failed (exit {code})")
        if code == 0 and pending:
            self.connect_serial_pty(pending)
            if pending_vnc_port is not None:
                QTimer.singleShot(300, lambda: self.connect_display(pending, pending_vnc_port))
        elif code != 0 and pending and pending_vnc_port is not None:
            (INSTANCES_DIR / pending / "vnc.port").unlink(missing_ok=True)
        elif self.serial_fd is None:
            self.serial_input.setEnabled(False)
            self.serial_send_btn.setEnabled(False)
        self.set_actions_enabled(True)
        self.refresh()
        if command == "set-resolution" and code == 0:
            QMessageBox.information(
                self,
                "Resolution Saved",
                "The guest display resolution will be applied by UEFI the next time this virtual machine starts.",
            )
        if command == "start" and code != 0 and pending:
            self.stop_authoritative_display_capture(pending)
        if self.task_dialog is not None:
            self.task_dialog.refresh()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.closing = True
        self.stop_vnc_share()
        self.stop_authoritative_display_capture()
        if self.process is not None and self.active_task_id is not None:
            self.read_task_output()
            self.task_output_timer.stop()
            self.flush_task_output()
            self.task_journal.update(
                self.active_task_id,
                state="interrupted",
                finished_at=int(time.time()),
                pid=None,
            )
            self.process.kill()
            self.process.waitForFinished(1000)
        self.close_serial_pty()
        for view in self.display_views.values():
            view.disconnect_from_server()
        self.display_ports.clear()
        for watcher in self.qmp_watchers.values():
            watcher.requestInterruption()
        for watcher in self.qmp_watchers.values():
            watcher.wait(1500)
        self.qmp_watchers.clear()
        super().closeEvent(event)

    def set_actions_enabled(self, enabled: bool) -> None:
        for button in [
            self.refresh_btn,
            self.create_btn,
            self.start_btn,
            self.start_headless_btn,
            self.start_no_kvm_btn,
            self.start_xfce_btn,
            self.stop_btn,
            self.update_btn,
            self.snapshot_btn,
            self.clone_btn,
            self.config_btn,
            self.delete_btn,
            self.tap_btn,
            self.macvtap_btn,
            self.vnc_share_btn,
            self.apply_resolution_btn,
        ]:
            button.setEnabled(enabled)
        for action in [
            self.new_action,
            self.refresh_action,
            self.start_action,
            self.stop_action,
            self.force_stop_action,
            self.reset_action,
            self.pause_action,
            self.resume_action,
            self.suspend_action,
            self.install_tools_action,
            self.settings_action,
        ]:
            action.setEnabled(enabled)
        self.maximize_display_action.setEnabled(enabled)
        self.fullscreen_action.setEnabled(enabled)
        self.header_start.setEnabled(enabled)
        self.header_settings.setEnabled(enabled)
        self.desktop_btn.setEnabled(enabled)
        self.display_resolution.setEnabled(enabled)
        if enabled:
            self.update_actions()

    def ensure_vm_tab(self, name: str) -> None:
        for index in range(self.vm_tabs.count()):
            if self.vm_tabs.tabData(index) == name:
                if self.vm_tabs.currentIndex() != index:
                    self.vm_tabs.setCurrentIndex(index)
                return
        index = self.vm_tabs.addTab(name)
        self.vm_tabs.setTabData(index, name)
        self.vm_tabs.setCurrentIndex(index)
        self.vm_tabs_container.show()

    def vm_tab_changed(self, index: int) -> None:
        if index < 0:
            return
        name = self.vm_tabs.tabData(index)
        if not name or self.selected_name() == str(name):
            return
        for row in range(self.library.topLevelItemCount()):
            item = self.library.topLevelItem(row)
            if item.data(0, Qt.ItemDataRole.UserRole) == name:
                self.library.setCurrentItem(item)
                break

    def close_vm_tab(self, index: int) -> None:
        name = str(self.vm_tabs.tabData(index) or "")
        self.vm_tabs.removeTab(index)
        view = self.display_views.pop(name, None)
        self.display_ports.pop(name, None)
        if view is not None:
            view.disconnect_from_server()
            self.display_stack.removeWidget(view)
            view.deleteLater()
        if self.vm_tabs.count() == 0:
            self.vm_tabs_container.hide()
            self.library.clearSelection()

    def selected_name(self) -> str | None:
        items = self.library.selectedItems()
        if not items:
            return None
        value = items[0].data(0, Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def update_actions(self) -> None:
        name = self.selected_name()
        has_vm = name is not None
        for button in [self.start_btn, self.start_headless_btn, self.start_no_kvm_btn, self.start_xfce_btn, self.stop_btn, self.update_btn, self.snapshot_btn, self.clone_btn, self.config_btn, self.delete_btn]:
            button.setEnabled(has_vm and self.process is None)
        runtime = self.vm_runtime_status(name) if name else {"running": False, "status": "shutdown"}
        running = bool(runtime.get("running"))
        paused = runtime.get("status") in ("paused", "prelaunch", "suspended")
        self.start_action.setEnabled(has_vm and not running and self.process is None)
        self.stop_action.setEnabled(has_vm and running and self.process is None)
        self.force_stop_action.setEnabled(has_vm and running and self.process is None)
        self.reset_action.setEnabled(has_vm and running and self.process is None)
        self.pause_action.setEnabled(has_vm and running and not paused and self.process is None)
        self.resume_action.setEnabled(has_vm and running and paused and self.process is None)
        self.suspend_action.setEnabled(has_vm and running and self.process is None)
        self.install_tools_action.setEnabled(has_vm and running and self.process is None)
        self.settings_action.setEnabled(has_vm and self.process is None)
        self.maximize_display_action.setEnabled(has_vm)
        self.fullscreen_action.setEnabled(has_vm)
        self.header_start.setEnabled(has_vm and self.process is None)
        self.header_settings.setEnabled(has_vm and self.process is None)
        self.desktop_btn.setEnabled(has_vm and running and self.process is None)
        cfg = self.config_for_name(name) if name else None
        resolution_supported = bool(
            cfg and supports_boot_display_resolution(cfg)
        )
        resolution_enabled = resolution_supported and self.process is None
        self.display_resolution.setEnabled(resolution_enabled)
        self.apply_resolution_btn.setEnabled(resolution_enabled)
        if not resolution_supported:
            resolution_tip = (
                "Boot resolution is available for ARM64 virtual machines."
            )
        elif self.process is not None:
            resolution_tip = (
                "Wait for the current task to finish before changing resolution."
            )
        else:
            resolution_tip = "Guest resolution for the next VM start"
        self.display_resolution.setToolTip(resolution_tip)
        self.apply_resolution_btn.setToolTip(resolution_tip)
        self.vnc_share_btn.setEnabled(
            has_vm
            and running
            and name in self.display_ports
            and self.process is None
        )
        self.sync_vnc_share_button()
        if not name:
            self.workspace.setCurrentIndex(0)
            self.status_name.setText("No virtual machine selected")
            return
        self.ensure_vm_tab(name)
        self.workspace.setCurrentIndex(1)
        cfg = self.config_for_name(name)
        if cfg is not None:
            self.update_vm_summary(name, cfg)
        self.attach_selected_vm(name, running)

    def attach_selected_vm(self, name: str, running: bool) -> None:
        self.activate_display_view(name)
        if not running:
            if self.vnc_share_name == name:
                self.stop_vnc_share()
            self.stop_authoritative_display_capture(name)
            if self.serial_fd is not None:
                self.close_serial_pty()
            self.serial_tail_name = None
            self.serial_input.setEnabled(False)
            self.serial_send_btn.setEnabled(False)
            self.disconnect_display(
                "VIRTUAL MACHINE IS POWERED OFF\n\nClick Power On to start it"
            )
            self.display_state.setText("POWERED OFF • NOT STARTED")
            self.refresh_auxiliary_views(name)
            return
        if self.serial_tail_name != name:
            self.start_serial_tail(name)
        if self.serial_connected_name != name:
            self.connect_serial_pty(name)
        port_path = INSTANCES_DIR / name / "vnc.port"
        try:
            port = int(port_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            self.disconnect_display(
                "THIS VIRTUAL MACHINE HAS NO EMBEDDED DISPLAY\n\nRestart it with the embedded console enabled"
            )
            self.display_state.setText("RUNNING WITHOUT EMBEDDED DISPLAY")
        else:
            if self.authoritative_display_name is None:
                cfg = self.config_for_name(name) or {}
                self.start_authoritative_display_capture(
                    name,
                    persistent=str(cfg.get("desktop", "console")) != "xfce",
                )
            self.connect_display(name, port)
        self.refresh_auxiliary_views(name)

    def refresh_auxiliary_views(self, name: str) -> None:
        qemu_log = INSTANCES_DIR / name / "qemu.log"
        try:
            data = qemu_log.read_bytes()[-256 * 1024:]
            self.qemu_log.setPlainText(data.decode("utf-8", errors="replace"))
            self.qemu_log.moveCursor(QTextCursor.MoveOperation.End)
        except OSError:
            self.qemu_log.clear()

        self.snapshots.clear()
        snapshot_dir = INSTANCES_DIR / name / "snapshots"
        for path in sorted(snapshot_dir.iterdir()) if snapshot_dir.is_dir() else []:
            if path.is_dir() and (path / "snapshot.json").is_file():
                try:
                    manifest = json.loads((path / "snapshot.json").read_text(encoding="utf-8"))
                    disks = manifest.get("disks", [])
                    size_bytes = sum(int(disk.get("size", 0)) for disk in disks if isinstance(disk, dict))
                    created_at = int(manifest.get("created_at", path.stat().st_mtime))
                    description = str(manifest.get("description", ""))
                    disk_count = len(disks) if isinstance(disks, list) else 0
                except (OSError, ValueError, json.JSONDecodeError):
                    description, disk_count, size_bytes, created_at = "Invalid manifest", 0, 0, int(path.stat().st_mtime)
                item = QTreeWidgetItem([
                    path.name,
                    description,
                    str(disk_count),
                    f"{size_bytes / (1024 ** 2):.1f} MB",
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(created_at)),
                ])
            elif path.suffix == ".img":
                stat = path.stat()
                item = QTreeWidgetItem([
                    path.stem,
                    "Legacy snapshot",
                    "1",
                    f"{stat.st_size / (1024 ** 2):.1f} MB",
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                ])
            else:
                continue
            item.setData(0, Qt.ItemDataRole.UserRole, item.text(0))
            self.snapshots.addTopLevelItem(item)

    @staticmethod
    def find_available_vnc_port() -> int:
        for port in range(5900, 6000):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                probe.close()
                continue
            probe.close()
            return port
        raise RuntimeError("No free local VNC port is available between 5900 and 5999")

    def connect_display(self, name: str, port: int) -> None:
        view = self.activate_display_view(name)
        if self.display_ports.get(name) == port and view.is_connected:
            self.display_connected_name = name
            self.display_connected_port = port
            return
        if name in self.display_ports:
            view.disconnect_from_server()
        self.display_connected_name = name
        self.display_connected_port = port
        self.display_ports[name] = port
        self.display_state.setText(f"CONNECTING • 127.0.0.1:{port}")
        view.connect_to("127.0.0.1", port)

    def start_authoritative_display_capture(
        self, name: str, *, persistent: bool
    ) -> None:
        self.authoritative_display_name = name
        self.authoritative_display_persistent = persistent
        self.authoritative_display_timer.start()
        self.capture_authoritative_display_frame()

    def stop_authoritative_display_capture(self, name: str | None = None) -> None:
        active_name = self.authoritative_display_name
        if name is not None and active_name != name:
            return
        self.authoritative_display_timer.stop()
        self.authoritative_display_name = None
        self.authoritative_display_persistent = False
        if active_name:
            view = self.display_views.get(active_name)
            if view is not None:
                view.clear_authoritative_frame()
            (INSTANCES_DIR / active_name / ".workstation-display.png").unlink(
                missing_ok=True
            )

    def capture_authoritative_display_frame(self) -> None:
        name = self.authoritative_display_name
        if not name or self.selected_name() != name:
            return
        instance = INSTANCES_DIR / name
        qmp_path = instance / "qmp.sock"
        output = instance / ".workstation-display.png"
        if not qmp_path.exists():
            return
        try:
            with QmpClient(qmp_path, timeout=0.2) as client:
                client.execute(
                    "screendump",
                    {"filename": str(output), "format": "png"},
                )
            image = QImage(str(output))
        except (OSError, QmpError):
            return
        if image.isNull():
            return
        view = self.display_views.get(name)
        if view is None:
            view = self.activate_display_view(name)
        view.set_authoritative_frame(image)

    def activate_display_view(self, name: str) -> RfbView:
        view = self.display_views.get(name)
        if view is None:
            view = RfbView()
            view.setObjectName("RfbView")
            view.setAccessibleName(f"Virtual machine display for {name}")
            view.connected.connect(
                lambda width, height, server_name, vm_name=name: self.display_connected(
                    vm_name, width, height, server_name
                )
            )
            view.disconnected.connect(
                lambda reason, vm_name=name: self.display_disconnected(vm_name, reason)
            )
            view.frame_updated.connect(
                lambda image, vm_name=name: self.display_frame_updated(vm_name, image)
            )
            view.scale_changed.connect(
                lambda percent, vm_name=name: self.display_scale_updated(
                    vm_name, percent
                )
            )
            self.display_views[name] = view
            self.display_stack.addWidget(view)
        self.display_stack.setCurrentWidget(view)
        self.rfb_view = view
        self.display_connected_name = name
        self.display_connected_port = self.display_ports.get(name)
        return view

    def disconnect_display(self, placeholder: str | None = None) -> None:
        name = self.selected_name() or self.display_connected_name
        self.display_connected_name = None
        self.display_connected_port = None
        if name:
            self.display_ports.pop(name, None)
        if placeholder is None:
            self.rfb_view.disconnect_from_server()
        else:
            self.rfb_view.show_placeholder(placeholder)

    def display_connected(self, name: str, width: int, height: int, _server_name: str) -> None:
        self.display_dimensions[name] = (width, height)
        if self.selected_name() == name:
            self.update_display_state(name)
            self.vnc_share_btn.setEnabled(self.process is None)

    def display_frame_updated(self, name: str, image: QImage) -> None:
        self.display_dimensions[name] = (image.width(), image.height())
        if self.selected_name() == name:
            self.update_display_state(name)
        if (
            self.authoritative_display_name == name
            and not self.authoritative_display_persistent
            and RfbView.frame_has_visual_detail(image)
        ):
            self.stop_authoritative_display_capture(name)

    def display_scale_updated(self, name: str, percent: int) -> None:
        self.display_scale_percent[name] = percent
        if self.selected_name() == name:
            self.update_display_state(name)

    def update_display_state(self, name: str) -> None:
        width, height = self.display_dimensions.get(name, (0, 0))
        percent = self.display_scale_percent.get(name, 100)
        if not width or not height:
            return
        scale_label = "NATIVE 100%" if percent == 100 else f"{percent}% VIEW"
        self.display_state.setText(
            f"LIVE • {width} × {height} • {scale_label} • INPUT ACTIVE"
        )

    def display_disconnected(self, name: str, reason: str) -> None:
        port = self.display_ports.pop(name, None)
        if port is None:
            return
        if self.selected_name() == name:
            self.display_connected_name = None
            self.display_connected_port = None
            self.display_state.setText(reason.upper())
        if self.vm_is_running(name) and self.selected_name() == name:
            QTimer.singleShot(1000, lambda: self.retry_display(name, port))

    def retry_display(self, name: str, port: int) -> None:
        if self.selected_name() != name or not self.vm_is_running(name):
            return
        port_path = INSTANCES_DIR / name / "vnc.port"
        try:
            current_port = int(port_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return
        if current_port == port:
            self.connect_display(name, port)

    def send_ctrl_alt_delete(self) -> None:
        self.rfb_view.send_ctrl_alt_delete()

    def change_display_scaling(self) -> None:
        mode = str(self.display_scaling.currentData() or "fit")
        for view in self.display_views.values():
            view.set_scaling_mode(mode)
        self.rfb_view.set_scaling_mode(mode)

    def toggle_vnc_share(self) -> None:
        name = self.selected_name()
        if self.vnc_share_server is not None:
            self.stop_vnc_share()
            return
        if not name or not self.vm_is_running(name):
            self.vnc_share_btn.setChecked(False)
            return
        local_port = self.display_ports.get(name)
        if local_port is None:
            port_path = INSTANCES_DIR / name / "vnc.port"
            try:
                local_port = int(port_path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                self.vnc_share_btn.setChecked(False)
                QMessageBox.information(
                    self,
                    "VNC sharing unavailable",
                    "Start this virtual machine with the embedded display before sharing it.",
                )
                return
        try:
            share_port = find_available_share_port()
            server = VncShareServer(
                "127.0.0.1",
                local_port,
                share_port,
                on_error=self.vnc_share_error.emit,
            )
            server.start()
        except VncShareError as exc:
            self.vnc_share_btn.setChecked(False)
            QMessageBox.warning(self, "Unable to share VNC", str(exc))
            return
        self.vnc_share_server = server
        self.vnc_share_name = name
        addresses = local_network_addresses() or ["this-mac.local"]
        endpoints = [f"{address}:{server.listen_port}" for address in addresses]
        connection_text = ", ".join(endpoints)
        self.vnc_share_btn.setChecked(True)
        self.vnc_share_btn.setToolTip(
            "Stop External VNC\n"
            f"Connect to {connection_text}\n"
            "No password is configured; use only on a trusted network."
        )
        self.vnc_share_btn.setAccessibleName("Stop external VNC sharing")
        self.status_task.setText(f"VNC shared at {connection_text}")

    def stop_vnc_share(self) -> None:
        server = self.vnc_share_server
        self.vnc_share_server = None
        self.vnc_share_name = None
        if server is not None:
            server.stop()
        if hasattr(self, "vnc_share_btn"):
            self.vnc_share_btn.setChecked(False)
            self.vnc_share_btn.setToolTip(
                "Share VNC on Local Network\n"
                "Temporarily exposes this display without a password."
            )
            self.vnc_share_btn.setAccessibleName("Share VNC on local network")
        if hasattr(self, "status_task") and not self.closing:
            self.status_task.setText("Ready")

    def sync_vnc_share_button(self) -> None:
        if not hasattr(self, "vnc_share_btn"):
            return
        selected = self.selected_name()
        active = self.vnc_share_server is not None and self.vnc_share_name == selected
        self.vnc_share_btn.setChecked(active)

    def handle_vnc_share_error(self, message: str) -> None:
        if self.closing:
            return
        self.append_log(f"[vnc-share] {message}")
        self.status_task.setText(message)

    def save_display_screenshot(self) -> None:
        name = self.selected_name() or "virtual-machine"
        default = str(Path.home() / "Desktop" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.png")
        path, _ = QFileDialog.getSaveFileName(self, "Save Display Screenshot", default, "PNG image (*.png)")
        if path and not self.rfb_view.save_screenshot(path):
            QMessageBox.information(self, "Screenshot unavailable", "The virtual display has not produced a framebuffer yet.")

    def send_host_clipboard(self) -> None:
        text = QApplication.clipboard().text()
        if not text:
            QMessageBox.information(self, "Clipboard is empty", "Copy text on the host, then send it to the virtual machine.")
            return
        self.rfb_view.send_clipboard(text)

    def open_or_start_console(self) -> None:
        name = self.selected_name()
        if not name:
            return
        if not self.vm_is_running(name):
            self.start_vm(False, True)
            return
        port_path = INSTANCES_DIR / name / "vnc.port"
        try:
            port = int(port_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            QMessageBox.information(
                self,
                "Embedded display unavailable",
                "This VM was started without the embedded display backend. Power it off, then power it on from EdgeOS Workstation to use the integrated console.",
            )
            return
        self.connect_display(name, port)
        self.rfb_view.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def config_for_name(self, name: str) -> dict[str, object] | None:
        path = INSTANCES_DIR / name / "vm.json"
        try:
            value = load_config(path)
        except (OSError, ConfigError):
            return None
        return value

    def vm_runtime_status(self, name: str | None) -> dict[str, object]:
        if not name:
            return {"running": False, "status": "shutdown", "qmp": False}
        instance = INSTANCES_DIR / name
        pid_path = instance / "qemu.pid"
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            cfg = self.config_for_name(name)
            suspend_value = str(cfg.get("suspend_state", "")).strip() if cfg else ""
            suspend_path = (instance / suspend_value) if suspend_value else None
            suspended = suspend_path is not None and suspend_path.is_file()
            return {
                "running": False,
                "status": "suspended" if suspended else "shutdown",
                "qmp": False,
                "pid": None,
            }
        qmp_path = instance / "qmp.sock"
        if qmp_path.exists():
            cached_status = self.qmp_states.get(name)
            if cached_status and cached_status != "disconnected":
                return {
                    "running": True,
                    "status": cached_status,
                    "qmp": True,
                    "pid": pid,
                }
            try:
                with QmpClient(qmp_path, timeout=0.25) as client:
                    status = client.query_status()
                status.update({"running": True, "qmp": True, "pid": pid})
                return status
            except (OSError, QmpError):
                pass
        return {"running": True, "status": "running", "qmp": False, "pid": pid}

    def vm_is_running(self, name: str) -> bool:
        return bool(self.vm_runtime_status(name).get("running"))

    def update_vm_summary(self, name: str, cfg: dict[str, object]) -> None:
        runtime = self.vm_runtime_status(name)
        running = bool(runtime.get("running"))
        runtime_state = str(runtime.get("status", "shutdown"))
        state_names = {
            "running": "Running",
            "paused": "Paused",
            "prelaunch": "Paused",
            "suspended": "Suspended",
            "shutdown": "Powered off",
            "inmigrate": "Migrating",
            "postmigrate": "Migrating",
        }
        state = state_names.get(runtime_state, runtime_state.replace("-", " ").title())
        networks = cfg.get("networks", [])
        network_text = "None"
        if isinstance(networks, list):
            modes = [str(net.get("type", "user")) for net in networks if isinstance(net, dict)]
            if modes:
                network_text = ", ".join(modes)
        disk_path = INSTANCES_DIR / name / str(cfg.get("rootfs_path", "rootfs.img"))
        try:
            disk_size = disk_path.stat().st_size
            disk_text = f"{disk_size / (1024 ** 3):.1f} GB"
        except OSError:
            disk_text = str(cfg.get("rootfs_size_mb", "—"))
            if disk_text != "—":
                disk_text += " MB"

        self.vm_title.setText(name)
        profile = str(cfg.get("profile", "EdgeOS"))
        self.vm_subtitle.setText(f"{state}  •  {profile} guest")
        self.detail_labels["architecture"].setText(str(cfg.get("architecture", "x86_64")))
        self.detail_labels["cpus"].setText(str(cfg.get("cpus", "—")))
        self.detail_labels["memory"].setText(str(cfg.get("memory", "—")))
        self.detail_labels["disk"].setText(disk_text)
        self.detail_labels["network"].setText(network_text)
        self.detail_labels["gpu"].setText(effective_gpu(cfg))
        resolution = self.pending_display_resolutions.get(
            name,
            str(cfg.get("display_resolution", "800x600")),
        )
        resolution_index = self.display_resolution.findData(resolution)
        custom_index = self.display_resolution.findData("custom")
        if custom_index >= 0:
            self.display_resolution.setItemText(custom_index, "Custom…")
        if resolution_index < 0 and custom_index >= 0:
            self.display_resolution.setItemText(
                custom_index, f"Custom: {resolution.replace('x', '×')}"
            )
            resolution_index = custom_index
        if resolution_index >= 0:
            self.display_resolution.blockSignals(True)
            self.display_resolution.setCurrentIndex(resolution_index)
            self.display_resolution.blockSignals(False)
        self.vm_path_label.setText(str(INSTANCES_DIR / name))
        if running:
            self.header_start.setText("▣  Open Console")
        elif runtime_state == "suspended":
            self.header_start.setText("▶  Resume")
        else:
            self.header_start.setText("▶  Power On")
        self.start_action.setEnabled(not running and self.process is None)
        self.stop_action.setEnabled(running and self.process is None)

        self.force_stop_action.setEnabled(running and self.process is None)
        self.reset_action.setEnabled(running and self.process is None)
        paused = runtime_state in ("paused", "prelaunch", "suspended")
        self.pause_action.setEnabled(running and not paused and self.process is None)
        self.resume_action.setEnabled(running and paused and self.process is None)
        try:
            self.header_start.clicked.disconnect()
        except TypeError:
            pass
        if running:
            self.header_start.clicked.connect(self.shutdown_vm)
        else:
            self.header_start.clicked.connect(self.open_or_start_console)
        self.status_name.setText(f"{name} — {state}")

    def display_resolution_selected(self) -> None:
        name = self.selected_name()
        if not name:
            return
        resolution = str(self.display_resolution.currentData() or "")
        if resolution and resolution != "custom":
            self.pending_display_resolutions[name] = resolution

    def apply_display_resolution(self) -> None:
        name = self.selected_name()
        if not name:
            return
        resolution = str(self.display_resolution.currentData() or "800x600")
        if resolution == "custom":
            cfg = self.config_for_name(name) or {}
            initial = str(cfg.get("display_resolution", "1920x1080"))
            value, accepted = QInputDialog.getText(
                self,
                "Custom Display Resolution",
                "Enter WIDTHxHEIGHT (320×200 to 7680×4320, maximum 128 MiB):",
                text=initial,
            )
            if not accepted:
                return
            try:
                resolution = normalize_display_resolution(value)
            except ConfigError as exc:
                QMessageBox.warning(self, "Invalid Resolution", str(exc))
                return
        self.pending_display_resolutions[name] = resolution
        self.run_cli(
            ["set-resolution", name, resolution],
            f"Set Display Resolution to {resolution}",
        )

    def refresh_runtime_state(self) -> None:
        if self.process is not None:
            return
        name = self.selected_name()
        if name:
            cfg = self.config_for_name(name)
            if cfg is not None:
                self.update_vm_summary(name, cfg)
            self.refresh_auxiliary_views(name)
        for index in range(self.library.topLevelItemCount()):
            item = self.library.topLevelItem(index)
            vm_name = str(item.data(0, Qt.ItemDataRole.UserRole))
            status = self.vm_runtime_status(vm_name)
            runtime_state = str(status.get("status", "shutdown"))
            item.setText(
                1,
                "Paused" if runtime_state in ("paused", "prelaunch", "suspended")
                else "Running" if status.get("running")
                else "Off",
            )
        self.sync_qmp_watchers()

    def sync_qmp_watchers(self) -> None:
        desired: dict[str, Path] = {}
        for config in INSTANCES_DIR.glob("*/vm.json") if INSTANCES_DIR.is_dir() else []:
            name = config.parent.name
            qmp_path = config.parent / "qmp-events.sock"
            if qmp_path.exists() and self.vm_is_running(name):
                desired[name] = qmp_path
        for name in list(self.qmp_watchers):
            if name in desired:
                continue
            watcher = self.qmp_watchers.pop(name)
            watcher.requestInterruption()
            watcher.wait(1000)
            watcher.deleteLater()
            self.qmp_states.pop(name, None)
        for name, qmp_path in desired.items():
            if name in self.qmp_watchers:
                continue
            watcher = QmpWatcher(name, qmp_path, self)
            watcher.state_changed.connect(self.qmp_state_changed)
            watcher.event_received.connect(self.qmp_event_received)
            self.qmp_watchers[name] = watcher
            watcher.start()

    def qmp_state_changed(self, name: str, status: str) -> None:
        self.qmp_states[name] = status
        if self.selected_name() == name:
            cfg = self.config_for_name(name)
            if cfg is not None:
                self.update_vm_summary(name, cfg)
        for index in range(self.library.topLevelItemCount()):
            item = self.library.topLevelItem(index)
            if str(item.data(0, Qt.ItemDataRole.UserRole)) != name:
                continue
            item.setText(
                1,
                "Paused" if status in ("paused", "prelaunch", "suspended")
                else "Off" if status in ("shutdown", "disconnected") and not self.vm_is_running(name)
                else "Running",
            )
            break

    def qmp_event_received(self, name: str, event: str) -> None:
        self.status_task.setText(f"{name}: {event.replace('_', ' ').title()}")

    def vm_configs(self) -> list[tuple[str, dict[str, object]]]:
        if not INSTANCES_DIR.is_dir():
            return []
        items: list[tuple[str, dict[str, object]]] = []
        for config in sorted(INSTANCES_DIR.glob("*/vm.json")):
            try:
                items.append((config.parent.name, load_config(config)))
            except (OSError, ConfigError) as exc:
                self.append_log(f"[warning] failed to read {config}: {exc}")
        return items

    def refresh(self) -> None:
        current = self.selected_name()
        rows = self.vm_configs()
        self.library.clear()
        selected_item: QTreeWidgetItem | None = None
        first_item: QTreeWidgetItem | None = None
        for name, cfg in rows:
            runtime = self.vm_runtime_status(name)
            runtime_value = str(runtime.get("status", "shutdown"))
            state = {
                "running": "Running",
                "paused": "Paused",
                "prelaunch": "Paused",
                "suspended": "Suspended",
                "shutdown": "Off",
            }.get(runtime_value, runtime_value.replace("-", " ").title())
            display_name = f"★  {name}" if name in self.favorites else name
            item = QTreeWidgetItem([display_name, state])
            item.setData(0, Qt.ItemDataRole.UserRole, name)
            item.setToolTip(0, f"{cfg.get('profile', 'edgeos')} • {cfg.get('architecture', 'x86_64')}")
            self.library.addTopLevelItem(item)
            if first_item is None:
                first_item = item
            if name == current:
                selected_item = item
        target = selected_item or first_item
        if target is not None:
            self.library.setCurrentItem(target)
        self.filter_library(self.search.text())
        self.update_actions()
        self.sync_qmp_watchers()

    def create_vm(self) -> None:
        dialog = CreateVmDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            args = dialog.command_args()
            if args:
                self.run_cli(args, "Create VM")

    def start_vm(self, force_tcg: bool, window: bool, embedded: bool = True) -> None:
        name = self.selected_name()
        if not name:
            return
        args = ["start", name]
        args.append("--background")
        vnc_port: int | None = None
        vnc_port_path = INSTANCES_DIR / name / "vnc.port"
        if window and embedded:
            try:
                vnc_port = self.find_available_vnc_port()
            except RuntimeError as exc:
                QMessageBox.warning(self, "Embedded display unavailable", str(exc))
                return
            vnc_port_path.write_text(f"{vnc_port}\n", encoding="ascii")
            display = f"vnc=127.0.0.1:{vnc_port - 5900}"
        else:
            vnc_port_path.unlink(missing_ok=True)
            display = "window" if window else "none"
        args += ["--display", display]
        if force_tcg:
            args += ["--accelerator", "tcg"]
        self.start_serial_tail(name)
        self.pending_serial_name = name
        self.pending_vnc_port = vnc_port
        cfg = self.config_for_name(name) or {}
        if window and embedded:
            self.start_authoritative_display_capture(
                name,
                persistent=str(cfg.get("desktop", "console")) != "xfce",
            )
        self.run_cli(args, "Start VM")

    def stop_vm(self) -> None:
        name = self.selected_name()
        if name:
            if self.vnc_share_name == name:
                self.stop_vnc_share()
            self.stop_authoritative_display_capture(name)
            self.serial_tail_name = None
            self.pending_serial_name = None
            self.pending_vnc_port = None
            self.close_serial_pty()
            self.disconnect_display()
            (INSTANCES_DIR / name / "vnc.port").unlink(missing_ok=True)
            self.run_cli(["stop", name, "--force"], "Stop VM")

    def shutdown_vm(self) -> None:
        name = self.selected_name()
        if not name:
            return
        self.status_task.setText("Waiting for guest shutdown")
        self.run_cli(["shutdown", name, "--timeout", "30"], "Shut Down Guest")

    def reset_vm(self) -> None:
        name = self.selected_name()
        if not name:
            return
        result = QMessageBox.question(
            self,
            "Reset virtual machine",
            f"Reset '{name}' immediately? Unsaved guest data may be lost.",
        )
        if result == QMessageBox.StandardButton.Yes:
            self.run_cli(["reset", name], "Reset VM")

    def pause_vm(self) -> None:
        name = self.selected_name()
        if name:
            self.run_cli(["pause", name], "Pause VM")

    def resume_vm(self) -> None:
        name = self.selected_name()
        if name:
            self.run_cli(["resume", name], "Resume VM")

    def suspend_vm(self) -> None:
        name = self.selected_name()
        if not name:
            return
        answer = QMessageBox.question(
            self,
            "Suspend Virtual Machine",
            f"Save the memory and device state of '{name}', then stop it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.run_cli(["suspend", name], "Suspend VM")

    def install_guest_tools(self) -> None:
        name = self.selected_name()
        if not name:
            return
        if not self.vm_is_running(name):
            QMessageBox.information(self, "Virtual machine is off", "Power on the virtual machine before installing Guest Tools.")
            return
        self.run_cli(["install-tools", name], "Install Guest Tools")

    def start_xfce4(self, _checked: bool = False, *, name: str | None = None, automatic: bool = False) -> None:
        name = name or self.selected_name()
        if not name:
            return
        if not self.vm_is_running(name):
            if not automatic:
                QMessageBox.information(self, "VM is powered off", "Power on the virtual machine before starting its desktop.")
            return
        if self.process is not None:
            return
        self.start_serial_tail(name)
        self.pending_serial_name = name
        self.start_authoritative_display_capture(name, persistent=False)
        self.run_cli(
            [
                "x11-run",
                name,
                "--desktop",
                "xfce",
                "--no-default-terminal",
                "--timeout",
                "420",
            ],
            "Restore XFCE Desktop" if automatic else "Start XFCE Desktop",
        )

    def update_kernel(self) -> None:
        name = self.selected_name()
        if name:
            cfg = self.config_for_name(name) or {}
            configured_jobs = int(cfg.get("build_jobs", 0))
            effective_jobs = configured_jobs or min(
                MAX_BUILD_JOBS,
                max(1, os.cpu_count() or 1),
            )
            self.run_cli(
                ["update-kernel", name],
                f"Update Kernel ({effective_jobs} jobs)",
            )

    def choose_transfer_host_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Host File", str(Path.home()))
        if path:
            self.transfer_host_path.setText(path)
            guest = self.transfer_guest_path.text().strip()
            if not guest or guest.endswith("/"):
                self.transfer_guest_path.setText((guest or "/tmp/") + Path(path).name)

    def push_file_to_guest(self) -> None:
        name = self.selected_name()
        host = self.transfer_host_path.text().strip()
        guest = self.transfer_guest_path.text().strip()
        if not name or not host or not guest:
            QMessageBox.information(self, "Transfer paths required", "Choose a host file and enter its destination path in the guest.")
            return
        if not self.vm_is_running(name):
            QMessageBox.information(self, "Virtual machine is off", "Power on the virtual machine before transferring files.")
            return
        self.run_cli(["guest-push", name, host, guest], "Send File to Guest")

    def pull_file_from_guest(self) -> None:
        name = self.selected_name()
        guest = self.transfer_guest_path.text().strip()
        if not name or not guest:
            QMessageBox.information(self, "Guest path required", "Enter the path of the file to receive from the guest.")
            return
        if not self.vm_is_running(name):
            QMessageBox.information(self, "Virtual machine is off", "Power on the virtual machine before transferring files.")
            return
        default = self.transfer_host_path.text().strip() or str(Path.home() / "Downloads" / Path(guest).name)
        host, _ = QFileDialog.getSaveFileName(self, "Save Guest File", default)
        if host:
            self.transfer_host_path.setText(host)
            self.run_cli(["guest-pull", name, guest, host], "Receive File from Guest")

    def snapshot_vm(self) -> None:
        name = self.selected_name()
        if not name:
            return
        if self.vm_is_running(name):
            QMessageBox.information(self, "Power off required", "Power off the virtual machine before creating a consistent disk snapshot.")
            return
        default = time.strftime("snapshot-%Y%m%d-%H%M%S")
        snap, ok = QInputDialog.getText(self, "Snapshot VM", "Snapshot name", text=default)
        if ok and snap.strip():
            description, description_ok = QInputDialog.getText(self, "Snapshot Description", "Description (optional)")
            if description_ok:
                self.run_cli(["snapshot", name, snap.strip(), "--description", description.strip()], "Snapshot VM")

    def selected_snapshot_name(self) -> str | None:
        selected = self.snapshots.selectedItems()
        if not selected:
            return None
        value = selected[0].data(0, Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def restore_snapshot(self) -> None:
        name = self.selected_name()
        snapshot = self.selected_snapshot_name()
        if not name or not snapshot:
            return
        if self.vm_is_running(name):
            QMessageBox.information(self, "Power off required", "Power off the virtual machine before restoring a disk snapshot.")
            return
        confirmation = QMessageBox(self)
        confirmation.setWindowTitle("Restore Snapshot")
        confirmation.setIcon(QMessageBox.Icon.Warning)
        confirmation.setText(f"Replace the current disks of '{name}' with snapshot '{snapshot}'?")
        confirmation.setInformativeText("Current disk contents will be replaced. This operation cannot be undone unless they are already in another snapshot.")
        restore_button = confirmation.addButton("Restore Snapshot", QMessageBox.ButtonRole.DestructiveRole)
        confirmation.addButton(QMessageBox.StandardButton.Cancel)
        confirmation.exec()
        if confirmation.clickedButton() is restore_button:
            self.run_cli(["restore", name, snapshot], "Restore Snapshot")

    def delete_snapshot(self) -> None:
        name = self.selected_name()
        snapshot = self.selected_snapshot_name()
        if not name or not snapshot:
            return
        answer = QMessageBox.question(
            self,
            "Delete Snapshot",
            f"Permanently delete snapshot '{snapshot}'?",
            QMessageBox.StandardButton.Delete | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Delete:
            self.run_cli(["snapshot-delete", name, snapshot], "Delete Snapshot")

    def clone_vm(self) -> None:
        name = self.selected_name()
        if not name:
            return
        clone, ok = QInputDialog.getText(self, "Clone VM", "New VM name", text=f"{name}-copy")
        if ok and clone.strip():
            mode = QMessageBox(self)
            mode.setWindowTitle("Clone Type")
            mode.setText("Choose how the virtual disks should be cloned.")
            mode.setInformativeText("A full clone is independent. A linked clone is faster and smaller but depends on the source disks.")
            full_button = mode.addButton("Full Clone", QMessageBox.ButtonRole.AcceptRole)
            linked_button = mode.addButton("Linked Clone", QMessageBox.ButtonRole.ActionRole)
            mode.addButton(QMessageBox.StandardButton.Cancel)
            mode.exec()
            if mode.clickedButton() is full_button:
                self.run_cli(["clone", name, clone.strip()], "Full Clone VM")
            elif mode.clickedButton() is linked_button:
                self.run_cli(["clone", name, clone.strip(), "--linked"], "Linked Clone VM")

    def show_config(self) -> None:
        name = self.selected_name()
        if not name:
            return
        cfg_path = INSTANCES_DIR / name / "vm.json"
        try:
            with cfg_path.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Config error", str(exc))
            return
        if ConfigDialog(name, cfg, cfg_path, self).exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def delete_vm(self) -> None:
        name = self.selected_name()
        if not name:
            return
        result = QMessageBox.question(self, "Delete VM", f"Delete VM '{name}' and its disk image?")
        if result == QMessageBox.StandardButton.Yes:
            self.run_cli(["delete", name, "--yes"], "Delete VM")

    def setup_tap(self) -> None:
        tap, ok = QInputDialog.getText(self, "Setup TAP", "TAP interface", text="tap0")
        if ok and tap.strip():
            self.run_cli(["tap", tap.strip()], "Setup TAP")

    def setup_macvtap(self) -> None:
        macvtap, ok = QInputDialog.getText(self, "Setup Macvtap", "Macvtap interface", text="edge-macvtap0")
        if not ok or not macvtap.strip():
            return
        parent, ok = QInputDialog.getText(self, "Setup Macvtap", "Parent interface (blank = default route)")
        args = ["macvtap", macvtap.strip()]
        if ok and parent.strip():
            args += ["--parent", parent.strip()]
        self.run_cli(args, "Setup Macvtap")


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
