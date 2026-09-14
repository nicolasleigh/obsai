"""One-shot Retrieve → Generate with strict citation acceptance."""

import asyncio

from obsai.answering.citation import validate_citations
from obsai.answering.context import ContextBuilder
from obsai.answering.models import Answer, LLMProvider
from obsai.config.models import AskConfig
from obsai.errors import LLMError, ObsAIError
from obsai.retrieval.hybrid import HybridRetriever
from obsai.retrieval.models import SearchFilters
from obsai.telemetry import measure, metric


class AskService:
    def __init__(
        self, retriever: HybridRetriever, context_builder: ContextBuilder,
        provider: LLMProvider, config: AskConfig,
    ):
        self.retriever = retriever
        self.context_builder = context_builder
        self.provider = provider
        self.config = config

    def ask(self, query: str, filters: SearchFilters | None = None) -> Answer:
        if not query.strip():
            return Answer("请提供问题。", abstained=True)
        outcome = self.retriever.search_with_status(
            query, limit=max(self.config.max_chunks * 3, 10), filters=filters
        )
        context = self.context_builder.build(query, list(outcome.results))
        metric("llm.context", token_estimate=context.token_estimate,
               evidence_count=len(context.evidence))
        if not context.evidence:
            return Answer("未找到可用于回答的笔记证据。", warnings=outcome.warnings, abstained=True)
        try:
            with measure("llm.generate", context_tokens=context.token_estimate):
                response = asyncio.run(
                    self.provider.generate(
                        context.system_prompt, context.user_prompt,
                        max_output_tokens=self.config.max_output_tokens,
                    )
                )
        except ObsAIError:
            raise
        except Exception as exc:
            raise LLMError(f"Answer provider failed: {type(exc).__name__}: {exc}") from exc
        sources = validate_citations(response, context.evidence)
        if sources is None:
            return Answer(
                "无法从检索到的证据生成带有效引用的回答。",
                warnings=(*outcome.warnings, "Model answer had missing or invalid citations"),
                abstained=True,
            )
        return Answer(response.strip(), sources, outcome.warnings)
