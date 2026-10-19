# coding_Agent

A local-first coding-agent runtime for repository inspection, task planning, bounded code changes, verification, and persistent task processing.

## Capabilities

- Builds bounded repository context without following symlinks or intentionally including common secret files.
- Supports local Ollama and OpenAI-compatible model endpoints.
- Stores tasks in an atomically written, process-locked JSON queue.
- Persists task status, attempts, timestamps, errors, results, and an append-only event history.
- Recovers tasks left running after a process interruption, subject to retry limits.
- Plans a high-level objective into 2-8 ordered coding steps. Each step waits for the previous step to succeed; downstream steps are marked blocked if a prerequisite fails.
- Runs read-only diagnostic tasks or restricted coding tasks from the same queue.
- Applies bounded model-proposed file changes on a dedicated Git branch, checks Python syntax, runs pytest when tests are present, rolls back failed proposals, retries within strict limits, and commits verified changes.
- Optionally pushes a verified task branch to `origin` and creates or reuses a GitHub pull request through the authenticated GitHub CLI; publishing is opt-in per task and never merges.
- Provides a persistent supervisor loop with idle polling and graceful SIGINT/SIGTERM shutdown after the active task completes.
- Runs the test suite in GitHub Actions.

## Requirements

- Python 3.10 or newer.
- pytest for tests and coding-mode verification.
- Ollama for local or hybrid inference, if using local mode.
- An API key for the configured OpenAI-compatible endpoint if cloud mode is used or hybrid fallback is desired.

## Install and test

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install pytest
python -m pytest -q
```

## Configure the model

For local inference on a machine running Ollama:

```bash
ollama pull qwen2.5-coder:7b
export AGENT_MODEL_MODE=local
export AGENT_LOCAL_MODEL=qwen2.5-coder:7b
export AGENT_OLLAMA_URL=http://127.0.0.1:11434
```

Hybrid mode tries local inference first, then uses the configured cloud endpoint if local inference fails and a cloud API key is available. Defaults are the OpenAI API at `https://api.openai.com/v1` and model `gpt-4.1-mini`. Compatible endpoints can be configured using `AGENT_CLOUD_URL` and `AGENT_CLOUD_MODEL`. Never commit API keys.

**Privacy:** repository context can contain source code and is sent to the configured model endpoint. Secret-file filters are best-effort and cannot guarantee detection of secrets embedded in source files. Prefer local-only mode for private repositories.

## CLI

Inspect a repository without intentionally executing its source files:

```bash
python agent.py inspect /path/to/project
```

Ask the configured model a direct question:

```bash
python agent.py ask "Explain the architecture of this project"
```

Queue a read-only diagnostic or coding task:

```bash
python agent.py task add --workspace /path/to/project "Find likely bugs and suggest fixes"
python agent.py task add --workspace /path/to/project --mode code "Fix the parser tests"
python agent.py task add --workspace /path/to/project --mode code --allow-path parser.py --allow-path tests/test_parser.py "Fix the parser tests"
python agent.py task add --workspace /path/to/project --mode code --publish "Fix the parser tests and open a PR"
python agent.py task list
python agent.py task list --status pending
python agent.py task show TASK_ID
python agent.py task purge-failed --yes
```

Run one task at a time manually:

```bash
python agent.py task diagnose
python agent.py task code
```

Plan a high-level coding objective into ordered, dependent tasks:

```bash
python agent.py task plan --workspace /path/to/project "Add import sorting and tests"
python agent.py task plan --workspace /path/to/project --publish "Add import sorting and tests"
python agent.py task list
```

The planner uses the configured model to produce 2-8 bounded steps. Steps are queued atomically and executed in order. A step does not become eligible until its predecessor succeeds; a permanently failed prerequisite blocks later steps. Review generated plans before letting them run.

Start the persistent supervisor:

```bash
python agent.py serve
```

The supervisor polls the queue every five seconds by default and processes both diagnostic and coding tasks. It recovers interrupted tasks once at startup, records task events, waits when idle, and handles Ctrl-C/SIGTERM by stopping after the active task finishes. For a bounded smoke test:

```bash
python agent.py serve --poll-interval 1 --max-tasks 1
```

Use `--no-recover` only if you have already handled interrupted tasks yourself. Run exactly one supervisor per queue; do not run `task recover` while a supervisor is active.

Queue state defaults to `~/.coding_agent/tasks.json`. Set `AGENT_QUEUE_PATH` to use a different queue file. Back up this file if its task history is important. `task purge-failed --yes` permanently removes terminal failed task records and clears saved failure details from remaining tasks; it refuses to delete records that non-failed tasks still depend on.

## Restricted coding mode and safety limits

Coding tasks require a Git repository root with a clean working tree. The engine creates a unique local branch named `agent/task-...`. Model output must be JSON containing complete file contents. Each proposal is limited to five files, 64 KB per file, and 200 KB total. Only selected text extensions are accepted; traversal, symlinks, secret-like paths, Git metadata, workflow files, and dependency lockfiles are blocked. Use repeated `--allow-path` options to enforce an exact per-task file allowlist; the engine rejects proposals that touch any other path. Without this option, no task-specific allowlist is applied. The model cannot supply shell commands for execution. Add `--publish` to a `--mode code` task to request branch publication and pull-request creation after local verification. The repository must have an `origin` remote, the `gh` GitHub CLI must be installed and authenticated, and the branch must be pushable. The agent pushes only its task branch and never merges a pull request. If publication fails, the coding task remains successful locally and the saved report explains the publication error.

Python files are syntax-checked. If tests exist, `python -m pytest -q` runs with a 90-second timeout. Failed proposals are rolled back and retried within a hard cap of three attempts. Successful changes are committed on the task branch.

**Important limitations:**

- The supervisor is a single-host process, not a hosted service. It stops when the host shuts down unless configured under an appropriate process manager on an always-on machine.
- The current planner sequences small coding tasks; it does not dynamically re-plan based on arbitrary results or guarantee that a broad objective can be completed.
- The coding engine uses bounded complete-file proposals rather than general-purpose patch hunks.
- Tests execute repository code. The current engine does not provide OS-level sandboxing, and the model is not allowed to choose commands but the configured test runner executes the project's tests. Use only trusted repositories and prefer a disposable container/worktree.
- By default, runtime-created task branches remain local. Publishing is opt-in with `--publish`; it requires network access, an `origin` remote, and an authenticated GitHub CLI (`gh auth login`). The agent never merges pull requests or pushes to the base branch.
- Review generated plans and code changes before relying on them. Keep merging under human control.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_MODEL_MODE` | `hybrid` | `local`, `cloud`, or `hybrid` |
| `AGENT_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server URL |
| `AGENT_LOCAL_MODEL` | `qwen2.5-coder:7b` | Local model |
| `AGENT_CLOUD_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base URL |
| `AGENT_CLOUD_MODEL` | `gpt-4.1-mini` | Cloud model |
| `OPENAI_API_KEY` | unset | Cloud API key |
| `AGENT_MODEL_TIMEOUT` | `120` | Per-request timeout in seconds |
| `AGENT_QUEUE_PATH` | `~/.coding_agent/tasks.json` | Persistent queue file |

The model client interface is `ModelClient().generate(prompt, system=None)` in `model_client.py`. Runtime code uses Python's standard library; pytest is required for tests.
