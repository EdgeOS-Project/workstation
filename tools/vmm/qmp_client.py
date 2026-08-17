#!/usr/bin/env python3
"""QEMU Machine Protocol client for EdgeOS Workstation.

This is original EdgeOS code licensed under MPL-2.0.
"""

from __future__ import annotations

import json
import select
import socket
import time
from pathlib import Path
from typing import Any


class QmpError(RuntimeError):
    """Base QMP transport or command error."""


class QmpCommandError(QmpError):
    """A command reached QEMU but QEMU rejected it."""

    def __init__(self, command: str, error: dict[str, Any]) -> None:
        description = str(error.get("desc", error))
        super().__init__(f"QMP command {command!r} failed: {description}")
        self.command = command
        self.error = error


class QmpClient:
    """Synchronous, request-correlated QMP connection over a Unix socket."""

    def __init__(self, path: Path | str, timeout: float = 3.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._reader = None
        self._next_id = 1
        self.events: list[dict[str, Any]] = []

    def __enter__(self) -> "QmpClient":
        self.connect()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def connect(self) -> None:
        if self._socket is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.path))
            self._socket = sock
            self._reader = sock.makefile("rb")
            greeting = self._read_message()
            if "QMP" not in greeting:
                raise QmpError(f"socket {self.path} is not a QMP endpoint")
            self.execute("qmp_capabilities")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        reader = self._reader
        self._reader = None
        if reader is not None:
            reader.close()
        sock = self._socket
        self._socket = None
        if sock is not None:
            sock.close()

    def execute(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        if self._socket is None:
            self.connect()
        request_id = self._next_id
        self._next_id += 1
        request: dict[str, Any] = {"execute": command, "id": request_id}
        if arguments:
            request["arguments"] = arguments
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\r\n"
        assert self._socket is not None
        self._socket.sendall(payload)
        while True:
            response = self._read_message()
            if "event" in response:
                self.events.append(response)
                continue
            if response.get("id") != request_id:
                continue
            error = response.get("error")
            if isinstance(error, dict):
                raise QmpCommandError(command, error)
            if "return" not in response:
                raise QmpError(f"malformed QMP response to {command!r}: {response}")
            return response["return"]

    def query_status(self) -> dict[str, Any]:
        result = self.execute("query-status")
        if not isinstance(result, dict):
            raise QmpError(f"unexpected query-status result: {result!r}")
        return result

    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Return the next asynchronous QMP event without issuing a command."""
        if self._socket is None:
            self.connect()
        assert self._socket is not None
        readable, _, _ = select.select([self._socket], [], [], timeout)
        if not readable:
            return None
        while True:
            message = self._read_message()
            if "event" in message:
                self.events.append(message)
                return message

    def _read_message(self) -> dict[str, Any]:
        if self._reader is None:
            raise QmpError("QMP connection is not open")
        while True:
            line = self._reader.readline()
            if not line:
                raise QmpError(f"QMP socket {self.path} closed unexpectedly")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise QmpError(f"invalid QMP JSON from {self.path}: {line!r}") from exc
            if isinstance(message, dict):
                return message


def wait_for_qmp(path: Path | str, timeout: float = 5.0) -> dict[str, Any]:
    """Wait for a QMP endpoint and return its first authoritative status."""
    qmp_path = Path(path)
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if qmp_path.exists():
            try:
                with QmpClient(qmp_path, timeout=min(1.0, timeout)) as client:
                    return client.query_status()
            except (OSError, QmpError) as exc:
                last_error = exc
        time.sleep(0.05)
    detail = f": {last_error}" if last_error is not None else ""
    raise QmpError(f"QMP endpoint did not become ready at {qmp_path}{detail}")


def legacy_hmp_command(path: Path | str, command: str, timeout: float = 2.0) -> str:
    """Send a command to VMs started before the QMP migration."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
        try:
            sock.recv(65536)
        except TimeoutError:
            pass
        sock.sendall(command.encode("ascii") + b"\n")
        chunks: list[bytes] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = sock.recv(65536)
            except TimeoutError:
                break
            if not data:
                break
            chunks.append(data)
            if b"(qemu)" in data:
                break
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        sock.close()
