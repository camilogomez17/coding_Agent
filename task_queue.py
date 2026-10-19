"""Durable, single-host task queue with dependencies and run history."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

STATUSES = {"pending", "running", "succeeded", "failed", "cancelled"}
KINDS = {"diagnose", "code"}
MAX_PLAN_STEPS = 20


class QueueError(RuntimeError):
    """Raised when the task queue cannot safely read or update its state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(action: str, detail: str | None = None) -> dict:
    item = {"at": _now(), "action": action}
    if detail:
        item["detail"] = detail[:1000]
    return item


def _find_task_commit(workspace: str, task_id: str) -> tuple[str, str] | None:
    """Find a committed coding result even if the worker died before queue checkpointing."""
    try:
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            return None
        log = subprocess.run(
            ["git", "log", "--all", "--format=%H%x09%B%x00"],
            cwd=root, capture_output=True, text=True, timeout=10, check=False,
        )
        if log.returncode:
            return None
        trailer = f"Coding-Agent-Task: {task_id}"
        for entry in log.stdout.split("\\x00"):
            header, separator, body = entry.partition("\\t")
            if not separator or not header.strip():
                continue
            if trailer not in {line.strip() for line in body.splitlines()}:
                continue
            commit = header.strip().splitlines()[-1]
            branches = subprocess.run(
                ["git", "for-each-ref", "--contains", commit,
                 "--format=%(refname:short)", "refs/heads/agent/task-*"],
                cwd=root, capture_output=True, text=True, timeout=10, check=False,
            )
            branch = next((line.strip() for line in branches.stdout.splitlines()
                           if line.strip()), "unknown")
            return commit, branch
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return None


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
    adaptive_plan: bool = False
    plan_objective: str | None = None


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
            return {"version": 1, "tasks": [], "control": {"paused": False, "cancel_requested": False}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise QueueError("Task queue file is unreadable or invalid JSON.") from error
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("tasks"), list):
            raise QueueError("Task queue file has an unsupported structure.")
        control = data.setdefault("control", {"paused": False, "cancel_requested": False})
        if not isinstance(control, dict) or not isinstance(control.get("paused", False), bool) or not isinstance(control.get("cancel_requested", False), bool):
            raise QueueError("Task queue contains invalid runtime control metadata.")
        control.setdefault("paused", False)
        control.setdefault("cancel_requested", False)
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
            item.setdefault("adaptive_plan", False)
            item.setdefault("plan_objective", None)
            if (item["kind"] not in KINDS or not isinstance(item["history"], list)
                    or not isinstance(item["publish"], bool)
                    or not isinstance(item["allowed_paths"], list)
                    or not isinstance(item["adaptive_plan"], bool)
                    or (item["plan_objective"] is not None and not isinstance(item["plan_objective"], str))
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
        allowed_paths: list[str] | None = None, adaptive_plan: bool = False,
        plan_objective: str | None = None,
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
                allowed_paths=list(allowed_paths or []), adaptive_plan=adaptive_plan,
                plan_objective=plan_objective,
            )
            data["tasks"].append(asdict(task))
            self._save(data)
            return task

    def enqueue_plan(
        self, objective: str, workspace: str | Path, steps: list[str], *, publish: bool = False,
        allowed_paths: list[str] | None = None, adaptive: bool = False,
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
                    adaptive_plan=adaptive, plan_objective=objective.strip(),
                )
                data["tasks"].append(asdict(task))
                created.append(task)
                previous_id = task.id
            self._save(data)
            return created



    def annotate_task(self, task_id: str, detail: str, *, action: str) -> Task:
        """Append a bounded operational note to a task result and event history."""
        if not detail or not detail.strip():
            raise QueueError("Task annotation cannot be empty.")
        with self._locked():
            data = self._load()
            for item in data["tasks"]:
                if item["id"] != task_id:
                    continue
                item["result"] = ((item.get("result") or "").rstrip()
                                  + "\n" + detail.strip()[:1000]).strip()
                item["updated_at"] = _now()
                item.setdefault("history", []).append(_event(action, detail))
                self._save(data)
                return self._task(item)
            raise QueueError(f"Task not found: {task_id}")

    def revise_pending_plan(self, completed_task_id: str, steps: list[str]) -> list[Task]:
        """Replace only the unstarted tail of an adaptive plan after a successful step."""
        if not isinstance(steps, list) or len(steps) > 8:
            raise QueueError("An adaptive plan must contain between 0 and 8 steps.")
        normalized = []
        for step in steps:
            if not isinstance(step, str) or not step.strip() or len(step) > 2000:
                raise QueueError("Each adaptive step must be non-empty and at most 2000 characters.")
            normalized.append(step.strip())
        with self._locked():
            data = self._load()
            if data["control"].get("paused") or data["control"].get("cancel_requested"):
                return None
            by_id = {item["id"]: item for item in data["tasks"]}
            current = by_id.get(completed_task_id)
            if current is None:
                raise QueueError(f"Task not found: {completed_task_id}")
            if current["status"] != "succeeded" or not current.get("adaptive_plan"):
                raise QueueError("Adaptive replanning requires a succeeded adaptive-plan step.")
            plan_id = current.get("plan_id")
            if not plan_id or not current.get("plan_objective"):
                raise QueueError("Adaptive plan metadata is missing.")
            later = [
                item for item in data["tasks"]
                if item.get("plan_id") == plan_id
                and (item.get("step_index") or 0) > current["step_index"]
            ]
            if any(item["status"] != "pending" for item in later):
                raise QueueError("Cannot revise a plan after a later step has started.")
            data["tasks"] = [item for item in data["tasks"] if item not in later]
            now = _now()
            created = []
            previous_id = completed_task_id
            start_index = current["step_index"] + 1
            if normalized and start_index + len(normalized) - 1 > MAX_PLAN_STEPS:
                raise QueueError(f"Adaptive plans are capped at {MAX_PLAN_STEPS} total steps.")
            for offset, step in enumerate(normalized):
                index = start_index + offset
                task = Task(
                    id=uuid.uuid4().hex,
                    prompt=(f"Overall objective: {current['plan_objective']}\\n"
                            f"Plan step {index}: {step}\\n"
                            "Implement only this step. Respect repository constraints and verify your changes."),
                    workspace=current["workspace"], status="pending", attempts=0,
                    created_at=now, updated_at=now, kind="code", plan_id=plan_id,
                    step_index=index, depends_on=previous_id,
                    history=[_event("queued", f"adaptive plan step {index}")],
                    publish=current.get("publish", False),
                    allowed_paths=list(current.get("allowed_paths", [])),
                    adaptive_plan=True, plan_objective=current["plan_objective"],
                )
                data["tasks"].append(asdict(task))
                created.append(task)
                previous_id = task.id
            current.setdefault("history", []).append(
                _event("plan_revised", f"Replaced {len(later)} pending step(s) with {len(created)} adaptive step(s).")
            )
            current["updated_at"] = now
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


    def runtime_control(self) -> dict:
        """Return persisted supervisor controls and live task counts."""
        with self._locked():
            data = self._load()
            control = dict(data["control"])
            control["running_tasks"] = [item["id"] for item in data["tasks"] if item["status"] == "running"]
            control["counts"] = {status: sum(1 for item in data["tasks"] if item["status"] == status) for status in sorted(STATUSES)}
            return control

    def set_paused(self, paused: bool) -> dict:
        """Pause or resume future task claims without interrupting active work."""
        if not isinstance(paused, bool):
            raise QueueError("paused must be a boolean.")
        with self._locked():
            data = self._load()
            data["control"]["paused"] = paused
            data["control"]["cancel_requested"] = False
            self._save(data)
            return dict(data["control"])

    def request_cancel(self) -> int:
        """Stop the supervisor after active work finishes and cancel pending tasks."""
        with self._locked():
            data = self._load()
            data["control"]["cancel_requested"] = True
            data["control"]["paused"] = False
            cancelled = 0
            now = _now()
            for item in data["tasks"]:
                if item["status"] == "pending":
                    item["status"] = "cancelled"
                    item["error"] = "Cancelled by operator before execution."
                    item["updated_at"] = now
                    item.setdefault("history", []).append(_event("cancelled", item["error"]))
                    cancelled += 1
            self._save(data)
            return cancelled

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
            if data["control"].get("paused") or data["control"].get("cancel_requested"):
                return None
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
                    if dependency["status"] in {"failed", "cancelled"}:
                        item["status"] = "failed"
                        item["error"] = f"Blocked because prerequisite {dependency_id} ended unsuccessfully."
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
                events = item.setdefault("history", [])
                verified_commit = next(
                    (event for event in reversed(events)
                     if event.get("action") == "verified_commit"),
                    None,
                )
                committed = (
                    _find_task_commit(item["workspace"], item["id"])
                    if item.get("kind") == "code" else None
                )
                if committed:
                    commit, branch = committed
                    checkpoint = (
                        f"Recovered verified commit from Git history.\\n"
                        f"Branch: {branch}\\nCommit: {commit}\\n"
                        "Verification: commit was created by the coding engine after verification."
                    )
                    item["result"] = ((item.get("result") or "").rstrip()
                                      + "\\n" + checkpoint).strip()
                    item["status"] = "succeeded"
                    item["error"] = None
                    detail = "Recovered successful coding task from its stable Git commit identifier."
                elif verified_commit and item.get("result"):
                    # A coding worker can die after its verified commit is saved
                    # but before the queue transition. Do not execute it again.
                    item["status"] = "succeeded"
                    item["error"] = None
                    detail = "Recovered completed coding task from its verified-commit checkpoint."
                elif item["attempts"] < self.max_attempts:
                    item["status"] = "pending"
                    item["error"] = "Requeued after an interrupted run."
                    detail = item["error"]
                else:
                    item["status"] = "failed"
                    item["error"] = "Retry limit reached after an interrupted run."
                    detail = item["error"]
                item["updated_at"] = _now()
                events.append(_event("recovered", detail))
                recovered += 1
            if recovered:
                self._save(data)
        return recovered
