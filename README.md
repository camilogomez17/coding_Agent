# coding_Agent

An incremental, testable foundation for a local-first coding agent.

## Current capabilities

- Scans Python files and parses them without executing project code.
- Runs pytest when test files are present and returns a non-zero exit code when checks fail.
- Provides a dependency-free model client for Ollama and OpenAI-compatible chat APIs.
- In hybrid mode, tries Ollama first and falls back to the configured cloud API if local inference fails.
- Persists tasks in a JSON queue with atomic writes, process locking, retry limits, and interrupted-task recovery.
- Provides a CLI for repository inspection, model prompts, and task queue management.
- Runs the test suite in GitHub Actions on pushes to `main` and pull requests targeting `main`.

**Important:** this is not yet an autonomous 24/7 coding agent. Tasks can be queued and model prompts can be sent, but the agent does not yet turn queued tasks into repository edits or run a persistent worker loop. Do not expect unattended code changes yet.

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

## Configure hybrid inference on an Apple Silicon Mac with 16 GB memory

Install Ollama from https://ollama.com, then download a coding model that fits your memory budget:

```bash
ollama pull qwen2.5-coder:7b
```

Ensure Ollama is running. In hybrid mode, local inference is attempted first. A cloud request is made only if the local request fails and a cloud API key is configured.

```bash
export AGENT_MODEL_MODE=hybrid
export AGENT_LOCAL_MODEL=qwen2.5-coder:7b
export AGENT_OLLAMA_URL=http://127.0.0.1:11434
export OPENAI_API_KEY='your-api-key'
```

The client defaults to the OpenAI API at `https://api.openai.com/v1` and model `gpt-4.1-mini`. You can configure a compatible provider with `AGENT_CLOUD_URL` and `AGENT_CLOUD_MODEL`. Keep API keys in environment variables or a local secrets manager; never commit them.

**Privacy:** hybrid fallback can send the prompt and supplied context to the cloud provider. Do not include secrets or proprietary code unless you have explicitly decided that provider may receive them. Local-only mode never intentionally calls the cloud endpoint.

## CLI examples

Run commands from the repository root with your virtual environment activated.

Inspect a project (read-only):

```bash
python agent.py inspect /path/to/project
```

Send a prompt to the configured model:

```bash
python agent.py ask "Explain the architecture of this project"
```

Queue a task for a project. This records the task; it does not execute it yet:

```bash
python agent.py task add --workspace /path/to/project "Find likely bugs and suggest fixes"
python agent.py task list
python agent.py task list --status pending
```

Queue state is stored by default in `~/.coding_agent/tasks.json`. Override it with `AGENT_QUEUE_PATH` if needed. The `task recover` command should only be used after confirming that no other supervisor is actively processing the same queue.

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

1. Model client, CLI, durable queue, and repeatable checks (current foundation).
2. Read-only project context builder and diagnostic reports.
3. Restricted edit worker with diff review and verification.
4. A single-task worker loop with retry budgets, cancellation, and run history.
5. Resource limits, process supervision, and safe restart behavior.
6. Optional launch-at-login setup for unattended operation.

The agent should not autonomously edit arbitrary projects until workspace boundaries, task limits, verification, and recovery are implemented and tested.
