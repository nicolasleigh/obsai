"""Preflight, budget, batch, retry, and cache orchestration.

该模块是系统向量嵌入子系统的核心编排引擎（Embedding Pipeline），
负责组织管理从知识切片发现到向量入库的全生命周期。

核心架构与设计原则：
1. 两阶段提交模式（Two-Phase Planning & Execution）：
   将产生外部网络与计费开销的操作严格划分为只读无副作用的“预检（Plan）”与“受控执行（Execute）”两阶段。
   执行前必须经过显式审批（Approved Gate），杜绝意外消费与不可预期的调用。
2. 多级智能去重与缓存穿透防护（Deduplication & Multi-Tier Caching）：
   - 切片级增量跳过：已存在有效向量的切片直接忽略；
   - 数据库全局缓存命中：相同文本哈希在相同代际下直接复用历史向量，避免重复调用 API；
   - 批内去重：单次规划中重复出现的文本（如通用模板、免责声明）仅向 API 请求一次。
3. 确定性预算熔断机制（Budget Guard）：
   基于 Token 数量、美元金额（Decimal 高精度定点计算）及 API 请求次数三重配额进行防御性熔断。
   在预检期与并发重试期均实施严格检查，防止因重试放大而超出用户预期支出。
4. 本地轻量 Token 估算（Conservative UTF-8 Bound）：
   采用 UTF-8 字节长度作为 BPE Token 计数的保守上界，无需在客户端本地加载庞大的分词器词表，
   使预检完全离线、轻量、毫秒级响应。
5. 弹性重试与抖动调度（Exponential Backoff with Full Jitter）：
   对 429 限流及 5xx 瞬态服务故障实施指数退避并结合随机抖动，防止并发重试引发服务端惊群效应；
   严格控制客户端并发请求上限（max_concurrency）。
6. 结构化并发与故障一致性（Crash Consistency & Graceful Shutdown）：
   任一并发协程崩溃时自动取消并回收兄弟任务，杜绝孤儿协程；
   在底层数据库写入关键段通过 defer_shutdown() 延迟响应 SIGINT/SIGTERM 中断信号，保证 SQLite 事务完整性。
"""

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
from obsai.shutdown import check_shutdown, defer_shutdown
from obsai.telemetry import measure, measured, measured_async, metric


@dataclass(frozen=True)
class PendingText:
    """待向远程模型请求向量的去重文本实体。"""

    text_hash: str  # 归一化文本的 SHA-256 唯一哈希
    text: str       # 原始待向量化文本
    tokens: int     # 估算的 Token 消耗数


@dataclass(frozen=True)
class EmbeddingPlan:
    """向量化执行规划报告，封装预检阶段的所有决策与预算估算结果。"""

    generation: EmbeddingGeneration                 # 目标向量模型代际规范
    pending_chunks: tuple[tuple[str, str], ...]     # 待写入向量库的切片元组 (chunk_id, text_hash)
    remote_texts: tuple[PendingText, ...]           # 需要向远程 API 发起的去重文本元组
    batches: tuple[tuple[PendingText, ...], ...]    # 打包装箱后的批次列表
    cache_hits: int                                 # 命中全局缓存与批内去重的切片总数
    estimated_tokens: int                           # 预计消耗的 Token 总量
    estimated_cost_usd: Decimal                     # 预估花费金额（高精度 Decimal，美元）

    @property
    def chunks_requiring_embeddings(self) -> int:
        """实际需要向远程接口请求向量的切片总数。"""
        return len(self.pending_chunks) - self.cache_hits

    @property
    def request_count(self) -> int:
        """打包后的 API 网络请求总批次数。"""
        return len(self.batches)


