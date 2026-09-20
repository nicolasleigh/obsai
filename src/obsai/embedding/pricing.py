"""One place for editable provider cost estimates (USD per million input tokens).

该模块是系统向量嵌入调用成本的唯一定价来源（Single Source of Truth, SSOT）。

核心设计哲学与原则：
1. 财务级定点精度（Financial-grade Decimal Precision）：
   涉及金钱计算与预算熔断比对时，严禁使用存在 IEEE 754 浮点数截断失真的原生 float，
   全程采用标准库 Decimal，杜绝舍入误差。
2. 三级费率确定策略（Hierarchical Pricing Resolution）：
   - 优先级 1（最高）：用户在配置文件或环境变量中显式指定的覆盖价格（override）；
   - 优先级 2：官方内置的标准模型费率字典；
   - 优先级 3：若均为未知，严格快速失败（Fail-Fast）抛出 ConfigError。
3. 拒绝隐式侥幸（Zero Implicit Free Assumptions）：
   对于未知的厂商或模型，系统绝不擅自假设为 0 元免费，必须强制用户配置明确的费率，
   彻底防止因隐式调用产生的非预期账单冲击。
"""

from decimal import Decimal

from obsai.errors import ConfigError

# 官方基准模型定价对照表（核对时间：2026-09-13）
# 计量单位：美元 / 每 100 万输入 Token（USD per million input tokens）
# 用户可通过 config.toml 或环境变量随时覆盖，以适应厂商调价或企业折扣
OPENAI_EMBEDDING_USD_PER_MILLION = {
    "text-embedding-3-small": Decimal("0.02"),  # 0.02 美元 / 100 万 Tokens
    "text-embedding-3-large": Decimal("0.13"),  # 0.13 美元 / 100 万 Tokens
}

LOCAL_EMBEDDING_PROVIDERS = {"ollama"}


def price_per_million(provider: str, model: str, override: float | None) -> Decimal:
    """计算并返回指定模型每百万输入 Token 的美元单价。

    决策优先级：
    1. 若提供了 override（用户配置），优先采用 override；
    2. 若为内置支持的 provider 与 model，采用内置标准价；
    3. 否则抛出 ConfigError，要求用户显式配置单价。

    :param provider: 向量模型提供商名称（如 "openai"）
    :param model: 向量模型标识符（如 "text-embedding-3-small"）
    :param override: 用户在配置中显式指定的单价（美元/百万Token），若未指定则为 None
    :return: 高精度的 Decimal 单价
    :raises ConfigError: 当模型未知且用户未显式提供 override 单价时抛出
    """
    # 优先级 1：优先采用用户在配置中显式声明的自定义价格（支持折扣价或代理商单价）
    # 通过 str(override) 转换，消除从 float 构建 Decimal 时的微小浮点杂质
    if override is not None:
        return Decimal(str(override))

    # 优先级 2：匹配内置的官方基准单价
    if provider == "openai" and model in OPENAI_EMBEDDING_USD_PER_MILLION:
        return OPENAI_EMBEDDING_USD_PER_MILLION[model]

    # Ollama runs on the local machine, so its embedding requests do not incur
    # provider charges. Keep the estimate explicit rather than requiring users
    # to add a meaningless price override for every local model.
    if provider in LOCAL_EMBEDDING_PROVIDERS:
        return Decimal("0")

    # 优先级 3：防御性熔断，拒绝隐式估价，防止未知模型在用户无感知情况下产生账单
    raise ConfigError(
        "Unknown embedding price; set embedding.price_per_million_tokens_usd"
    )
