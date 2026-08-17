#!/usr/bin/env python3
"""Local HTTP and Unix-socket control service for EdgeOS Workstation."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socketserver
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from paths import STATE_ROOT, WORKSTATION_ROOT


REPO_ROOT = WORKSTATION_ROOT
CLI = REPO_ROOT / "tools/vmm/edgeos_vm.py"
INSTANCES_DIR = STATE_ROOT / "instances"


class WorkstationApi:
    """Expose a small versioned control surface shared by HTTP and RPC."""

    ACTIONS = {
        "start": lambda name: ["start", name, "--background", "--display", "none"],
        "shutdown": lambda name: ["shutdown", name, "--timeout", "30"],
        "stop": lambda name: ["stop", name, "--force"],
        "reset": lambda name: ["reset", name],
        "pause": lambda name: ["pause", name],
        "resume": lambda name: ["resume", name],
        "suspend": lambda name: ["suspend", name],
    }

    def dispatch(self, method: str, path: str, _body: dict[str, Any] | None = None) -> tuple[int, Any]:
        parts = [unquote(part) for part in urlparse(path).path.split("/") if part]
        if method == "GET" and parts == ["api", "v1", "vms"]:
            return HTTPStatus.OK, self.list_vms()
        if len(parts) == 4 and parts[:3] == ["api", "v1", "vms"] and method == "GET":
            return self.vm_status(parts[3])
        if len(parts) == 6 and parts[:3] == ["api", "v1", "vms"] and parts[4] == "actions" and method == "POST":
            return self.run_action(parts[3], parts[5])
        return HTTPStatus.NOT_FOUND, {"error": "endpoint not found"}

    def list_vms(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for config in sorted(INSTANCES_DIR.glob("*/vm.json")) if INSTANCES_DIR.is_dir() else []:
            status_code, status = self.vm_status(config.parent.name)
            if status_code == HTTPStatus.OK:
                result.append(status)
        return result

    def vm_status(self, name: str) -> tuple[int, dict[str, Any]]:
        if not self._valid_vm(name):
            return HTTPStatus.NOT_FOUND, {"error": "virtual machine not found"}
        result = self._run(["status", name, "--json"])
        if result.returncode != 0:
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": result.stderr.strip() or result.stdout.strip()}
        status = json.loads(result.stdout)
        status["name"] = name
        try:
            config = json.loads((INSTANCES_DIR / name / "vm.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {}
        status["architecture"] = config.get("architecture", "unknown")
        status["memory"] = config.get("memory", "unknown")
        status["cpus"] = config.get("cpus", 0)
        return HTTPStatus.OK, status

    def run_action(self, name: str, action: str) -> tuple[int, dict[str, Any]]:
        if not self._valid_vm(name):
            return HTTPStatus.NOT_FOUND, {"error": "virtual machine not found"}
        builder = self.ACTIONS.get(action)
        if builder is None:
            return HTTPStatus.BAD_REQUEST, {"error": "unsupported action"}
        result = self._run(builder(name))
        payload = {
            "action": action,
            "name": name,
            "exit_code": result.returncode,
            "output": (result.stdout + result.stderr).strip(),
        }
        return (HTTPStatus.OK if result.returncode == 0 else HTTPStatus.CONFLICT), payload

    @staticmethod
    def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [os.environ.get("PYTHON", "python3"), str(CLI), *arguments],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def _valid_vm(name: str) -> bool:
        return bool(name) and "/" not in name and name not in (".", "..") and (INSTANCES_DIR / name / "vm.json").is_file()


WEB_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EdgeOS Workstation</title><style>
body{margin:0;background:#171c21;color:#e8eef2;font:14px -apple-system,BlinkMacSystemFont,sans-serif}header{padding:20px 28px;background:#242c33;border-bottom:1px solid #3b4650}h1{margin:0;font-size:22px}main{padding:24px;display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}.vm{background:#252d34;border:1px solid #3f4a53;border-radius:9px;padding:18px}.state{color:#83c5f1;text-transform:uppercase;font-size:11px;font-weight:700}.meta{color:#aab7c0;margin:8px 0 16px}button{background:#316f9e;color:white;border:1px solid #4b83ad;border-radius:5px;padding:7px 10px;margin:3px;cursor:pointer}button.danger{background:#8d3b3b;border-color:#aa5555}</style></head>
<body><header><h1>EdgeOS Workstation</h1></header><main id="vms"></main><script>
async function action(name,act){await fetch(`/api/v1/vms/${encodeURIComponent(name)}/actions/${act}`,{method:'POST'});refresh()}
async function refresh(){const vms=await (await fetch('/api/v1/vms')).json();document.querySelector('#vms').innerHTML=vms.map(v=>`<section class="vm"><div class="state">${v.status}</div><h2>${v.name}</h2><div class="meta">${v.architecture} · ${v.cpus} vCPU · ${v.memory}</div>${v.running?`<button onclick="action('${v.name}','shutdown')">Shut Down</button><button onclick="action('${v.name}','pause')">Pause</button><button onclick="action('${v.name}','suspend')">Suspend</button><button class="danger" onclick="action('${v.name}','stop')">Force Off</button>`:`<button onclick="action('${v.name}','start')">Power On</button>`}</section>`).join('')}
refresh();setInterval(refresh,3000)</script></body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    api: WorkstationApi

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/":
            self._send(HTTPStatus.OK, WEB_PAGE, "text/html; charset=utf-8")
            return
        status, payload = self.api.dispatch("GET", self.path)
        self._send(status, json.dumps(payload), "application/json")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send(HTTPStatus.BAD_REQUEST, '{"error":"invalid JSON"}', "application/json")
            return
        status, payload = self.api.dispatch("POST", self.path, body)
        self._send(status, json.dumps(payload), "application/json")

    def _send(self, status: int, data: str, content_type: str) -> None:
        encoded = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"http: {format % args}")


class RpcHandler(socketserver.StreamRequestHandler):
    api: WorkstationApi

    def handle(self) -> None:
        for line in self.rfile:
            try:
                request = json.loads(line)
                status, payload = self.api.dispatch(str(request.get("method", "GET")), str(request.get("path", "/")), request.get("body"))
                response = {"status": int(status), "body": payload}
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                response = {"status": 400, "body": {"error": str(exc)}}
            self.wfile.write(json.dumps(response).encode("utf-8") + b"\n")


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", default="127.0.0.1:8765", help="HTTP listen address")
    parser.add_argument("--rpc-socket", type=Path, default=Path(f"/tmp/edgeos-workstation-{os.getuid()}.sock"))
    args = parser.parse_args()
    host, port_text = args.http.rsplit(":", 1)
    api = WorkstationApi()
    HttpHandler.api = api
    RpcHandler.api = api
    args.rpc_socket.unlink(missing_ok=True)
    http = ThreadingHTTPServer((host, int(port_text)), HttpHandler)
    rpc = ThreadingUnixServer(str(args.rpc_socket), RpcHandler)
    rpc_thread = threading.Thread(target=rpc.serve_forever, daemon=True)
    rpc_thread.start()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    print(f"Web console: http://{host}:{port_text}")
    print(f"RPC socket: {args.rpc_socket}")
    http.timeout = 0.5
    try:
        while not stop.is_set():
            http.handle_request()
    finally:
        http.server_close()
        rpc.shutdown()
        rpc.server_close()
        args.rpc_socket.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
