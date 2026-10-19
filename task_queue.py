"""Durable, single-host task queue for the coding agent.

Tasks are stored as JSON and writes are atomic. A filesystem lock serializes
queue operations across local processes. This module schedules work; it does
not execute prompts or modify project files.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


STATUSES = {"pending", "running", "succeeded", "failed"}


class QueueError(RuntimeError):
    """Raised when the task queue cannot safely read or update its state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Task:
    id: str
    prompt: str
    workspace: str
    status: str
    attempts: int
    created_at: str
    updated_at: str
    result: str | None = None
    error: str | None = None


class TaskQueue:
    """JSON-backed task queue with atomic persistence and process locking."""

    def __init__(self, path: str | Path, max_attempts: int = 3):
        self.path = Path(path).expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        if max_attempts < 1:
            raise QueueError("max_attempts must be at least 1.")
        self.max_attempts = max_attempts

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise QueueError(f"Could not lock task queue: {error}") from error

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "tasks": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise QueueError("Task queue file is unreadable or invalid JSON.") from error
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("tasks"), list)
        ):
            raise QueueError("Task queue file has an unsupported structure.")
        seen_ids = set()
        for item in data["tasks"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise QueueError("Task queue contains an invalid task record.")
            if item["id"] in seen_ids:
                raise QueueError("Task queue contains duplicate task IDs.")
            seen_ids.add(item["id"])
            if item.get("status") not in STATUSES:
                raise QueueError("Task queue contains an invalid task status.")
        return data

    def _save(self, data: dict) -> None:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(data, temporary_file, indent=2)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise QueueError(f"Could not persist task queue: {error}") from error

    @staticmethod
    def _task(item: dict) -> Task:
        try:
            return Task(**item)
        except TypeError as error:
            raise QueueError("Task queue contains an invalid task record.") from error

    def enqueue(self, prompt: str, workspace: str | Path) -> Task:
        if not prompt or not prompt.strip():
            raise QueueError("Task prompt cannot be empty.")
        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            raise QueueError(f"Workspace is not an existing directory: {workspace_path}")

        with self._locked():
            data = self._load()
            now = _now()
            task = Task(
                id=uuid.uuid4().hex,
                prompt=prompt.strip(),
                workspace=str(workspace_path),
                status="pending",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            data["tasks"].append(asdict(task))
            self._save(data)
            return task

    def list_tasks(self, status: str | None = None) -> list[Task]:
        if status is not None and status not in STATUSES:
            raise QueueError(f"Unknown task status: {status}")
        with self._locked():
            items = self._load()["tasks"]
            tasks = [self._task(item) for item in items]
            if status is not None:
                tasks = [task for task in tasks if task.status == status]
            return tasks

    def claim_next(self) -> Task | None:
        """Atomically claim the oldest pending task, or return None."""
        with self._locked():
            data = self._load()
            for item in data["tasks"]:
                if item["status"] == "pending":
                    item["status"] = "running"
                    item["attempts"] += 1
                    item["updated_at"] = _now()
                    item["error"] = None
                    self._save(data)
                    return self._task(item)
            return None

    def complete(self, task_id: str, result: str = "") -> Task:
        return self._transition(task_id, "succeeded", result=result, error=None)

    def fail(self, task_id: str, error: str, retry: bool = True) -> Task:
        if not error or not error.strip():
            raise QueueError("A failure reason is required.")
        with self._locked():
            data = self._load()
            item = self._find_running(data, task_id)
            item["error"] = error.strip()
            item["result"] = None
            item["status"] = (
                "pending" if retry and item["attempts"] < self.max_attempts else "failed"
            )
            item["updated_at"] = _now()
            self._save(data)
            return self._task(item)

    def _transition(
        self,
        task_id: str,
        status: str,
        result: str | None,
        error: str | None,
    ) -> Task:
        with self._locked():
            data = self._load()
            item = self._find_running(data, task_id)
            item["status"] = status
            item["result"] = result
            item["error"] = error
            item["updated_at"] = _now()
            self._save(data)
            return self._task(item)

    @staticmethod
    def _find_running(data: dict, task_id: str) -> dict:
        for item in data["tasks"]:
            if item["id"] == task_id:
                if item["status"] != "running":
                    raise QueueError(
                        f"Task {task_id} must be running to record its outcome."
                    )
                return item
        raise QueueError(f"Task not found: {task_id}")

    def recover_interrupted(self) -> int:
        """Requeue interrupted work after restart, respecting the retry limit.

        Call this once when starting a single supervisor process. Do not call it
        while another supervisor is actively working from the same queue.
        """
        recovered = 0
        with self._locked():
            data = self._load()
            for item in data["tasks"]:
                if item["status"] != "running":
                    continue
                if item["attempts"] < self.max_attempts:
                    item["status"] = "pending"
                    item["error"] = "Requeued after an interrupted run."
                else:
                    item["status"] = "failed"
                    item["error"] = "Retry limit reached after an interrupted run."
                item["updated_at"] = _now()
                recovered += 1
            if recovered:
                self._save(data)
        return recovered
