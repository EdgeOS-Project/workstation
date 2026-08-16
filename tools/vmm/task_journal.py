#!/usr/bin/env python3
"""Persistent operation journal for EdgeOS Workstation tasks."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}


class TaskJournal:
    """Store task lifecycle data in one atomic, recoverable JSON document."""

    def __init__(self, path: Path, max_entries: int = 200) -> None:
        self.path = path
        self.max_entries = max_entries
        self.tasks = self._load()
        self._dirty = False

    def _load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return []
        tasks = value.get("tasks", [])
        return [task for task in tasks if isinstance(task, dict)]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "updated_at": int(time.time()),
            "tasks": self.tasks[-self.max_entries :],
        }
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._dirty = False
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def create(self, title: str, command: list[str], vm_name: str | None) -> dict[str, Any]:
        now = int(time.time())
        task = {
            "id": uuid.uuid4().hex,
            "title": title,
            "command": list(command),
            "vm_name": vm_name,
            "state": "queued",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "pid": None,
            "exit_code": None,
            "output": "",
        }
        self.tasks.append(task)
        self._save()
        return task

    def update(self, task_id: str, **values: Any) -> dict[str, Any] | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.update(values)
        self._save()
        return task

    def append_output(
        self,
        task_id: str,
        text: str,
        limit: int = 256 * 1024,
        *,
        persist: bool = True,
    ) -> None:
        task = self.get(task_id)
        if task is None or not text:
            return
        task["output"] = (str(task.get("output", "")) + text)[-limit:]
        self._dirty = True
        if persist:
            self._save()

    def flush(self) -> None:
        """Persist output accumulated by non-persistent append operations."""
        if self._dirty:
            self._save()

    def get(self, task_id: str) -> dict[str, Any] | None:
        return next((task for task in self.tasks if task.get("id") == task_id), None)

    def recover_interrupted(self) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        now = int(time.time())
        for task in self.tasks:
            if task.get("state") not in {"queued", "running", "cancelling"}:
                continue
            pid = task.get("pid")
            if isinstance(pid, int) and self._pid_is_alive(pid):
                continue
            task.update(
                {
                    "state": "interrupted",
                    "finished_at": now,
                    "exit_code": None,
                    "pid": None,
                }
            )
            recovered.append(task)
        if recovered:
            self._save()
        return recovered

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def clear_finished(self) -> int:
        before = len(self.tasks)
        self.tasks = [task for task in self.tasks if task.get("state") not in TERMINAL_STATES]
        removed = before - len(self.tasks)
        if removed:
            self._save()
        return removed
