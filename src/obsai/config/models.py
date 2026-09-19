"""Phase 0 configuration fields.

该模块定义了系统的全局配置模型层，基于 Pydantic V2 与 Pydantic Settings 构建。

核心架构与设计原则：
1. 领域关注点分离（Separation of Concerns）：将知识库路径（Vault）、SQLite 索引（Index）、
   向量模型（Embedding）、证据问答（Ask）以及收件箱整理（Organize）划分为独立的配置子域。
2. 严密的防御性工程：所有模型均启用 extra="forbid"，杜绝拼写错误导致的配置静默失效；
   字段配置了严格的范围约束（gt, le, ge），防止非法数值引发运行时崩溃。
3. 路径遍历防护（Path Traversal Guard）：对收件箱目录执行严格的纯相对路径校验，
   禁止包含绝对路径、反斜杠和 ".."，防止安全越界逃逸。
4. 12-Factor 环境变量无缝覆盖：支持以 "OBSAI_" 为前缀、"__" 双下划线为层级分隔符的
   环境变量自动注入（如 OBSAI_ASK__MODEL），且环境变量优先级高于配置文件。
"""

from pathlib import Path
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class VaultConfig(BaseModel):
    """Obsidian 知识库本地路径配置。"""

    model_config = ConfigDict(extra="forbid")  # 严禁未知字段

    path: Path | None = None  # Obsidian 知识库真实物理根目录（未配置时由 CLI 参数或当前工作区指定）


class IndexConfig(BaseModel):
    """SQLite 索引数据库存储配置。"""

    model_config = ConfigDict(extra="forbid")

    database: Path | None = None  # 索引数据库文件路径（默认存放在 vault 目录下的 .obsai/index.db）


class EmbeddingConfig(BaseModel):
    """向量嵌入模型、批处理与成本控制配置。"""

    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"  # 向量模型提供商（默认 openai）
    model: str = "text-embedding-3-small"  # 向量模型名称
    model_version: str | None = None  # 模型版本号（可选）
    dimensions: int = Field(default=1536, gt=0)  # 向量输出维度（必须大于 0）
    batch_size: int = Field(default=64, gt=0, le=2048)  # 单批次提交的切片数量（1 ~ 2048）
    max_concurrency: int = Field(default=2, gt=0)  # API 最大并发请求数（控制并发限流）
    max_input_tokens: int = Field(default=8192, gt=0, le=8192)  # 单条切片允许的最大 Token 长度
    max_request_tokens: int = Field(default=300000, gt=0, le=300000)  # 单次请求累计 Token 总数硬上限
    timeout_seconds: float = Field(default=30, gt=0)  # 向量请求超时时间（秒）
    max_attempts: int = Field(default=4, gt=0)  # 网络错误或限流时的最大重试次数

    # --- 成本与配额安全熔断控制 ---
    max_embedding_tokens: int | None = Field(default=None, ge=0)  # 本次执行允许消耗的最大 Token 总数配额
    estimated_cost_limit_usd: float | None = Field(default=None, ge=0)  # 预估消费金额上限（美元，防账单失控）
    max_embedding_requests: int | None = Field(default=None, ge=0)  # 允许调用的最大 API 请求次数
    price_per_million_tokens_usd: float | None = Field(default=None, ge=0)  # 每百万 Token 单价（美元，用于成本预估）


class AskConfig(BaseModel):
    """证据问答流水线与 Agent 规划器模型配置。"""

    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"  # 问答/规划模型提供商
    model: str = "gpt-4.1-mini"  # 问答与规划使用的大模型名称
    timeout_seconds: float = Field(default=60, gt=0)  # 单次 LLM 请求超时时间（秒）
    max_output_tokens: int = Field(default=1024, gt=0)  # 模型生成内容的最大 Token 上限
    max_context_tokens: int = Field(default=12000, gt=0)  # 组装到 Prompt 里的总上下文 Token 预算上限
    max_evidence_tokens: int = Field(default=2500, gt=0)  # 检索切片证据正文的最大 Token 预算
    max_chunks: int = Field(default=6, gt=0)  # 单次回答精选注入的最相关切片数量上限


class OrganizeConfig(BaseModel):
    """收件箱笔记归档与整理配置。"""

    model_config = ConfigDict(extra="forbid")

    inbox: str = "Inbox"  # 待整理笔记存放的收件箱相对目录名

    @field_validator("inbox")
    @classmethod
    def valid_inbox(cls, value: str) -> str:
        """校验收件箱路径，防御目录遍历攻击（Path Traversal Guard）。"""
        value = value.strip().rstrip("/")
        parts = PurePosixPath(value).parts
        # 安全防御规则：禁止空路径、当前目录 "."、绝对路径 "/"、Windows 风格 "\" 以及任意层级的 ".."
        if (not value or value == "." or value.startswith("/") or "\\" in value
                or any(part in (".", "..") for part in parts)):
            raise ValueError("inbox must be a safe Vault-relative directory")
        return value


class Settings(BaseSettings):
    """全局总设置类，负责统一聚合各子域配置并处理环境变量多源覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix="OBSAI_",          # 识别以 OBSAI_ 开头的系统环境变量
        env_nested_delimiter="__",    # 双下划线映射嵌套模型（如 OBSAI_ASK__MODEL）
        extra="forbid",               # 严禁未知多余字段
    )

    vault: VaultConfig = Field(default_factory=VaultConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    ask: AskConfig = Field(default_factory=AskConfig)
    organize: OrganizeConfig = Field(default_factory=OrganizeConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """定制配置源优先级：将系统环境变量置于首位，确保可按需动态覆盖 TOML 文件。"""
        return env_settings, init_settings, dotenv_settings, file_secret_settings
