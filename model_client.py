"""Small, dependency-free client for local Ollama and OpenAI-compatible models.

Hybrid mode tries the local model first and uses the configured cloud endpoint
only when the local request fails. API keys are read from the environment.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelError(RuntimeError):
    """Raised when a configured model cannot produce a response."""


@dataclass(frozen=True)
class ModelConfig:
    mode: str = "hybrid"
    ollama_url: str = "http://127.0.0.1:11434"
    local_model: str = "qwen2.5-coder:7b"
    cloud_url: str = "https://api.openai.com/v1"
    cloud_model: str = "gpt-4.1-mini"
    cloud_api_key: str | None = None
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "ModelConfig":
        """Build configuration from environment variables."""
        mode = os.getenv("AGENT_MODEL_MODE", "hybrid").strip().lower()
        try:
            timeout = float(os.getenv("AGENT_MODEL_TIMEOUT", "120"))
        except ValueError as error:
            raise ModelError(
                "AGENT_MODEL_TIMEOUT must be a number of seconds."
            ) from error

        config = cls(
            mode=mode,
            ollama_url=os.getenv("AGENT_OLLAMA_URL", "http://127.0.0.1:11434").rstrip(
                "/"
            ),
            local_model=os.getenv("AGENT_LOCAL_MODEL", "qwen2.5-coder:7b"),
            cloud_url=os.getenv("AGENT_CLOUD_URL", "https://api.openai.com/v1").rstrip(
                "/"
            ),
            cloud_model=os.getenv("AGENT_CLOUD_MODEL", "gpt-4.1-mini"),
            cloud_api_key=os.getenv("OPENAI_API_KEY") or None,
            timeout_seconds=timeout,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.mode not in {"local", "cloud", "hybrid"}:
            raise ModelError("AGENT_MODEL_MODE must be local, cloud, or hybrid.")
        if self.timeout_seconds <= 0:
            raise ModelError("AGENT_MODEL_TIMEOUT must be greater than zero.")
        if not self.local_model.strip():
            raise ModelError("AGENT_LOCAL_MODEL cannot be empty.")
        if not self.cloud_model.strip():
            raise ModelError("AGENT_CLOUD_MODEL cannot be empty.")
        if self.mode == "cloud" and not self.cloud_api_key:
            raise ModelError("Cloud mode requires OPENAI_API_KEY to be set.")


PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "changes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "required": ["summary", "changes"],
}


class ModelClient:
    """Generate text using local, cloud, or local-first hybrid inference."""

    def __init__(self, config: ModelConfig | None = None):
        self.config = config or ModelConfig.from_env()
        self.config.validate()

    def generate(self, prompt: str, system: str | None = None) -> str:
        if not prompt.strip():
            raise ModelError("Prompt cannot be empty.")

        if self.config.mode == "local":
            return self._generate_local(prompt, system)
        if self.config.mode == "cloud":
            return self._generate_cloud(prompt, system)

        try:
            return self._generate_local(prompt, system)
        except (ModelError, OSError, TimeoutError) as local_error:
            if not self.config.cloud_api_key:
                raise ModelError(
                    "Local model request failed and no cloud fallback is configured. "
                    "Start Ollama or set OPENAI_API_KEY to enable hybrid fallback."
                ) from local_error
            try:
                return self._generate_cloud(prompt, system)
            except (ModelError, OSError, TimeoutError) as cloud_error:
                raise ModelError(
                    "Both local inference and cloud fallback failed. "
                    "Check Ollama, cloud endpoint settings, and API credentials."
                ) from cloud_error

    def _messages(self, prompt: str, system: str | None) -> list[dict[str, str]]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw_body = response.read()
        except HTTPError as error:
            # Do not include response bodies; providers can echo sensitive input.
            raise ModelError(f"Model endpoint returned HTTP {error.code}.") from error
        except (URLError, TimeoutError, OSError) as error:
            raise ModelError(
                f"Could not reach model endpoint ({type(error).__name__})."
            ) from error

        try:
            decoded = json.loads(raw_body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ModelError("Model endpoint returned invalid JSON.") from error
        if not isinstance(decoded, dict):
            raise ModelError("Model endpoint returned an unexpected response.")
        return decoded

    def _generate_local(self, prompt: str, system: str | None) -> str:
        response = self._post_json(
            f"{self.config.ollama_url}/api/chat",
            {
                "model": self.config.local_model,
                "messages": self._messages(prompt, system),
                "stream": False,
                "format": PROPOSAL_SCHEMA,
            },
        )
        message = response.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ModelError("Local model returned no message content.")
        return content.strip()

    def _generate_cloud(self, prompt: str, system: str | None) -> str:
        if not self.config.cloud_api_key:
            raise ModelError("Cloud inference requires OPENAI_API_KEY to be set.")
        response = self._post_json(
            f"{self.config.cloud_url}/chat/completions",
            {
                "model": self.config.cloud_model,
                "messages": self._messages(prompt, system),
                "stream": False,
            },
            {"Authorization": f"Bearer {self.config.cloud_api_key}"},
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError("Cloud model returned no choices.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ModelError("Cloud model returned no message content.")
        return content.strip()
