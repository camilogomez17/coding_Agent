# coding_Agent

An incremental, local-first coding-agent runtime for inspecting repositories and diagnosing queued tasks with a language model.

## Current capabilities

- Scans Python files and parses them without executing project code.
- Runs pytest when test files are present and returns a non-zero exit code when checks fail.
- Provides a dependency-free model client for Ollama and OpenAI-compatible chat APIs.
- In hybrid mode, tries Ollama first and falls back to the configured cloud API if local inference fails.
- Persists tasks in a JSON queue with atomic writes, process locking, retry limits, and interrupted-task recovery.
- Builds bounded repository context from project files, Git status, and a deterministic file inventory.
- Excludes common secret files, generated directories, symlinks, binary files, and oversized files from model context.
- Runs one queued task at a time as a read-only model-assisted diagnostic and persists the report in the queue.
- Runs the test suite in GitHub Actions on pushes to `main` and pull requests targeting `main`.

- Includes an opt-in restricted coding worker that validates bounded JSON file proposals, creates a dedicated local Git branch, runs syntax checks and pytest with a timeout, rolls back failed proposals, retries within a fixed budget, and commits only verified changes.\n\n**Important:** this is not yet an autonomous 24/7 coding agent. The diagnostic worker can inspect context and save reports, but it does not edit project files, run arbitrary model-requested commands, or continuously process tasks in the background.

## Requirements

- Python 3.10 or newer.
- pytest for tests.
- Ollama for local inference, if using local or hybrid mode.
- An API key for the selected OpenAI-compatible cloud endpoint if cloud mode is used or hybrid fallback is desired.

## Run tests

Create and activate a virtual environment, then install pytest:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install pytest
python -m pytest -q
```

## Configure local inference on an Apple Silicon Mac with 16 GB memory

Install Ollama from https://ollama.com, then download a coding model that fits your memory budget:

```bash
ollama pull qwen2.5-coder:7b
```

For local-only inference without an API key:

```bash
export AGENT_MODEL_MODE=local
export AGENT_LOCAL_MODEL=qwen2.5-coder:7b
export AGENT_OLLAMA_URL=http://127.0.0.1:11434
```

Hybrid mode tries local inference first. It can send prompts and repository context to a configured cloud provider if local inference fails and a cloud API key is available. The client defaults to the OpenAI API at `https://api.openai.com/v1` and model `gpt-4.1-mini`. Configure compatible providers with `AGENT_CLOUD_URL` and `AGENT_CLOUD_MODEL`. Never commit API keys.

**Privacy:** the context builder excludes common secret-file names and directories, but cannot guarantee detection of secrets embedded in ordinary source files. Prefer local-only mode for private repositories. Cloud fallback can transmit supplied context to the configured provider.

## CLI examples

Run read-only repository checks:

```bash
python agent.py inspect /path/to/project
```

Send a prompt directly to the configured model:

```bash
python agent.py ask "Explain the architecture of this project"
```

Queue a task for a project. This records the task without executing it:

```bash
python agent.py task add --workspace /path/to/project "Find likely bugs and suggest fixes"
python agent.py task list
python agent.py task list --status pending
```

Run one pending task as a read-only diagnostic using the configured model:

```bash
python agent.py task diagnose
```

The command claims at most one pending task, builds a bounded repository context, asks the model for a diagnosis, and stores the report in the queue. It never applies edits or executes commands requested by the model. If inference fails, the task is retried within the queue's attempt limit.

View the saved result using the task ID printed by `task diagnose`:

```bash
python agent.py task show TASK_ID
```

Queue state is stored by default in `~/.coding_agent/tasks.json`. Override it with `AGENT_QUEUE_PATH`. The `task recover` command should only be used after confirming that no other supervisor is actively processing the same queue.

## Restricted coding mode

Queue a task as usual, then explicitly run the coding worker:

```bash
python agent.py task add --workspace /path/to/project "Fix the failing parser tests"
python agent.py task code
python agent.py task list
python agent.py task show TASK_ID
```

The coding worker requires the workspace to be the root of a clean Git repository. It creates a unique local branch named `agent/task-...` from the current branch. Model output must be a JSON proposal with complete file contents; the worker accepts at most five files, allows only selected text extensions, caps each file at 64 KB and the whole proposal at 200 KB, and blocks traversal, symlinks, secret files, Git metadata, workflow files, and lockfiles. It never runs shell commands supplied by the model.

Changed Python files are syntax-checked. If tests exist, `python -m pytest -q` runs with a 90-second timeout. Failed proposals are rolled back and retried at most twice by default; the engine enforces a hard maximum of three attempts. Successful changes are committed on the new branch. The worker does not push that branch or open a pull request automatically.

**Execution warning:** pytest executes the repository's own test code. Use coding mode only in repositories you trust, preferably inside a disposable worktree or sandbox. The engine does not provide OS-level sandboxing.

## Model client settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_MODEL_MODE` | `hybrid` | `local`, `cloud`, or `hybrid` |
| `AGENT_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server URL |
| `AGENT_LOCAL_MODEL` | `qwen2.5-coder:7b` | Ollama model name |
| `AGENT_CLOUD_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base URL |
| `AGENT_CLOUD_MODEL` | `gpt-4.1-mini` | Cloud model name |
| `OPENAI_API_KEY` | unset | Cloud API key |
| `AGENT_MODEL_TIMEOUT` | `120` | Per-request timeout in seconds |
| `AGENT_QUEUE_PATH` | `~/.coding_agent/tasks.json` | Persistent queue file |

The Python model interface is `ModelClient().generate(prompt, system=None)` in `model_client.py`. It uses Python's standard library and adds no runtime package dependencies.

## Planned development sequence

1. Model client, CLI, durable queue, and repeatable checks.
2. Bounded read-only repository context and diagnostic reports (implemented).
3. Restricted edit worker with diff review and verification.
4. A single-task worker loop with retry budgets, cancellation, and run history.
5. Resource limits, process supervision, and safe restart behavior.
6. Optional launch-at-login setup for unattended operation.

The agent should not autonomously edit arbitrary projects until workspace boundaries, task limits, verification, and recovery are implemented and tested.
