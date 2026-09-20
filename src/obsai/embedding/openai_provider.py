"""OpenAI embeddings adapter. Construction and preflight do not make network calls.

该模块是对接 OpenAI 官方向量嵌入 API（Embeddings API）的高可靠适配器，
实现了 obsai.embedding.models.EmbeddingProvider 协议契约。

核心设计哲学与工程防护：
1. 构造与预检零网络副作用（Zero Network I/O in Construction）：
   初始化时仅校验提供商类型与记录配置，不发起任何连通性探测或 API Key 预检，确保对象构建轻量、可测试且无副作用。
2. 延迟凭证提取（Lazy Credential Lookup）：
   仅在发起 embed 调用时从环境变量中读取 OPENAI_API_KEY，支持容器环境下的动态注入与密钥轮转；若缺失则抛出 MissingCredentialError。
3. 禁用底层黑盒重试（max_retries=0）：
   OpenAI SDK 默认内置自动重试，本适配器显式将其置为 0，将重试决策、指数退避、并发限流与成本审计完全上收至
   上层流水线（obsai.embedding.pipeline）统一编排，杜绝多层重试叠加导致的“重试风暴（Retry Storm）”。
4. 细粒度异常分类映射（Granular Exception Hierarchy）：
   精确区分 429 频控、网络断开、超时、5xx 服务端瞬态故障（可重试）与 4xx 客户端逻辑错误（不可重试），
   为上层调度提供清晰的重试与降级依据。
5. 返回结果保序与完整性守卫（Order Preservation & Integrity Guard）：
   利用返回体中的 item.index 进行防御性排序，并断言返回索引序列严格覆盖 [0, len(texts)-1]，
   防止因乱序或漏包导致向量与切片内容错位。
"""

import os

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from obsai.embedding.models import EmbeddingGeneration
from obsai.errors import (
    ConfigError,
    EmbeddingError,
    EmbeddingRateLimitError,
    EmbeddingServiceError,
    MissingCredentialError,
)


class OpenAIEmbeddingProvider:
    """OpenAI 向量嵌入服务适配器。

    负责将分块切片文本转换为与指定代际（EmbeddingGeneration）兼容的高维稠密向量。
    """

    def __init__(
        self,
        generation: EmbeddingGeneration,
        timeout_seconds: float,
        base_url: str | None = None,
    ):
        """初始化 OpenAI 向量提供商适配器。

        :param generation: 向量代际配置（必须声明 provider="openai"）
        :param timeout_seconds: 单次 API 请求的超时时间阈值（秒）
        :raises ConfigError: 若代际配置中的提供商非 "openai" 时抛出
        """
        if generation.provider != "openai":
            raise ConfigError(f"Unsupported embedding provider: {generation.provider}")
        self.generation = generation
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """异步批量将文本列表转换为对应维度的浮点数向量列表。

        :param texts: 待向量化的纯文本字符串列表
        :return: 顺序与输入严格一一对应的浮点数向量列表
        :raises MissingCredentialError: 若环境中未配置 OPENAI_API_KEY
        :raises EmbeddingRateLimitError: 触发 OpenAI 429 限流
        :raises TimeoutError: 请求超时
        :raises ConnectionError: 底层网络连接异常
        :raises EmbeddingServiceError: OpenAI 5xx 服务端错误或返回结果缺失/错乱
        :raises EmbeddingError: OpenAI 4xx 客户端参数或权限错误
        """
        # 快速短路返回：若传入空文本列表，直接返回空结果，避免浪费网络调用与客户端初始化
        if not texts:
            return []

        # 延迟获取 API Key 凭证
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise MissingCredentialError("OPENAI_API_KEY is required for remote embeddings")

        # 使用异步上下文管理器管理 AsyncOpenAI 客户端，确保 HTTP 连接池资源能被安全及时释放
        # 设置 max_retries=0，交由上层 pipeline 统筹管理重试和限流
        async with AsyncOpenAI(
            api_key=key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            max_retries=0,
        ) as client:
            try:
                response = await client.embeddings.create(
                    model=self.generation.model,
                    input=texts,
                    dimensions=self.generation.dimensions,  # 显式指定维度（利用 Matryoshka 特性）
                    encoding_format="float",               # 明确要求返回浮点稠密向量
                )
            except RateLimitError as exc:
                # 捕获 HTTP 429 限流错误，便于上层捕获后执行退避重试
                raise EmbeddingRateLimitError("OpenAI rate limit (429)") from exc
            except APITimeoutError as exc:
                # 请求等待超时
                raise TimeoutError("OpenAI embedding request timed out") from exc
            except APIConnectionError as exc:
                # 底层网络连接故障（DNS 解析失败、连接重置等）
                raise ConnectionError("OpenAI embedding network error") from exc
            except APIStatusError as exc:
                # 5xx 服务端内部故障：可重试错误
                if exc.status_code >= 500:
                    raise EmbeddingServiceError(f"OpenAI service error ({exc.status_code})") from exc
                # 4xx 客户端错误（如格式错误、Token 超长等）：不可重试错误
                raise EmbeddingError(f"OpenAI rejected embedding request ({exc.status_code})") from exc

        # 防御性按返回项原始 index 严格重排，确保与输入的 texts 保持 1:1 物理顺序对齐
        ordered = sorted(response.data, key=lambda item: item.index)

        # 严密的数据完整性守卫：检查结果数量是否完全吻合，且索引必须连续覆盖 [0, len(texts)-1]
        if len(ordered) != len(texts) or [item.index for item in ordered] != list(range(len(texts))):
            raise EmbeddingServiceError("OpenAI returned incomplete embedding results")

        # 提取排序后的高维向量列表
        return [item.embedding for item in ordered]
