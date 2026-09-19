"""OpenAI Responses adapter; the answer domain knows only LLMProvider."""

import os

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from obsai.config.models import AskConfig
from obsai.errors import ConfigError, LLMError, MissingCredentialError


class OpenAILLMProvider:
    def __init__(self, config: AskConfig):
        if config.provider != "openai":
            raise ConfigError(f"Unsupported answer provider: {config.provider}")
        self.config = config

    async def generate(
        self, system_prompt: str, user_prompt: str, *, max_output_tokens: int
    ) -> str:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            # Not a plain ConfigError: the configuration is fine, the environment
            # is not. The UI needs to tell those two apart to give the right
            # instruction. See MissingCredentialError.
            raise MissingCredentialError("OPENAI_API_KEY is required for remote answers")
        try:
            async with AsyncOpenAI(
                api_key=key, timeout=self.config.timeout_seconds, max_retries=0
            ) as client:
                response = await client.responses.create(
                    model=self.config.model, instructions=system_prompt,
                    input=user_prompt, max_output_tokens=max_output_tokens,
                )
        except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
            raise LLMError(f"OpenAI answer request failed: {type(exc).__name__}: {exc}") from exc
        if not response.output_text:
            raise LLMError("OpenAI returned an empty answer")
        return response.output_text
