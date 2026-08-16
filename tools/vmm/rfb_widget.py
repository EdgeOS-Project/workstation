#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) EdgeOS Contributors.
"""Interactive RFB 3.8 display widget for QEMU virtual machines."""

from __future__ import annotations

import queue
import socket
import struct
import threading
import time
from typing import Final

try:
    from PyQt6.QtCore import QPointF, QSize, QThread, Qt, pyqtSignal as Signal
    from PyQt6.QtGui import QImage, QKeyEvent, QMouseEvent, QPainter, QWheelEvent
    from PyQt6.QtWidgets import QApplication, QWidget
except ImportError:
    from PySide6.QtCore import QPointF, QSize, QThread, Qt, Signal
    from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QPainter, QWheelEvent
    from PySide6.QtWidgets import QApplication, QWidget


RAW_ENCODING: Final = 0
DESKTOP_SIZE_ENCODING: Final = -223


class RfbProtocolError(RuntimeError):
    """Raised when an RFB server violates the negotiated protocol."""


class RfbClient(QThread):
    """Run a QEMU-compatible RFB connection outside the GUI thread."""

    connected = Signal(int, int, str)
    disconnected = Signal(str)
    frame_ready = Signal(QImage)
    clipboard_received = Signal(str)

    def __init__(self, host: str, port: int, parent=None) -> None:
        super().__init__(parent)
        self.host = host
        self.port = port
        self._stop = threading.Event()
        self._outgoing: queue.Queue[bytes] = queue.Queue()
        self._socket: socket.socket | None = None
        self._width = 0
        self._height = 0
        self._framebuffer = bytearray()
        self._blank_refresh_active = True
        self._blank_refresh_deadline = 0.0
        self._next_blank_refresh = 0.0

    def stop(self) -> None:
        self._stop.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def send_key(self, keysym: int, down: bool) -> None:
        self._outgoing.put(struct.pack(">BB2xI", 4, int(down), keysym & 0xFFFFFFFF))

    def send_pointer(self, button_mask: int, x: int, y: int) -> None:
        x = max(0, min(65535, x))
        y = max(0, min(65535, y))
        self._outgoing.put(struct.pack(">BBHH", 5, button_mask & 0xFF, x, y))

    def send_clipboard(self, text: str) -> None:
        data = text.encode("utf-8")
        self._outgoing.put(struct.pack(">B3xI", 6, len(data)) + data)

    def run(self) -> None:
        reason = "Display disconnected"
        try:
            self._connect_and_run()
            if self._stop.is_set():
                reason = "Display disconnected"
            else:
                reason = "QEMU closed the display connection"
        except (OSError, RfbProtocolError, ValueError) as exc:
            if not self._stop.is_set():
                reason = str(exc)
        finally:
            sock = self._socket
            self._socket = None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self.disconnected.emit(reason)

    def _connect_and_run(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=5.0)
        self._socket = sock
        self._handshake(sock)
        sock.settimeout(0.1)
        self._blank_refresh_active = True
        self._blank_refresh_deadline = time.monotonic() + 60.0
        self._next_blank_refresh = time.monotonic() + 0.25
        self._request_update(incremental=False)

        while not self._stop.is_set():
            self._flush_outgoing(sock)
            try:
                message_type = self._recv_exact(sock, 1, allow_timeout=True)[0]
            except TimeoutError:
                now = time.monotonic()
                if (
                    self._blank_refresh_active
                    and now < self._blank_refresh_deadline
                    and now >= self._next_blank_refresh
                ):
                    self._request_update(incremental=False)
                    self._flush_outgoing(sock)
                    self._next_blank_refresh = now + 0.25
                elif self._blank_refresh_active and now >= self._blank_refresh_deadline:
                    self._blank_refresh_active = False
                    self._request_update(incremental=True)
                    self._flush_outgoing(sock)
                continue
            if message_type == 0:
                blank_frame = self._read_framebuffer_update(sock)
                now = time.monotonic()
                if blank_frame and now < self._blank_refresh_deadline:
                    self._blank_refresh_active = True
                    self._next_blank_refresh = now + 0.25
                else:
                    self._blank_refresh_active = False
                    self._request_update(incremental=True)
            elif message_type == 2:
                continue
            elif message_type == 3:
                self._read_server_clipboard(sock)
            else:
                raise RfbProtocolError(f"Unsupported RFB server message {message_type}")

    def _handshake(self, sock: socket.socket) -> None:
        version = self._recv_exact(sock, 12)
        if not version.startswith(b"RFB 003."):
            raise RfbProtocolError("The display server did not provide an RFB version")
        sock.sendall(b"RFB 003.008\n")

        count = self._recv_exact(sock, 1)[0]
        if count == 0:
            length = struct.unpack(">I", self._recv_exact(sock, 4))[0]
            reason = self._recv_exact(sock, length).decode("utf-8", errors="replace")
            raise RfbProtocolError(reason or "RFB server rejected the connection")
        security_types = self._recv_exact(sock, count)
        if 1 not in security_types:
            raise RfbProtocolError("QEMU VNC authentication is enabled but no password was supplied")
        sock.sendall(b"\x01")
        security_result = struct.unpack(">I", self._recv_exact(sock, 4))[0]
        if security_result != 0:
            raise RfbProtocolError("RFB security negotiation failed")

        sock.sendall(b"\x01")
        server_init = self._recv_exact(sock, 24)
        width, height = struct.unpack(">HH", server_init[:4])
        name_length = struct.unpack(">I", server_init[20:24])[0]
        name = self._recv_exact(sock, name_length).decode("utf-8", errors="replace")
        self._resize_framebuffer(width, height)

        pixel_format = struct.pack(
            ">B3xBBBBHHHBBB3x",
            0,
            32,
            24,
            0,
            1,
            255,
            255,
            255,
            16,
            8,
            0,
        )
        sock.sendall(pixel_format)
        encodings = (RAW_ENCODING, DESKTOP_SIZE_ENCODING)
        sock.sendall(struct.pack(">BBH", 2, 0, len(encodings)))
        sock.sendall(b"".join(struct.pack(">i", encoding) for encoding in encodings))
        self.connected.emit(width, height, name)

    def _read_framebuffer_update(self, sock: socket.socket) -> bool:
        _, rectangle_count = struct.unpack(">BH", self._recv_exact(sock, 3))
        changed = False
        for _ in range(rectangle_count):
            x, y, width, height, encoding = struct.unpack(">HHHHi", self._recv_exact(sock, 12))
            if encoding == RAW_ENCODING:
                self._read_raw_rectangle(sock, x, y, width, height)
                changed = True
            elif encoding == DESKTOP_SIZE_ENCODING:
                self._resize_framebuffer(width, height)
                changed = True
            else:
                raise RfbProtocolError(f"QEMU selected unrequested RFB encoding {encoding}")
        if changed and self._width and self._height:
            image = QImage(
                bytes(self._framebuffer),
                self._width,
                self._height,
                self._width * 4,
                QImage.Format.Format_RGB32,
            ).copy()
            self.frame_ready.emit(image)
            return self._framebuffer_is_blank_white()
        return False

    def _framebuffer_is_blank_white(self) -> bool:
        """Detect the all-white ramfb transition that can precede firmware output."""
        if not self._framebuffer or not self._width or not self._height:
            return False
        columns = min(9, self._width)
        rows = min(7, self._height)
        for row in range(rows):
            y = row * (self._height - 1) // max(1, rows - 1)
            for column in range(columns):
                x = column * (self._width - 1) // max(1, columns - 1)
                offset = (y * self._width + x) * 4
                blue, green, red = self._framebuffer[offset:offset + 3]
                if red < 248 or green < 248 or blue < 248:
                    return False
        return True

    def _read_raw_rectangle(
        self,
        sock: socket.socket,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        if x + width > self._width or y + height > self._height:
            raise RfbProtocolError("RFB update extends beyond the framebuffer")
        row_bytes = width * 4
        data = self._recv_exact(sock, row_bytes * height)
        for row in range(height):
            source = row * row_bytes
            destination = ((y + row) * self._width + x) * 4
            self._framebuffer[destination:destination + row_bytes] = data[source:source + row_bytes]

    def _read_server_clipboard(self, sock: socket.socket) -> None:
        length = struct.unpack(">I", self._recv_exact(sock, 7)[3:])[0]
        data = self._recv_exact(sock, length)
        self.clipboard_received.emit(data.decode("utf-8", errors="replace"))

    def _resize_framebuffer(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0 or width > 16384 or height > 16384:
            raise RfbProtocolError(f"Invalid framebuffer dimensions {width}x{height}")
        self._width = width
        self._height = height
        self._framebuffer = bytearray(width * height * 4)

    def _request_update(self, incremental: bool) -> None:
        self._outgoing.put(
            struct.pack(">BBHHHH", 3, int(incremental), 0, 0, self._width, self._height)
        )

    def _flush_outgoing(self, sock: socket.socket) -> None:
        while True:
            try:
                message = self._outgoing.get_nowait()
            except queue.Empty:
                return
            sock.sendall(message)

    def _recv_exact(self, sock: socket.socket, length: int, allow_timeout: bool = False) -> bytes:
        data = bytearray()
        while len(data) < length:
            if self._stop.is_set():
                raise OSError("Display connection stopped")
            try:
                chunk = sock.recv(length - len(data))
            except socket.timeout as exc:
                if allow_timeout and not data:
                    raise TimeoutError from exc
                continue
            if not chunk:
                raise OSError("QEMU closed the display connection")
            data.extend(chunk)
        return bytes(data)


KEYSYMS: Final[dict[int, int]] = {
    int(Qt.Key.Key_Backspace): 0xFF08,
    int(Qt.Key.Key_Tab): 0xFF09,
    int(Qt.Key.Key_Return): 0xFF0D,
    int(Qt.Key.Key_Enter): 0xFF0D,
    int(Qt.Key.Key_Escape): 0xFF1B,
    int(Qt.Key.Key_Insert): 0xFF63,
    int(Qt.Key.Key_Delete): 0xFFFF,
    int(Qt.Key.Key_Home): 0xFF50,
    int(Qt.Key.Key_End): 0xFF57,
    int(Qt.Key.Key_PageUp): 0xFF55,
    int(Qt.Key.Key_PageDown): 0xFF56,
    int(Qt.Key.Key_Left): 0xFF51,
    int(Qt.Key.Key_Up): 0xFF52,
    int(Qt.Key.Key_Right): 0xFF53,
    int(Qt.Key.Key_Down): 0xFF54,
    int(Qt.Key.Key_Shift): 0xFFE1,
    int(Qt.Key.Key_Control): 0xFFE3,
    int(Qt.Key.Key_Meta): 0xFFEB,
    int(Qt.Key.Key_Alt): 0xFFE9,
    int(Qt.Key.Key_CapsLock): 0xFFE5,
    int(Qt.Key.Key_NumLock): 0xFF7F,
    int(Qt.Key.Key_ScrollLock): 0xFF14,
    int(Qt.Key.Key_Pause): 0xFF13,
    int(Qt.Key.Key_Print): 0xFF61,
}
for _index in range(1, 36):
    KEYSYMS[int(getattr(Qt.Key, f"Key_F{_index}"))] = 0xFFBD + _index


class RfbView(QWidget):
    """Paint an RFB framebuffer and forward local input to QEMU."""

    connected = Signal(int, int, str)
    disconnected = Signal(str)
    frame_updated = Signal(QImage)
    scale_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._client: RfbClient | None = None
        self._image = QImage()
        self._authoritative_image = QImage()
        self._button_mask = 0
        self._display_rect = self.rect()
        self._status = "Power on this virtual machine to open the display"
        self._scaling_mode = "fit"
        self._reported_scale_percent = -1
        self._render_cache_key: tuple[int, int, int, int, str] | None = None
        self._render_cache = QImage()

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    def set_scaling_mode(self, mode: str) -> None:
        if mode not in ("fit", "integer", "actual", "stretch"):
            raise ValueError(f"unsupported display scaling mode: {mode}")
        self._scaling_mode = mode
        self._clear_render_cache()
        self.update()

    def save_screenshot(self, path: str) -> bool:
        image = self._visible_image()
        return not image.isNull() and image.save(path)

    def set_authoritative_frame(self, image: QImage) -> None:
        """Display a QMP-sourced frame while ramfb VNC updates are unreliable."""
        self._authoritative_image = image.copy()
        self._clear_render_cache()
        self.update()

    def clear_authoritative_frame(self) -> None:
        self._authoritative_image = QImage()
        self._clear_render_cache()
        self.update()

    @staticmethod
    def frame_has_visual_detail(image: QImage) -> bool:
        """Return whether an RFB frame contains more than a blank boot surface."""
        if image.isNull():
            return False
        sample = image.scaled(
            32,
            24,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        colors: set[int] = set()
        for y in range(sample.height()):
            for x in range(sample.width()):
                colors.add(sample.pixel(x, y))
                if len(colors) >= 5:
                    return True
        return False

    @staticmethod
    def clear_fit_size(image_size, available_size):
        """Shrink oversized frames while preserving native pixels when they fit."""
        if (
            image_size.width() <= available_size.width()
            and image_size.height() <= available_size.height()
        ):
            return image_size
        scale = min(
            available_size.width() / max(1, image_size.width()),
            available_size.height() / max(1, image_size.height()),
        )
        return QSize(
            max(1, round(image_size.width() * scale)),
            max(1, round(image_size.height() * scale)),
        )

    @staticmethod
    def integer_fit_size(image_size, available_size):
        """Fit with an integer zoom factor or an integer reciprocal."""
        source_width = max(1, image_size.width())
        source_height = max(1, image_size.height())
        available_width = max(1, available_size.width())
        available_height = max(1, available_size.height())
        if source_width <= available_width and source_height <= available_height:
            multiplier = max(
                1,
                min(
                    available_width // source_width,
                    available_height // source_height,
                ),
            )
            return image_size * multiplier
        divisor = max(
            1,
            (source_width + available_width - 1) // available_width,
            (source_height + available_height - 1) // available_height,
        )
        return image_size.scaled(
            max(1, source_width // divisor),
            max(1, source_height // divisor),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

    @staticmethod
    def transformation_mode_for_target(
        image: QImage,
        physical_width: int,
        physical_height: int,
        scaling_mode: str,
    ):
        """Choose predictable filtering for guest text and desktop graphics."""
        if scaling_mode == "integer":
            return Qt.TransformationMode.FastTransformation
        source_width = max(1, image.width())
        source_height = max(1, image.height())
        downscaling = (
            physical_width < source_width or physical_height < source_height
        )
        integer_upscale = (
            physical_width >= source_width
            and physical_height >= source_height
            and physical_width % source_width == 0
            and physical_height % source_height == 0
        )
        if downscaling or not integer_upscale:
            return Qt.TransformationMode.SmoothTransformation
        return Qt.TransformationMode.FastTransformation

    @staticmethod
    def scale_for_device_pixels(
        image: QImage,
        logical_width: int,
        logical_height: int,
        device_ratio: float,
        scaling_mode: str = "fit",
    ) -> QImage:
        """Render once at final device pixels without compositor resampling."""
        ratio = max(1.0, device_ratio)
        physical_width = max(1, round(logical_width * ratio))
        physical_height = max(1, round(logical_height * ratio))
        if image.width() == physical_width and image.height() == physical_height:
            rendered = image.copy()
        else:
            transformation = RfbView.transformation_mode_for_target(
                image,
                physical_width,
                physical_height,
                scaling_mode,
            )
            rendered = image.scaled(
                physical_width,
                physical_height,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                transformation,
            )
        rendered.setDevicePixelRatio(ratio)
        return rendered

    def send_clipboard(self, text: str) -> None:
        if self._client is not None:
            self._client.send_clipboard(text)

    def connect_to(self, host: str, port: int) -> None:
        self.disconnect_from_server()
        self._status = f"Connecting to display on {host}:{port}…"
        self.update()
        client = RfbClient(host, port, self)
        client.connected.connect(self._on_connected)
        client.disconnected.connect(self._on_disconnected)
        client.frame_ready.connect(self._on_frame)
        client.clipboard_received.connect(self._on_clipboard)
        self._client = client
        client.start()

    def disconnect_from_server(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            client.stop()
            client.wait(1500)
            client.deleteLater()
        self._image = QImage()
        self._authoritative_image = QImage()
        self._clear_render_cache()
        self.update()

    def show_placeholder(self, message: str) -> None:
        """Disconnect the active session and show a stable display state."""
        self.disconnect_from_server()
        self._status = message
        self.update()

    def send_ctrl_alt_delete(self) -> None:
        client = self._client
        if client is None:
            return
        for keysym in (0xFFE3, 0xFFE9, 0xFFFF):
            client.send_key(keysym, True)
        for keysym in (0xFFFF, 0xFFE9, 0xFFE3):
            client.send_key(keysym, False)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        image = self._visible_image()
        if image.isNull():
            painter.setPen(Qt.GlobalColor.lightGray)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status)
            return
        if self._scaling_mode == "stretch":
            scaled = self.size()
        elif self._scaling_mode == "actual":
            scaled = image.size()
        elif self._scaling_mode == "integer":
            scaled = self.integer_fit_size(image.size(), self.size())
        else:
            scaled = self.clear_fit_size(image.size(), self.size())
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        self._display_rect = self.rect().adjusted(x, y, -(self.width() - x - scaled.width()), -(self.height() - y - scaled.height()))
        scale_percent = max(
            1,
            round(
                min(
                    self._display_rect.width() / max(1, image.width()),
                    self._display_rect.height() / max(1, image.height()),
                )
                * 100
            ),
        )
        if scale_percent != self._reported_scale_percent:
            self._reported_scale_percent = scale_percent
            self.scale_changed.emit(scale_percent)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        device_ratio = max(1.0, painter.device().devicePixelRatioF())
        physical_width = max(1, round(self._display_rect.width() * device_ratio))
        physical_height = max(1, round(self._display_rect.height() * device_ratio))
        cache_key = (
            int(image.cacheKey()),
            physical_width,
            physical_height,
            round(device_ratio * 1000),
            self._scaling_mode,
        )
        if self._render_cache_key != cache_key:
            self._render_cache = self.scale_for_device_pixels(
                image,
                self._display_rect.width(),
                self._display_rect.height(),
                device_ratio,
                self._scaling_mode,
            )
            self._render_cache_key = cache_key
        painter.drawImage(
            QPointF(self._display_rect.x(), self._display_rect.y()),
            self._render_cache,
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        self._send_key_event(event, True)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        self._send_key_event(event, False)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._button_mask |= self._mouse_button_mask(event.button())
        self._send_pointer(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._button_mask &= ~self._mouse_button_mask(event.button())
        self._send_pointer(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._send_pointer(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        client = self._client
        point = self._remote_point(event.position().x(), event.position().y())
        if client is None or point is None:
            return
        button = 8 if event.angleDelta().y() > 0 else 16
        client.send_pointer(self._button_mask | button, *point)
        client.send_pointer(self._button_mask, *point)
        event.accept()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.disconnect_from_server()
        super().closeEvent(event)

    def _send_key_event(self, event: QKeyEvent, down: bool) -> None:
        client = self._client
        if client is None or event.isAutoRepeat():
            return
        keysym = KEYSYMS.get(int(event.key()))
        key = int(event.key())
        if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
            keysym = ord("a") + key - int(Qt.Key.Key_A)
        elif int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
            keysym = ord("0") + key - int(Qt.Key.Key_0)
        text = event.text()
        if keysym is None and text:
            codepoint = ord(text[0])
            keysym = codepoint if codepoint <= 0xFF else 0x01000000 | codepoint
        if keysym is not None:
            client.send_key(keysym, down)
            event.accept()

    def _send_pointer(self, event: QMouseEvent) -> None:
        client = self._client
        point = self._remote_point(event.position().x(), event.position().y())
        if client is not None and point is not None:
            client.send_pointer(self._button_mask, *point)
            event.accept()

    def _remote_point(self, x: float, y: float) -> tuple[int, int] | None:
        image = self._visible_image()
        if image.isNull() or not self._display_rect.contains(int(x), int(y)):
            return None
        remote_x = int((x - self._display_rect.x()) * image.width() / self._display_rect.width())
        remote_y = int((y - self._display_rect.y()) * image.height() / self._display_rect.height())
        return min(remote_x, image.width() - 1), min(remote_y, image.height() - 1)

    def _visible_image(self) -> QImage:
        if not self._authoritative_image.isNull():
            return self._authoritative_image
        return self._image

    def _clear_render_cache(self) -> None:
        self._render_cache_key = None
        self._render_cache = QImage()

    @staticmethod
    def _mouse_button_mask(button) -> int:
        if button == Qt.MouseButton.LeftButton:
            return 1
        if button == Qt.MouseButton.MiddleButton:
            return 2
        if button == Qt.MouseButton.RightButton:
            return 4
        return 0

    def _on_connected(self, width: int, height: int, name: str) -> None:
        if self.sender() is not self._client:
            return
        self._status = f"Connected to {name} ({width}×{height})"
        self.connected.emit(width, height, name)
        self.update()

    def _on_disconnected(self, reason: str) -> None:
        if self.sender() is not self._client:
            return
        self._client = None
        self._status = reason
        self.disconnected.emit(reason)
        self.update()

    def _on_frame(self, image: QImage) -> None:
        if self.sender() is not self._client:
            return
        self._image = image
        self._clear_render_cache()
        self.frame_updated.emit(image)
        self.update()

    def _on_clipboard(self, text: str) -> None:
        if self.sender() is not self._client:
            return
        QApplication.clipboard().setText(text)
