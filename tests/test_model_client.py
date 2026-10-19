import json
from urllib.error import URLError

import pytest

import model_client
from model_client import ModelClient, ModelConfig, ModelError


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def config(**overrides):
    values = {
        "mode": "local",
        "ollama_url": "http://localhost:11434",
        "local_model": "test-local",
        "cloud_url": "https://cloud.example/v1",
        "cloud_model": "test-cloud",
        "cloud_api_key": None,
        "timeout_seconds": 3,
    }
    values.update(overrides)
    return ModelConfig(**values)


def test_local_mode_posts_to_ollama_and_returns_content(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["payload"] = json.loads(request.data)
        seen["timeout"] = timeout
        return FakeResponse({"message": {"content": "  local answer  "}})

    monkeypatch.setattr(model_client, "urlopen", fake_urlopen)

    result = ModelClient(config()).generate("Explain this code", "Be concise")

    assert result == "local answer"
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["payload"]["model"] == "test-local"
    assert seen["payload"]["messages"] == [
        {"role": "system", "content": "Be concise"},
        {"role": "user", "content": "Explain this code"},
    ]
    assert seen["payload"]["stream"] is False
    schema = seen["payload"]["format"]
    assert schema == model_client.PROPOSAL_SCHEMA
    assert schema["type"] == "object"
    assert schema["required"] == ["summary", "changes"]

    change_schema = schema["properties"]["changes"]["items"]
    assert change_schema["required"] == ["path", "content"]
    assert change_schema["properties"]["path"]["type"] == "string"
    assert change_schema["properties"]["content"]["type"] == "string"
    assert change_schema["properties"]["reason"]["type"] == "string"
    assert seen["timeout"] == 3


def test_cloud_mode_sends_api_key_and_returns_content(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers.get("Authorization")
        seen["payload"] = json.loads(request.data)
        return FakeResponse(
            {"choices": [{"message": {"content": "cloud answer"}}]}
        )

    monkeypatch.setattr(model_client, "urlopen", fake_urlopen)
    client = ModelClient(config(mode="cloud", cloud_api_key="test-secret"))

    assert client.generate("Review this patch") == "cloud answer"
    assert seen["url"] == "https://cloud.example/v1/chat/completions"
    assert seen["authorization"] == "Bearer test-secret"
    assert seen["payload"]["model"] == "test-cloud"


def test_hybrid_mode_falls_back_to_cloud_when_local_is_unavailable(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if "localhost:11434" in request.full_url:
            raise URLError("local server is offline")
        return FakeResponse(
            {"choices": [{"message": {"content": "cloud fallback"}}]}
        )

    monkeypatch.setattr(model_client, "urlopen", fake_urlopen)
    client = ModelClient(config(mode="hybrid", cloud_api_key="test-secret"))

    assert client.generate("Find the bug") == "cloud fallback"
    assert calls == [
        "http://localhost:11434/api/chat",
        "https://cloud.example/v1/chat/completions",
    ]


def test_hybrid_mode_explains_missing_cloud_fallback(monkeypatch):
    def fake_urlopen(request, timeout):
        raise URLError("offline")

    monkeypatch.setattr(model_client, "urlopen", fake_urlopen)
    client = ModelClient(config(mode="hybrid"))

    with pytest.raises(ModelError, match="no cloud fallback is configured"):
        client.generate("Find the bug")


def test_empty_prompt_is_rejected_without_network_call(monkeypatch):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Network must not be called for an empty prompt")

    monkeypatch.setattr(model_client, "urlopen", unexpected_call)

    with pytest.raises(ModelError, match="Prompt cannot be empty"):
        ModelClient(config()).generate("  ")


def test_cloud_mode_requires_api_key():
    with pytest.raises(ModelError, match="requires OPENAI_API_KEY"):
        ModelClient(config(mode="cloud"))


def test_invalid_mode_is_rejected():
    with pytest.raises(ModelError, match="must be local, cloud, or hybrid"):
        ModelClient(config(mode="automatic"))
