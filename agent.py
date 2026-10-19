"""Command-line interface for the local-first coding agent."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from coding_worker import run_next_coding_task
from diagnostic_worker import run_next_diagnostic
from model_client import ModelClient, ModelError
from task_queue import QueueError, TaskQueue
from worker import inspect_repository


def _queue() -> TaskQueue:
    configured_path = os.getenv("AGENT_QUEUE_PATH")
    path = Path(configured_path).expanduser() if configured_path else (
        Path.home() / ".coding_agent" / "tasks.json"
    )
    return TaskQueue(path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect projects, query a configured model, and manage queued tasks."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser("inspect", help="Run read-only repository checks.")
    inspect_parser.add_argument("workspace", nargs="?", default=".")

    ask_parser = commands.add_parser("ask", help="Send a prompt to the configured model.")
    ask_parser.add_argument("prompt", nargs="+")
    ask_parser.add_argument("--system", default=None)

    task_parser = commands.add_parser("task", help="Manage the persistent task queue.")
    task_commands = task_parser.add_subparsers(dest="task_command", required=True)

    add_parser = task_commands.add_parser("add", help="Queue a task without executing it.")
    add_parser.add_argument("--workspace", required=True)
    add_parser.add_argument("prompt", nargs="+")

    list_parser = task_commands.add_parser("list", help="List queued tasks.")
    list_parser.add_argument("--status", choices=["pending", "running", "succeeded", "failed"])

    task_commands.add_parser(
        "diagnose",
        help="Run one queued task as a read-only model-assisted diagnosis.",
    )
    task_commands.add_parser(
        "code",
        help="Run one queued task through the restricted coding engine.",
    )

    show_parser = task_commands.add_parser("show", help="Show a task's saved result or error.")
    show_parser.add_argument("task_id")

    task_commands.add_parser(
        "recover",
        help="Recover interrupted tasks after confirming no other supervisor is running.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        if args.command == "inspect":
            return inspect_repository(args.workspace)

        if args.command == "ask":
            print(ModelClient().generate(" ".join(args.prompt), system=args.system))
            return 0

        queue = _queue()
        if args.task_command == "add":
            task = queue.enqueue(" ".join(args.prompt), args.workspace)
            print(f"Queued task {task.id}")
            print(f"Workspace: {task.workspace}")
            print(f"Status: {task.status}")
            return 0

        if args.task_command == "list":
            tasks = queue.list_tasks(status=args.status)
            if not tasks:
                print("No tasks found.")
                return 0
            for task in tasks:
                print(
                    f"{task.id}  {task.status:<9} attempts={task.attempts} "
                    f"workspace={task.workspace}"
                )
                print(f"  {task.prompt}")
                if task.error:
                    print(f"  Last error: {task.error}")
            return 0

        if args.task_command == "code":
            task = run_next_coding_task(queue)
            if task is None:
                print("No pending tasks to code.")
                return 0
            print(f"Task: {task.id}")
            print(f"Status: {task.status}")
            if task.status == "succeeded":
                print("\nCoding report:\n")
                print(task.result or "(The coding engine returned an empty report.)")
                return 0
            print(f"Coding failed: {task.error}", file=sys.stderr)
            if task.status == "pending":
                print("The task was requeued for a later retry.", file=sys.stderr)
            return 1

        if args.task_command == "diagnose":
            task = run_next_diagnostic(queue)
            if task is None:
                print("No pending tasks to diagnose.")
                return 0
            print(f"Task: {task.id}")
            print(f"Status: {task.status}")
            if task.status == "succeeded":
                print("\nDiagnostic report:\n")
                print(task.result or "(The model returned an empty report.)")
                return 0
            print(f"Diagnostic failed: {task.error}", file=sys.stderr)
            if task.status == "pending":
                print("The task was requeued for a later retry.", file=sys.stderr)
            return 1

        if args.task_command == "show":
            task = next(
                (item for item in queue.list_tasks() if item.id == args.task_id),
                None,
            )
            if task is None:
                print(f"Task not found: {args.task_id}", file=sys.stderr)
                return 1
            print(f"Task: {task.id}")
            print(f"Status: {task.status}")
            print(f"Workspace: {task.workspace}")
            print(f"Prompt: {task.prompt}")
            if task.error:
                print(f"Error: {task.error}")
            if task.result:
                print("\nSaved result:\n")
                print(task.result)
            return 0

        if args.task_command == "recover":
            count = queue.recover_interrupted()
            print(f"Recovered {count} interrupted task(s).")
            return 0

    except (ModelError, QueueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
