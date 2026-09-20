"""Ollama chat-completions adapter for fully local answering."""

from __future__ import annotations

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from obsai.config.models import AskConfig
from obsai.errors import ConfigError, LLMError
from obsai.ollama import ollama_api_key, ollama_base_url


class OllamaLLMProvider:
    """Generate answers through Ollama's local OpenAI-compatible chat API."""

    def __init__(self, config: AskConfig) -> None:
        if config.provider != "ollama":
            raise ConfigError(f"Unsupported answer provider: {config.provider}")
        self.config = config
        self.base_url = ollama_base_url(config.base_url)

    async def generate(
        self, system_prompt: str, user_prompt: str, *, max_output_tokens: int
    ) -> str:
        try:
            async with AsyncOpenAI(
                base_url=self.base_url,
                api_key=ollama_api_key(),
                timeout=self.config.timeout_seconds,
                max_retries=0,
            ) as client:
                response = await client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=max_output_tokens,
                )
        except APITimeoutError as exc:
            raise LLMError("Ollama answer request timed out") from exc
        except APIConnectionError as exc:
            raise LLMError("Ollama server is unavailable") from exc
        except APIStatusError as exc:
            raise LLMError(f"Ollama answer request failed ({exc.status_code})") from exc

        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise LLMError("Ollama returned an empty answer")
        return content
