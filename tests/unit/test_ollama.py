from __future__ import annotations

import asyncio
from types import SimpleNamespace

from obsai.agent.ollama_planner import OllamaDecisionProvider
from obsai.answering.ollama_provider import OllamaLLMProvider
from obsai.config.models import AskConfig
from obsai.embedding.models import EmbeddingGeneration
from obsai.embedding.ollama_provider import OllamaEmbeddingProvider
from obsai.ollama import ollama_base_url


class _FakeAsyncEmbeddings:
    async def create(self, **kwargs):
        assert kwargs["model"] == "embed-local"
        assert kwargs["input"] == ["first", "second"]
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[0.0, 1.0]),
                SimpleNamespace(index=0, embedding=[1.0, 0.0]),
            ]
        )


class _FakeAsyncCompletions:
    async def create(self, **kwargs):
        assert kwargs["model"] == "chat-local"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="local answer"))]
        )


class _FakeAsyncClient:
    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.embeddings = _FakeAsyncEmbeddings()
        self.chat = SimpleNamespace(completions=_FakeAsyncCompletions())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _FakeOpenAICompletions:
    def create(self, **kwargs):
        assert kwargs["model"] == "chat-local"
        assert kwargs["response_format"] == {"type": "json_object"}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"tool":"search_notes","args":{"query":"redis"}}'
                    )
                )
            ]
        )


class _FakeOpenAIClient:
    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.chat = SimpleNamespace(completions=_FakeOpenAICompletions())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_ollama_base_url_normalization(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    assert ollama_base_url() == "http://ollama:11434/v1/"
    assert ollama_base_url("http://localhost:11434/v1/") == "http://localhost:11434/v1/"


def test_ollama_embedding_provider_uses_local_openai_compatible_endpoint(monkeypatch) -> None:
    monkeypatch.setattr("obsai.embedding.ollama_provider.AsyncOpenAI", _FakeAsyncClient)
    provider = OllamaEmbeddingProvider(
        EmbeddingGeneration("ollama", "embed-local", "embed-local", 2),
        timeout_seconds=3,
    )

    vectors = asyncio.run(provider.embed(["first", "second"]))

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert _FakeAsyncClient.last_kwargs["base_url"] == "http://127.0.0.1:11434/v1/"
    assert _FakeAsyncClient.last_kwargs["api_key"] == "ollama"


def test_ollama_answer_provider_uses_chat_completions(monkeypatch) -> None:
    monkeypatch.setattr("obsai.answering.ollama_provider.AsyncOpenAI", _FakeAsyncClient)
    provider = OllamaLLMProvider(AskConfig(provider="ollama", model="chat-local"))

    answer = asyncio.run(provider.generate("system", "question", max_output_tokens=20))

    assert answer == "local answer"
    assert _FakeAsyncClient.last_kwargs["api_key"] == "ollama"


def test_ollama_planner_returns_structured_tool_decision(monkeypatch) -> None:
    monkeypatch.setattr("obsai.agent.ollama_planner.OpenAI", _FakeOpenAIClient)
    provider = OllamaDecisionProvider(AskConfig(provider="ollama", model="chat-local"))

    decision = provider.decide(
        query="redis",
        intent="search",
        recent_summaries=[],
        available_tools=("search_notes",),
    )

    assert decision.tool == "search_notes"
    assert decision.args == {"query": "redis"}
