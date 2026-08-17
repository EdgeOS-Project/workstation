#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) EdgeOS Contributors.
"""Runtime TCP forwarding for explicitly shared local QEMU VNC servers."""

from __future__ import annotations

import select
import socket
import threading
from collections.abc import Callable


class VncShareError(RuntimeError):
    """Raised when an external VNC listener cannot be started."""


class VncShareServer:
    """Forward external VNC clients to a loopback-only QEMU RFB endpoint."""

    def __init__(
        self,
        target_host: str,
        target_port: int,
        listen_port: int,
        *,
        listen_host: str = "0.0.0.0",
        connection_limit: int = 16,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.target_host = target_host
        self.target_port = target_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.connection_limit = connection_limit
        self.on_error = on_error
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._client_threads: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    @property
    def is_running(self) -> bool:
        thread = self._accept_thread
        return self._listener is not None and thread is not None and thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.listen_host, self.listen_port))
            listener.listen(self.connection_limit)
            listener.settimeout(0.25)
        except OSError as exc:
            listener.close()
            raise VncShareError(
                f"Unable to listen on {self.listen_host}:{self.listen_port}: {exc}"
            ) from exc
        self._listener = listener
        self.listen_port = int(listener.getsockname()[1])
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name=f"VncShare-{self.listen_port}",
            daemon=True,
        )
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        thread = self._accept_thread
        self._accept_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            client_threads = list(self._client_threads)
        for client_thread in client_threads:
            if client_thread is not threading.current_thread():
                client_thread.join(timeout=1.0)

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            listener = self._listener
            if listener is None:
                break
            try:
                external, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop_event.is_set():
                    self._report_error(f"External VNC listener stopped: {exc}")
                break
            with self._lock:
                active_count = len(self._client_threads)
            if active_count >= self.connection_limit:
                external.close()
                continue
            thread = threading.Thread(
                target=self._serve_client,
                args=(external,),
                name=f"VncShareClient-{self.listen_port}",
                daemon=True,
            )
            with self._lock:
                self._client_threads.add(thread)
            thread.start()

    def _serve_client(self, external: socket.socket) -> None:
        internal: socket.socket | None = None
        try:
            internal = socket.create_connection(
                (self.target_host, self.target_port), timeout=3.0
            )
            external.setblocking(False)
            internal.setblocking(False)
            with self._lock:
                self._connections.update((external, internal))
            self._relay(external, internal)
        except OSError as exc:
            if not self._stop_event.is_set():
                self._report_error(f"External VNC connection failed: {exc}")
        finally:
            for connection in (external, internal):
                if connection is None:
                    continue
                with self._lock:
                    self._connections.discard(connection)
                try:
                    connection.close()
                except OSError:
                    pass
            with self._lock:
                self._client_threads.discard(threading.current_thread())

    def _relay(self, external: socket.socket, internal: socket.socket) -> None:
        peer = {external: internal, internal: external}
        while not self._stop_event.is_set():
            readable, _writable, _exceptional = select.select(
                (external, internal), (), (), 0.25
            )
            for source in readable:
                try:
                    payload = source.recv(128 * 1024)
                except BlockingIOError:
                    continue
                if not payload:
                    return
                destination = peer[source]
                view = memoryview(payload)
                while view and not self._stop_event.is_set():
                    try:
                        sent = destination.send(view)
                    except BlockingIOError:
                        select.select((), (destination,), (), 0.25)
                        continue
                    if sent <= 0:
                        return
                    view = view[sent:]

    def _report_error(self, message: str) -> None:
        if self.on_error is not None:
            self.on_error(message)


def find_available_share_port(start: int = 6000, end: int = 6099) -> int:
    """Return an available all-interface TCP port for temporary VNC sharing."""
    for port in range(start, end + 1):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            probe.close()
            continue
        probe.close()
        return port
    raise VncShareError(f"No free external VNC port is available from {start} to {end}")


def local_network_addresses() -> list[str]:
    """Return stable non-loopback IPv4 addresses for connection instructions."""
    addresses: set[str] = set()
    try:
        candidates = socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        )
    except OSError:
        candidates = []
    for candidate in candidates:
        address = candidate[4][0]
        if not address.startswith("127.") and address != "0.0.0.0":
            addresses.add(address)
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        if not address.startswith("127."):
            addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)
