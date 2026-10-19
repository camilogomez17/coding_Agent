"""Durable, single-host task queue with dependencies and run history."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

STATUSES = {"pending", "running", "succeeded", "failed"}
KINDS = {"diagnose", "code"}


class QueueError(RuntimeError):
    """Raised when the task queue cannot safely read or update its state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(action: str, detail: str | None = None) -> dict:
    item = {"at": _now(), "action": action}
    if detail:
        item["detail"] = detail[:1000]
    return item


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
    kind: str = "diagnose"
    plan_id: str | None = None
    step_index: int | None = None
    depends_on: str | None = None
    history: list[dict] = field(default_factory=list)
    publish: bool = False
    allowed_paths: list[str] = field(default_factory=list)


class TaskQueue:
    """JSON-backed queue with atomic writes, process locking, dependencies and history."""

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
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("tasks"), list):
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
            # Backward compatibility for queue files created before run history/kinds.
            item.setdefault("history", [])
            item.setdefault("kind", "diagnose")
            item.setdefault("plan_id", None)
            item.setdefault("step_index", None)
            item.setdefault("depends_on", None)
            item.setdefault("publish", False)
            item.setdefault("allowed_paths", [])
            if (item["kind"] not in KINDS or not isinstance(item["history"], list)
                    or not isinstance(item["publish"], bool)
                    or not isinstance(item["allowed_paths"], list)
                    or not all(isinstance(path, str) and path.strip() for path in item["allowed_paths"])):
                raise QueueError("Task queue contains invalid task metadata.")
        return data

    def _save(self, data: dict) -> None:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
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

    def enqueue(
        self, prompt: str, workspace: str | Path, *, kind: str = "diagnose",
        plan_id: str | None = None, step_index: int | None = None,
        depends_on: str | None = None, publish: bool = False,
        allowed_paths: list[str] | None = None,
    ) -> Task:
        if not prompt or not prompt.strip():
            raise QueueError("Task prompt cannot be empty.")
        if kind not in KINDS:
            raise QueueError(f"Unknown task kind: {kind}")
        if allowed_paths is not None and (
            not all(isinstance(path, str) and path.strip() for path in allowed_paths)
            or len(set(allowed_paths)) != len(allowed_paths)
        ):
            raise QueueError("allowed_paths must contain unique, non-empty path strings.")
        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            raise QueueError(f"Workspace is not an existing directory: {workspace_path}")
        with self._locked():
            data = self._load()
            now = _now()
            task = Task(
                id=uuid.uuid4().hex, prompt=prompt.strip(), workspace=str(workspace_path),
                status="pending", attempts=0, created_at=now, updated_at=now,
                kind=kind, plan_id=plan_id, step_index=step_index, depends_on=depends_on,
                history=[_event("queued")], publish=publish,
                allowed_paths=list(allowed_paths or []),
            )
            data["tasks"].append(asdict(task))
            self._save(data)
            return task

    def enqueue_plan(
        self, objective: str, workspace: str | Path, steps: list[str], *, publish: bool = False,
        allowed_paths: list[str] | None = None,
    ) -> list[Task]:
        """Persist an ordered coding plan atomically; each step depends on its predecessor."""
        if not objective or not objective.strip():
            raise QueueError("Plan objective cannot be empty.")
        if not steps or len(steps) > 8:
            raise QueueError("A plan must contain between 1 and 8 steps.")
        normalized = []
        for step in steps:
            if not isinstance(step, str) or not step.strip() or len(step) > 2000:
                raise QueueError("Each plan step must be non-empty and at most 2000 characters.")
            normalized.append(step.strip())
        if allowed_paths is not None and (
            not all(isinstance(path, str) and path.strip() for path in allowed_paths)
            or len(set(allowed_paths)) != len(allowed_paths)
        ):
            raise QueueError("allowed_paths must contain unique, non-empty path strings.")
        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            raise QueueError(f"Workspace is not an existing directory: {workspace_path}")
        plan_id = uuid.uuid4().hex
        with self._locked():
            data = self._load()
            now = _now()
            created: list[Task] = []
            previous_id = None
            for index, step in enumerate(normalized, start=1):
                task = Task(
                    id=uuid.uuid4().hex,
                    prompt=(f"Overall objective: {objective.strip()}\n"
                            f"Plan step {index}/{len(normalized)}: {step}\n"
                            "Implement only this step. Respect repository constraints and verify your changes."),
                    workspace=str(workspace_path), status="pending", attempts=0,
                    created_at=now, updated_at=now, kind="code", plan_id=plan_id,
                    step_index=index, depends_on=previous_id,
                    history=[_event("queued", f"plan step {index}/{len(normalized)}")],
                    publish=publish, allowed_paths=list(allowed_paths or []),
                )
                data["tasks"].append(asdict(task))
                created.append(task)
                previous_id = task.id
            self._save(data)
            return created

    def purge_failed(self) -> int:
        """Delete failed tasks and clear saved failure details from remaining tasks."""
        with self._locked():
            data = self._load()
            failed_ids = {
                item["id"] for item in data["tasks"] if item["status"] == "failed"
            }
            dependents = [
                item["id"] for item in data["tasks"]
                if item["status"] != "failed" and item.get("depends_on") in failed_ids
            ]
            if dependents:
                raise QueueError(
                    "Cannot purge failed tasks while non-failed tasks depend on them: "
                    + ", ".join(dependents[:10])
                )
            original_count = len(data["tasks"])
            remaining = []
            for item in data["tasks"]:
                if item["id"] in failed_ids:
                    continue
                item["history"] = [
                    event for event in item.get("history", [])
                    if event.get("action") != "failed"
                ]
                item["error"] = None
                remaining.append(item)
            data["tasks"] = remaining
            self._save(data)
            return original_count - len(data["tasks"])

    def list_tasks(self, status: str | None = None) -> list[Task]:
        if status is not None and status not in STATUSES:
            raise QueueError(f"Unknown task status: {status}")
        with self._locked():
            tasks = [self._task(item) for item in self._load()["tasks"]]
            return [task for task in tasks if status is None or task.status == status]

    def claim_next(self, kind: str | None = None) -> Task | None:
        """Claim the oldest eligible task. Plan steps wait for predecessors."""
        if kind is not None and kind not in KINDS:
            raise QueueError(f"Unknown task kind: {kind}")
        with self._locked():
            data = self._load()
            by_id = {item["id"]: item for item in data["tasks"]}
            changed = False
            for item in data["tasks"]:
                if item["status"] != "pending" or (kind and item["kind"] != kind):
                    continue
                dependency_id = item.get("depends_on")
                if dependency_id:
                    dependency = by_id.get(dependency_id)
                    if dependency is None:
                        item["status"] = "failed"
                        item["error"] = "Plan dependency is missing; task blocked."
                        item["updated_at"] = _now()
                        item.setdefault("history", []).append(_event("blocked", item["error"]))
                        changed = True
                        continue
                    if dependency["status"] == "failed":
                        item["status"] = "failed"
                        item["error"] = f"Blocked because prerequisite {dependency_id} failed."
                        item["updated_at"] = _now()
                        item.setdefault("history", []).append(_event("blocked", item["error"]))
                        changed = True
                        continue
                    if dependency["status"] != "succeeded":
                        continue
                item["status"] = "running"
                item["attempts"] += 1
                item["updated_at"] = _now()
                item["error"] = None
                item.setdefault("history", []).append(_event("started", f"attempt {item['attempts']}"))
                self._save(data)
                return self._task(item)
            if changed:
                self._save(data)
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
            item["status"] = "pending" if retry and item["attempts"] < self.max_attempts else "failed"
            item["updated_at"] = _now()
            item.setdefault("history", []).append(_event("failed", error.strip()))
            self._save(data)
            return self._task(item)

    def _transition(self, task_id: str, status: str, result: str | None, error: str | None) -> Task:
        with self._locked():
            data = self._load()
            item = self._find_running(data, task_id)
            item["status"], item["result"], item["error"] = status, result, error
            item["updated_at"] = _now()
            item.setdefault("history", []).append(_event(status))
            self._save(data)
            return self._task(item)

    @staticmethod
    def _find_running(data: dict, task_id: str) -> dict:
        for item in data["tasks"]:
            if item["id"] == task_id:
                if item["status"] != "running":
                    raise QueueError(f"Task {task_id} must be running to record its outcome.")
                return item
        raise QueueError(f"Task not found: {task_id}")

    def recover_interrupted(self) -> int:
        """Requeue interrupted work after restart, respecting retry limits."""
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
                item.setdefault("history", []).append(_event("recovered", item["error"]))
                recovered += 1
            if recovered:
                self._save(data)
        return recovered
