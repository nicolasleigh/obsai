"""ObsAI 统一领域异常体系定义。

该模块定义了 ObsAI 系统的完整领域错误类型层级结构（Domain Exception Hierarchy）。
所有预期的业务失败均继承自 :class:`ObsAIError`，以实现与系统级未捕获崩溃的清晰隔离。

核心设计哲学：
1. **多端适配与结构化载荷（Structured Payload Support）**：
   - 基类 :class:`ObsAIError` 挂载了可选的 ``details`` 属性。
   - HTTP/REST API 适配器可将 ``details`` 自动提取并序列化至响应包体的 ``error.details``
     字段（例如携带 Consent 授权挑战的成本预估、或 PlanDrift 变更差异），供前端直接呈现交互对话框；
   - CLI 适配层在无需结构化交互时可直接输出人类友好的简要错误文本。
2. **状态映射与行动指引（Actionable Error Classification）**：
   - 细分不同的错误子类不仅是为了分类，更是为了明确**用户的下一步行动**：
     * :class:`MissingCredentialError`（需配置环境变量而非修改 `config.toml`）
     * :class:`SemanticIndexMissingError`（需运行 `obsai index embeddings` 重建索引）
     * :class:`ConsentRequiredError`（需在交互界面点击授权确认）
     * :class:`RecoveryRequiredError`（需运行崩溃事务恢复）
   - HTTP 适配层能够将不同的业务错误无缝映射至标准的 HTTP 状态码（如 400, 404, 409, 422, 503）。
3. **精准继承树（Inheritance Tree Design）**：
   - 采用多层继承保持向下兼容。例如 :class:`MissingCredentialError` 继承自 :class:`ConfigError`，
     既保持了已有 `except ConfigError` 捕获逻辑的正常运转，又赋予了上层适配器细粒度辨识的能力；
   - :class:`PlanExpiredError` 同时继承 :class:`PlanNotFoundError` 与 :class:`ConflictError`，
     在语义上兼具“实体不存在”与“状态冲突”两重视角。
"""

from typing import Any


class ObsAIError(Exception):
    """ObsAI 系统的领域异常基类。

    所有由 ObsAI 内部业务逻辑引发的受控异常均派生自此类。
    """

    #: 可选的结构化载荷（Payload），供支持结构化响应的适配器（如 HTTP API）使用。
    #: HTTP 适配器将其置于 JSON 响应包的 ``error.details`` 中；CLI 适配器可根据需要读取或忽略。
    #: 挂载在基类而非特定子类上，便于适配层以统一策略处理所有可能携带元数据的错误。
    details: Any = None


# ---------------------------------------------------------------------------
# 配置与凭证相关异常 (Configuration & Credentials)
# ---------------------------------------------------------------------------


class ConfigError(ObsAIError):
    """配置加载、解析或验证失败异常。

    通常表示 `config.toml` 格式错误、缺少必填字段、类型不匹配或逻辑冲突。
    """


class MissingCredentialError(ConfigError):
    """请求远程提供商（Provider）服务时缺失必需的 API 凭证或密钥。

    设计说明：
    - 作为 :class:`ConfigError` 的子类，是因为缺少凭证从语义上属于“配置不完备导致无法提供服务”；
    - 独立成特定子类是因为**解决途径截然不同**：用户应当导出环境变量（如 ``export OPENAI_API_KEY="..."``），
      而非修改配置文件。
    - HTTP 适配器可根据类名派生出 ``missing_credential`` 错误码，指导前端提示“请配置 OPENAI_API_KEY”，
      避免错误地引导用户检查本就无误的配置文件。
    - 继承关系确保现有的 ``except ConfigError`` 继续透明生效，且 CLI 的错误文案保持不变。
    """


# ---------------------------------------------------------------------------
# 实体与资源查找异常 (Entity & Resource Lookup)
# ---------------------------------------------------------------------------


class NotFoundError(ObsAIError):
    """请求的实体在派生索引或仓库中不存在。

    设计说明：
    - 与 :class:`ConfigError` 截然不同：请求本身格式合法且配置完备，纯粹是因为目标资源不存在
      （例如已删除笔记的书签失效）。
    - HTTP 适配器直接将其映射为 HTTP 404，允许前端明确渲染“资源已删除/不存在”而非“系统故障”。
    """


# ---------------------------------------------------------------------------
# 用户授权与审批流程异常 (Consent & Approval Workflow)
# ---------------------------------------------------------------------------


class ConsentRequiredError(ObsAIError):
    """不可降级的查询或变更操作缺少必需的用户前置授权。

    常见触发场景：
    - 调用方发起了语义向量检索，且显式禁止降级（degrade）为全文关键字检索，但未在请求中附带有效的
      授权凭证（Consent Token）。
    - 非配置问题，只需用户在界面上明确完成一次授权决策（如同意 API 调用花销）即可继续。
    - ``details`` 中携带了本次操作对应的授权挑战详情（如预估消耗 Token、预估花费及过期 TTL），
      前端 UI 可据此直接弹出与预检阶段一致的确认对话框。
    """

    def __init__(self, message: str, *, details: Any = None) -> None:
        """初始化 ConsentRequiredError。

        Args:
            message: 错误描述文本。
            details: 授权挑战相关的结构化元数据（如预估费用、操作范围）。
        """
        super().__init__(message)
        self.details = details


