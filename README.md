# coding_Agent

An incremental, testable foundation for a local-first coding agent.

## Current capabilities

- Scans Python files and parses them without executing project code.
- Runs pytest when test files are present and returns a non-zero exit code when checks fail.
- Provides a dependency-free model client for Ollama and OpenAI-compatible chat APIs.
- In hybrid mode, tries Ollama first and falls back to the configured cloud API if local inference fails.
- Runs the test suite in GitHub Actions on pushes to `main` and pull requests targeting `main`.

**Important:** this repository is not yet an autonomous 24/7 coding agent. The model client currently generates text only; it is not yet connected to a task queue, repository-editing worker, scheduler, or approval/rollback system. Do not point it at important projects expecting unattended code changes.

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

The Python interface is `ModelClient().generate(prompt, system=None)` in `model_client.py`. It uses Python's standard library and does not add runtime package dependencies.

## Planned development sequence

1. Model client and repeatable checks (current foundation).
2. Persistent task queue and run history.
3. Read-only project context builder and diagnostic reports.
4. Restricted edit worker with diff review and verification.
5. Recovery, resource limits, and a supervised scheduler.
6. Optional launch-at-login setup for unattended operation.

The agent should not autonomously edit arbitrary projects until workspace boundaries, task limits, verification, and recovery are implemented and tested.
