"""Preflight, budget, batch, retry, and cache orchestration."""

import asyncio
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable

from obsai.config.models import EmbeddingConfig
from obsai.embedding.models import EmbeddingGeneration, EmbeddingProvider
from obsai.embedding.pricing import price_per_million
from obsai.errors import (
    EmbeddingBudgetError, EmbeddingError, EmbeddingRateLimitError,
    EmbeddingServiceError,
)
from obsai.storage.vectors import SQLiteVectorStore
from obsai.shutdown import check_shutdown
from obsai.telemetry import measure, measured, measured_async, metric


@dataclass(frozen=True)
class PendingText:
    text_hash: str
    text: str
    tokens: int


@dataclass(frozen=True)
class EmbeddingPlan:
    generation: EmbeddingGeneration
    pending_chunks: tuple[tuple[str, str], ...]
    remote_texts: tuple[PendingText, ...]
    batches: tuple[tuple[PendingText, ...], ...]
    cache_hits: int
    estimated_tokens: int
    estimated_cost_usd: Decimal

    @property
    def chunks_requiring_embeddings(self) -> int:
        return len(self.pending_chunks) - self.cache_hits

    @property
    def request_count(self) -> int:
        return len(self.batches)


class EmbeddingPipeline:
    def __init__(
        self,
        store: SQLiteVectorStore,
        provider: EmbeddingProvider,
        config: EmbeddingConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        self.store = store
        self.provider = provider
        self.config = config
        self.generation = provider.generation
        self.sleep = sleep
        self.jitter = jitter
        self.price = price_per_million(
            self.generation.provider, self.generation.model,
            config.price_per_million_tokens_usd,
        )
        self.attempts = 0
        self.actual_tokens = 0
        self._attempt_lock = asyncio.Lock()

    def count_tokens(self, text: str) -> int:
        # UTF-8 bytes are a conservative upper bound for BPE token counts.
        # This keeps cost preflight local and protects per-input/request limits.
        return len(text.encode("utf-8"))

    def _check_budget(self, tokens: int, requests: int) -> None:
        if self.config.max_embedding_tokens is not None and tokens > self.config.max_embedding_tokens:
            raise EmbeddingBudgetError("Estimated embedding tokens exceed budget")
        cost = Decimal(tokens) * self.price / Decimal(1_000_000)
        if (self.config.estimated_cost_limit_usd is not None
                and cost > Decimal(str(self.config.estimated_cost_limit_usd))):
            raise EmbeddingBudgetError("Estimated embedding cost exceeds budget")
        if (self.config.max_embedding_requests is not None
                and requests > self.config.max_embedding_requests):
            raise EmbeddingBudgetError("Embedding requests exceed budget")

    def _batches(self, items: list[PendingText]) -> tuple[tuple[PendingText, ...], ...]:
        batches: list[tuple[PendingText, ...]] = []
        current: list[PendingText] = []
        total = 0
        for item in items:
            if item.tokens > self.config.max_input_tokens or not item.text.strip():
                raise EmbeddingError("Embedding input is empty or exceeds per-input token limit")
            if item.tokens > self.config.max_request_tokens:
                raise EmbeddingError("Embedding input exceeds per-request token limit")
            if current and (len(current) >= self.config.batch_size
                            or total + item.tokens > self.config.max_request_tokens):
                batches.append(tuple(current))
                current, total = [], 0
            current.append(item)
            total += item.tokens
        if current:
            batches.append(tuple(current))
        return tuple(batches)

    @measured("embedding.plan_latency")
    def plan(self) -> EmbeddingPlan:
        rows = self.store.db.connection.execute(
            "SELECT id, embedding_text, embedding_text_hash FROM chunks ORDER BY note_id, position"
        )
        pending: list[tuple[str, str]] = []
        remote: dict[str, PendingText] = {}
        cached_hashes: set[str] = set()
        cache_hits = 0
        for row in rows:
            check_shutdown()
            chunk_id, text_hash = row["id"], row["embedding_text_hash"]
            if self.store.has_chunk(self.generation, chunk_id, text_hash):
                continue
            pending.append((chunk_id, text_hash))
            if text_hash in cached_hashes or self.store.has_cache(self.generation, text_hash):
                cached_hashes.add(text_hash)
                cache_hits += 1
            elif text_hash in remote:
                cache_hits += 1
            elif text_hash not in remote:
                remote[text_hash] = PendingText(
                    text_hash, row["embedding_text"], self.count_tokens(row["embedding_text"])
                )
        batches = self._batches(list(remote.values()))
        tokens = sum(item.tokens for item in remote.values())
        self._check_budget(tokens, len(batches))
        metric("embedding.plan", chunks=len(pending), cache_hits=cache_hits,
               tokens=tokens, requests=len(batches),
               estimated_cost_usd=float(Decimal(tokens) * self.price / Decimal(1_000_000)))
        return EmbeddingPlan(
            self.generation, tuple(pending), tuple(remote.values()), batches,
            cache_hits, tokens, Decimal(tokens) * self.price / Decimal(1_000_000),
        )

    async def _request(self, texts: list[str]) -> list[list[float]]:
        request_tokens = sum(self.count_tokens(text) for text in texts)
        for attempt in range(1, self.config.max_attempts + 1):
            check_shutdown()
            async with self._attempt_lock:
                if (self.config.max_embedding_requests is not None
                        and self.attempts >= self.config.max_embedding_requests):
                    raise EmbeddingBudgetError("Actual embedding request budget exhausted")
                next_tokens = self.actual_tokens + request_tokens
                if (self.config.max_embedding_tokens is not None
                        and next_tokens > self.config.max_embedding_tokens):
                    raise EmbeddingBudgetError("Actual embedding token budget exhausted")
                if (self.config.estimated_cost_limit_usd is not None
                        and Decimal(next_tokens) * self.price / Decimal(1_000_000)
                        > Decimal(str(self.config.estimated_cost_limit_usd))):
                    raise EmbeddingBudgetError("Actual embedding cost budget exhausted")
                self.attempts += 1
                self.actual_tokens = next_tokens
            try:
                with measure("embedding.request", tokens=request_tokens,
                             estimated_cost_usd=float(Decimal(request_tokens) * self.price / Decimal(1_000_000))):
                    vectors = await asyncio.wait_for(
                        self.provider.embed(texts), timeout=self.config.timeout_seconds
                    )
                if len(vectors) != len(texts):
                    raise EmbeddingError("Provider returned the wrong number of vectors")
                check_shutdown()
                return vectors
            except (EmbeddingRateLimitError, EmbeddingServiceError,
                    asyncio.TimeoutError, TimeoutError, ConnectionError):
                if attempt == self.config.max_attempts:
                    raise
                await self.sleep(min(30.0, 2 ** (attempt - 1)) * self.jitter())
        raise AssertionError("unreachable")

    @measured_async("embedding.execute")
    async def execute(self, plan: EmbeddingPlan, *, approved: bool = False) -> int:
        if plan.generation != self.generation:
            raise EmbeddingError("Plan belongs to a different embedding generation")
        if plan.request_count and not approved:
            raise EmbeddingError("Remote embeddings require explicit approval")
        results: list[list[list[float]] | None] = [None] * len(plan.batches)
        cursor = 0

        async def worker() -> None:
            nonlocal cursor
            while cursor < len(plan.batches):
                check_shutdown()
                number = cursor
                cursor += 1
                batch = plan.batches[number]
                results[number] = await self._request([item.text for item in batch])
                check_shutdown()

        tasks = [asyncio.create_task(worker())
                 for _ in range(min(self.config.max_concurrency, len(plan.batches)))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        check_shutdown()
        if any(vectors is None for vectors in results):
            raise EmbeddingError("Embedding batch stopped before completion")
        generated = {
            item.text_hash: vector
            for batch, vectors in zip(plan.batches, results, strict=True)
            if vectors is not None
            for item, vector in zip(batch, vectors, strict=True)
        }
        token_counts = {item.text_hash: item.tokens for item in plan.remote_texts}
        with self.store.db.transaction():
            check_shutdown()
            self.store.ensure_generation(self.generation)
            for chunk_id, text_hash in plan.pending_chunks:
                check_shutdown()
                vector = generated.get(text_hash)
                if vector is None:
                    vector = self.store.get_cached(self.generation, text_hash)
                if vector is None:
                    raise EmbeddingError("Embedding cache changed after preflight")
                self.store.upsert(
                    self.generation, chunk_id, text_hash, vector,
                    token_counts.get(text_hash, 0),
                )
        return len(plan.pending_chunks)

    async def embed_query(self, query: str, *, approved: bool = False) -> list[float]:
        if not approved:
            raise EmbeddingError("Remote query embedding requires explicit approval")
        tokens = self.count_tokens(query)
        self._batches([PendingText("query", query, tokens)])
        self._check_budget(tokens, 1)
        check_shutdown()
        return (await self._request([query]))[0]