class ConsentExpiredError(ObsAIError):
    """用户提交的授权令牌已超时过期。

    常见触发场景：
    - 在 CLI 确认提示界面或 HTTP 弹窗中停留时间超过了授权挑战的生命周期（TTL）。
    - 解决方案是重新发起查询或规划流程，获取新的授权挑战。
    """


class SemanticIndexMissingError(ConfigError):
    """当前配置的 Embedding 模型世代在向量索引库中无任何可用向量。

    设计说明：
    - 属于 :class:`ConfigError`，因为系统现有的派生状态确实无法满足检索请求；
    - 独立成专门类是因为修复手段不同：无需修改 `config.toml`，而是需要运行
      ``obsai index embeddings`` 命令以构建或补充向量。
    - 仅由 HTTP 适配层抛出；CLI 端则直接复用 `search` 自带的基准 `ConfigError` 提示。
    """


# ---------------------------------------------------------------------------
# Vault 与解析引擎异常 (Vault & Parser)
# ---------------------------------------------------------------------------


class VaultError(ObsAIError):
    """Vault 仓库目录无法扫描、访问、解析或读取。

    包含根目录不存在、路径越界逃逸、权限不足或读取 I/O 故障等情形。
    """


class ParseError(ObsAIError):
    """Markdown 笔记或 YAML Frontmatter 无法安全解析。

    包含 YAML 语法错误、未闭合 frontmatter、或生成的 AST 数据无法通过 Pydantic 模型校验等。
    """


class SchemaError(ObsAIError):
    """SQLite 数据库架构缺失、损坏或版本不兼容。"""


# ---------------------------------------------------------------------------
# 向量与嵌入模型异常 (Embeddings & Semantic Search)
# ---------------------------------------------------------------------------


class EmbeddingError(ObsAIError):
    """向量生成、存储或向量检索失败基类。"""


class EmbeddingBudgetError(EmbeddingError):
    """预检（Preflight）或实时请求的 Token / 成本预算超出阈值限制。"""


class EmbeddingRateLimitError(EmbeddingError):
    """远端嵌入提供商触发速率限制而临时拒绝请求（HTTP 429）。"""


class EmbeddingServiceError(EmbeddingError):
    """远端嵌入服务提供商发生临时性服务端内部故障（HTTP 5xx）。"""


class SemanticUnavailableError(EmbeddingError):
    """语义后端不可达或明确拒绝了查询。

    设计说明：
    - 属于 :class:`EmbeddingError` 的细分子类；
    - 独立成类的核心原因在于 HTTP 状态码映射：这属于下游服务不可用的故障（Downstream Failure），
      应精准映射为 HTTP 503 Service Unavailable，而非通用的 500 内部服务器错误。
    - 由 HTTP 适配层负责在无法连通 Provider 或向量库服务时抛出。
    """


# ---------------------------------------------------------------------------
# 上下文预算与 LLM 异常 (Context Budget & Answer Generation)
# ---------------------------------------------------------------------------


class ContextError(ObsAIError):
    """检索出的证据（Evidence）超出了配置的 LLM 上下文预算限制。"""


class LLMError(ObsAIError):
    """问答提供商（Answer Provider / LLM）无法成功生成回答。"""


# ---------------------------------------------------------------------------
# 安全写入、事务与变更计划异常 (Safe Write, Transactions & Plans)
# ---------------------------------------------------------------------------


class SafeWriteError(ObsAIError):
    """提议的 Vault 修改计划非法或无法安全提交至文件系统。"""


class ConflictError(SafeWriteError):
    """并发写冲突异常（乐观锁检测失败）。

    表明自变更计划（Change Plan）生成以来，目标源文件在物理磁盘上已被外部修改或删除。
    """


class PlanNotFoundError(NotFoundError):
    """请求的变更计划（Change Plan）不存在或已被清理。"""


class PlanExpiredError(PlanNotFoundError, ConflictError):
    """请求的变更计划已超过其最大生存时间（TTL）。

    多重继承自 :class:`PlanNotFoundError` 与 :class:`ConflictError`，
    表达该计划既无法被找到执行，又构成了操作时效上的状态冲突。
    """


class PlanDriftError(ObsAIError):
    """已批准的计划与当前 Vault 或索引的实时状态发生漂移（Plan Drift）。

    常见触发场景：
    - 用户审核并批准了某项批量变更计划（如向量重构或笔记整理），但在最终提交执行前，
      相关文件、文本切片、Token 数量或预估费用发生了实质性变动，超出了批准范围。
    - ``details`` 携带实时计算出的全新计划数据，调用方可据此呈现对比界面并提示用户重新确认。
    """

    def __init__(
        self,
        message: str = "Plan has drifted; review and approve the updated plan",
        *,
        details: Any = None,
    ) -> None:
        """初始化 PlanDriftError。

        Args:
            message: 漂移提示文本，默认为建议重新审查并批准最新计划。
            details: 包含最新重算计划详情的载荷对象。
        """
        super().__init__(message)
        self.details = details


class CollisionError(SafeWriteError):
    """目标写入路径已存在文件，防止意外覆盖。"""


class InvalidEncodingError(SafeWriteError):
    """笔记文件非合法 UTF-8 编码文本。"""


class TransactionError(SafeWriteError):
    """多文件 Vault 原子事务无法正常提交或回滚。"""


class RecoveryRequiredError(TransactionError):
    """检测到未完成的崩溃事务日志（Crash Journal），必须显式执行恢复后方可继续写入。"""