class EmbeddingPipeline:
    """向量化调度流水线，负责执行预检、预算审核、动态批处理打包、并发限流与入库事务。"""

    def __init__(
        self,
        store: SQLiteVectorStore,
        provider: EmbeddingProvider,
        config: EmbeddingConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        """初始化向量流水线，支持依赖注入以实现高可靠单元测试。

        :param store: 向量数据库存储后端
        :param provider: 具体的向量模型提供商适配器
        :param config: 向量与成本配置
        :param sleep: 异步等待函数（单测可注入快速 Mock）
        :param jitter: 随机抖动因子生成器（单测可注入确定性函数）
        """
        self.store = store
        self.provider = provider
        self.config = config
        self.generation = provider.generation
        self.sleep = sleep
        self.jitter = jitter
        # 获取当前模型的每百万 Token 单价（美元），支持用户自定义单价配置
        self.price = price_per_million(
            self.generation.provider, self.generation.model,
            config.price_per_million_tokens_usd,
        )
        self.attempts = 0            # 实际发起的网络请求批次计数器
        self.actual_tokens = 0        # 实际调用的累计 Token 计数器
        self._attempt_lock = asyncio.Lock()  # 保护并发执行与重试时的原子预算计数

    def count_tokens(self, text: str) -> int:
        """估算文本的 Token 消耗上限。

        工程权衡：UTF-8 字节数是 BPE 分词算法非常安全的保守上界（Conservative Upper Bound）。
        采用字节数使得成本预检完全在本地进行，无需下载或加载繁重的分词器（如 tiktoken），
        同时保证不会低估 Token 导致越过 API 限制或预算超支。
        """
        return len(text.encode("utf-8"))

    def check_budget(self, tokens: int, requests: int) -> None:
        """熔断预算校验：拒绝任何预估 Token 数、美元消费或请求批次数超出配置上限的工作。

        :param tokens: 估算的 Token 消耗总量
        :param requests: 估算的请求批次总数
        :raises EmbeddingBudgetError: 任一指标越界时抛出
        """
        # 1. 校验 Token 总量配额
        if self.config.max_embedding_tokens is not None and tokens > self.config.max_embedding_tokens:
            raise EmbeddingBudgetError("Estimated embedding tokens exceed budget")
        # 2. 校验美元消费上限（采用 Decimal 高精度乘除，杜绝浮点数舍入误差）
        cost = Decimal(tokens) * self.price / Decimal(1_000_000)
        if (self.config.estimated_cost_limit_usd is not None
                and cost > Decimal(str(self.config.estimated_cost_limit_usd))):
            raise EmbeddingBudgetError("Estimated embedding cost exceeds budget")
        # 3. 校验总请求批次数上限
        if (self.config.max_embedding_requests is not None
                and requests > self.config.max_embedding_requests):
            raise EmbeddingBudgetError("Embedding requests exceed budget")

    def _batches(self, items: list[PendingText]) -> tuple[tuple[PendingText, ...], ...]:
        """贪心分批装箱算法：依据 batch_size 与 max_request_tokens 双重约束切分批次。

        :param items: 待发送的去重文本列表
        :return: 嵌套只读元组，每个子元组代表一个 API 请求批次
        :raises EmbeddingError: 若单条切片为空、超出单条输入上限或超出请求上限
        """
        batches: list[tuple[PendingText, ...]] = []
        current: list[PendingText] = []
        total = 0
        for item in items:
            # 单项防御：分别报告空白文本与超出单条输入上限，避免调用方无法判断修复方向。
            if not item.text.strip():
                raise EmbeddingError("Embedding input is empty")
            if item.tokens > self.config.max_input_tokens:
                raise EmbeddingError(
                    "Embedding input exceeds per-input token limit "
                    f"(estimated={item.tokens}, limit={self.config.max_input_tokens})"
                )
            # 单项超出单次请求总容量上限
            if item.tokens > self.config.max_request_tokens:
                raise EmbeddingError("Embedding input exceeds per-request token limit")
            # 当条数达到 batch_size，或累积 Token 加入当前项后会突破单次请求上限时，完成当前批次打包
            if current and (len(current) >= self.config.batch_size
                            or total + item.tokens > self.config.max_request_tokens):
                batches.append(tuple(current))
                current, total = [], 0
            current.append(item)
            total += item.tokens
        # 收尾最后一个批次
        if current:
            batches.append(tuple(current))
        return tuple(batches)

    @measured("embedding.plan_latency")
    def plan(self) -> EmbeddingPlan:
        """只读预检规划：扫描未向量化切片，执行去重、全局缓存比对、分批装箱与预算审核。

        该过程为纯只读操作，不产生网络调用与费用支出。

        :return: 包含完整规划细节的不可变 EmbeddingPlan 报告
        :raises EmbeddingBudgetError: 预估开销超出配置限额
        """
        # 按照笔记与其内部位置顺序稳定拉取所有切片
        rows = self.store.db.connection.execute(
            "SELECT id, embedding_text, embedding_text_hash FROM chunks ORDER BY note_id, position"
        )
        pending: list[tuple[str, str]] = []
        remote: dict[str, PendingText] = {}
        cached_hashes: set[str] = set()
        cache_hits = 0
        for row in rows:
            check_shutdown()  # 检查是否收到外部退出信号
            chunk_id, text_hash = row["id"], row["embedding_text_hash"]
            # 增量检查：若该切片在当前代际下已拥有匹配的向量，跳过
            if self.store.has_chunk(self.generation, chunk_id, text_hash):
                continue
            pending.append((chunk_id, text_hash))
            # 命中数据库已有缓存（相同文本已在其他切片生成过向量）
            if text_hash in cached_hashes or self.store.has_cache(self.generation, text_hash):
                cached_hashes.add(text_hash)
                cache_hits += 1
            # 命中本批次内去重（相同文本已在本轮待发列表中）
            elif text_hash in remote:
                cache_hits += 1
            # 全新未向量化文本
            elif text_hash not in remote:
                remote[text_hash] = PendingText(
                    text_hash, row["embedding_text"], self.count_tokens(row["embedding_text"])
                )
        # 将去重后的文本列表执行流式装箱打包
        batches = self._batches(list(remote.values()))
        tokens = sum(item.tokens for item in remote.values())
        # 执行预检预算熔断断言
        self.check_budget(tokens, len(batches))
        # 记录预检阶段遥测指标
        metric("embedding.plan", chunks=len(pending), cache_hits=cache_hits,
               tokens=tokens, requests=len(batches),
               estimated_cost_usd=float(Decimal(tokens) * self.price / Decimal(1_000_000)))
        return EmbeddingPlan(
            self.generation, tuple(pending), tuple(remote.values()), batches,
            cache_hits, tokens, Decimal(tokens) * self.price / Decimal(1_000_000),
        )

    async def _request(self, texts: list[str]) -> list[list[float]]:
        """执行单批次网络请求，集成并发安全预算核减、超时控制与指数退避重试。

        :param texts: 该批次待向量化的纯文本列表
        :return: 与输入一一对应的高维向量列表
        :raises EmbeddingBudgetError: 运行时因重试等导致累计预算耗尽
        :raises EmbeddingError: 提供商返回数量不匹配或不可恢复错误
        """
        request_tokens = sum(self.count_tokens(text) for text in texts)
        for attempt in range(1, self.config.max_attempts + 1):
            check_shutdown()
            # 加锁原子核算实际累计预算，防止重试或并发工程导致预算被击穿
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
                # 记录单次网络请求的耗时与 Token 统计
                with measure("embedding.request", tokens=request_tokens,
                             estimated_cost_usd=float(Decimal(request_tokens) * self.price / Decimal(1_000_000))):
                    vectors = await asyncio.wait_for(
                        self.provider.embed(texts), timeout=self.config.timeout_seconds
                    )
                # 完整性校验：返回向量数量必须与输入文本数绝对一致
                if len(vectors) != len(texts):
                    raise EmbeddingError("Provider returned the wrong number of vectors")
                check_shutdown()
                return vectors
            except (EmbeddingRateLimitError, EmbeddingServiceError,
                    asyncio.TimeoutError, TimeoutError, ConnectionError):
                # 遇到瞬态故障（429 限流、5xx 服务端错误、网络超时、连接断开）时进行指数退避重试
                if attempt == self.config.max_attempts:
                    raise  # 已达最大尝试次数，向上抛出
                # Full Jitter 指数退避：min(30s, 2^(attempt-1)) * 随机因子[0, 1)
                await self.sleep(min(30.0, 2 ** (attempt - 1)) * self.jitter())
        raise AssertionError("unreachable")

    @measured_async("embedding.execute")
    async def execute(
        self,
        plan: EmbeddingPlan,
        *,
        approved: bool = False,
        on_batch: Callable[[int, int], None] | None = None,
    ) -> int:
        """根据预检规划并发执行向量生成，并在安全事务内完成原子落库。

        :param plan: 由 plan() 生成的不可变执行规划
        :param approved: 是否已获得显式审批确认（存在网络请求时必须为 True）
        :param on_batch: 可选的批次完成进度回调函数，签名 (completed_batches, total_batches)
        :return: 成功建立/更新向量的切片总数
        :raises EmbeddingError: 代际不一致、未经审批、部分批次未完成或缓存状态漂移
        """
        # 前置断言：严禁跨代际执行
        if plan.generation != self.generation:
            raise EmbeddingError("Plan belongs to a different embedding generation")
        # 显式审批闸门：只要包含远程网络请求，必须显式审批，防止未授权计费
        if plan.request_count and not approved:
            raise EmbeddingError("Remote embeddings require explicit approval")

        # 预先开辟插槽列表，使得并发返回结果能够按原始批次编号精确定位
        results: list[list[list[float]] | None] = [None] * len(plan.batches)
        cursor = 0
        completed_batches = 0
        batch_lock = asyncio.Lock()

        # 工作协程：流式认领批次并执行网络请求
        async def worker() -> None:
            nonlocal cursor, completed_batches
            while cursor < len(plan.batches):
                check_shutdown()
                number = cursor
                cursor += 1
                batch = plan.batches[number]
                # 发送单批次请求
                results[number] = await self._request([item.text for item in batch])
                # 更新完成进度并通知回调
                async with batch_lock:
                    completed_batches += 1
                    if on_batch is not None:
                        on_batch(completed_batches, len(plan.batches))
                check_shutdown()

        # 根据配置的最大并发限制（max_concurrency）启动工作协程池
        tasks = [asyncio.create_task(worker())
                 for _ in range(min(self.config.max_concurrency, len(plan.batches)))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # 结构化并发异常处理：任一协程抛出异常，立即取消所有其他正在运行的任务并等待回收
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        check_shutdown()

        # 校验所有批次是否全部完成
        if any(vectors is None for vectors in results):
            raise EmbeddingError("Embedding batch stopped before completion")

        # 将所有批次的生成向量组装为 text_hash -> vector 映射
        generated = {
            item.text_hash: vector
            for batch, vectors in zip(plan.batches, results, strict=True)
            if vectors is not None
            for item, vector in zip(batch, vectors, strict=True)
        }
        token_counts = {item.text_hash: item.tokens for item in plan.remote_texts}

        # 故障一致性持久化：通过 defer_shutdown 延迟处理中断信号，确保事务完整提交，避免破坏数据库
        with defer_shutdown():
            with self.store.db.transaction():
                self.store.ensure_generation(self.generation)
                for chunk_id, text_hash in plan.pending_chunks:
                    # 优先取新生成的向量，若无则从已有缓存读取
                    vector = generated.get(text_hash)
                    if vector is None:
                        vector = self.store.get_cached(self.generation, text_hash)
                    if vector is None:
                        # 预检后缓存被意外篡改的防御
                        raise EmbeddingError("Embedding cache changed after preflight")
                    self.store.upsert(
                        self.generation, chunk_id, text_hash, vector,
                        token_counts.get(text_hash, 0),
                    )
        return len(plan.pending_chunks)

    async def embed_query(self, query: str, *, approved: bool = False) -> list[float]:
        """为单条用户查询问题生成检索向量。

        :param query: 用户输入的查询文本
        :param approved: 显式审批确认
        :return: 浮点数向量
        :raises EmbeddingError: 未经审批
        """
        if not approved:
            raise EmbeddingError("Remote query embedding requires explicit approval")
        tokens = self.count_tokens(query)
        # 借用分批逻辑对单条查询执行输入长度合规性检查
        self._batches([PendingText("query", query, tokens)])
        # 检查单条查询的预算
        self.check_budget(tokens, 1)
        check_shutdown()
        return (await self._request([query]))[0]
