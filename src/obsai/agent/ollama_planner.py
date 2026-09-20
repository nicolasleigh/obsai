"""Ollama decision adapter for the fully local Agent workflow."""

from __future__ import annotations

import json

from openai import OpenAI

from obsai.agent.openai_planner import INSTRUCTIONS
from obsai.agent.workflow import ToolDecision
from obsai.config.models import AskConfig
from obsai.errors import ConfigError, LLMError
from obsai.ollama import ollama_api_key, ollama_base_url


class OllamaDecisionProvider:
    """Choose Agent tools with a local Ollama chat model in JSON mode."""

    def __init__(self, config: AskConfig) -> None:
        if config.provider != "ollama":
            raise ConfigError(f"Unsupported planner provider: {config.provider}")
        self.config = config
        self.base_url = ollama_base_url(config.base_url)

    def decide(
        self,
        *,
        query: str,
        intent: str,
        recent_summaries: list[str],
        available_tools: tuple[str, ...],
    ) -> ToolDecision:
        prompt = json.dumps(
            {
                "query": query,
                "intent": intent,
                "recent_tool_summaries": recent_summaries,
                "available_tools": available_tools,
            },
            ensure_ascii=False,
        )

        try:
            with OpenAI(
                base_url=self.base_url,
                api_key=ollama_api_key(),
                timeout=self.config.timeout_seconds,
                max_retries=0,
            ) as client:
                response = client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": INSTRUCTIONS},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=self.config.max_output_tokens,
                    response_format={"type": "json_object"},
                )
            output = response.choices[0].message.content if response.choices else None
            if not output:
                raise ValueError("Decision response was empty")
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise ValueError("Decision must be a JSON object")
            if "final_answer" in payload:
                return ToolDecision(final_answer=str(payload["final_answer"]))
            if not isinstance(payload.get("args"), dict):
                raise ValueError("Tool args must be a JSON object")
            return ToolDecision(tool=str(payload["tool"]), args=payload["args"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(f"Invalid Ollama planner response: {exc}") from exc
        except Exception as exc:
            raise LLMError(f"Ollama planner request failed: {type(exc).__name__}: {exc}") from exc
