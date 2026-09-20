/**
 * 服务端错误 → 可以直接显示给用户的中文。
 *
 * 后端把所有失败都归一成一个信封（`{"error": {code, type, message}}`），其中
 * `message` 是英文的领域原文，例如
 * `"Semantic index missing; run 'obsai index embeddings'"`。那句话对开发者有用，
 * 对用户没用：它没说"我该做什么"。所以这里做两件事：
 *
 * 1. 按 `code` 给出**中文标题**与**下一步动作**；
 * 2. 永远保留服务端原文作为 `detail`，因为翻译会丢信息，而排查问题时原文才是证据。
 *
 * 目录的键必须与 `src/obsai/api/errors.py::error_code()` 的输出一致——它由类名
 * 推导（`RecoveryRequiredError` → `recovery_required`），加上几处适配层显式指定的
 * 码（`invalid_host` / `cross_origin` / `invalid_request` / `http_error` / `internal`），
 * 以及三个只可能由前端产生的码（`offline` / `unparsable_response` / `cancelled`）。
 * 漏掉一个码不会报错，只会退化成 `FALLBACK`——所以新增领域异常时记得回来补。
 */

import type { ApiErrorEnvelope } from './api-types'

/** 与具体这次请求无关的那部分文案。 */
type Wording = {
  /** 一句话说明发生了什么。 */
  title: string
  /** 下一步该做什么；没有明确动作时留空，而不是写"请重试"。 */
  hint?: string
  /** 是否值得自动重试。 */
  retryable: boolean
}

const CATALOGUE: Record<string, Wording> = {
  // --- 配置与环境 ---------------------------------------------------------
  config: {
    title: '配置不可用',
    hint: '检查 ~/.config/obsai/config.toml 里的 vault.path 与 index.database。',
    retryable: false,
  },
  offline: {
    title: '无法连接到本地服务',
    hint: '后端可能没有启动。运行 ./scripts/dev.sh 同时启动前后端。',
    retryable: true,
  },
  unparsable_response: {
    title: '服务端返回了无法解析的响应',
    hint: '检查 127.0.0.1:8000 上跑的确实是 ObsAgent，而不是别的服务。',
    retryable: false,
  },
  invalid_host: {
    title: '请求来源不是本机地址，已被拒绝',
    hint: '请通过 http://127.0.0.1:8000 访问，不要使用其它域名或 IP。',
    retryable: false,
  },
  cross_origin: {
    title: '跨站请求已被拒绝',
    hint: '本机 API 只接受来自 127.0.0.1 页面的请求。',
    retryable: false,
  },
  cancelled: {
    title: '请求已取消',
    retryable: false,
  },

  // --- 索引与嵌入 ---------------------------------------------------------
  schema: {
    title: '索引文件损坏或版本不兼容',
    hint: '执行 obsai index rebuild 重建索引；索引是可重建的派生物，不会影响笔记。',
    retryable: false,
  },
  embedding_budget: {
    title: '本次嵌入会超出成本上限',
    hint: '调高 config.toml 里的 embedding.estimated_cost_limit_usd，或缩小索引范围。',
    retryable: false,
  },
  embedding: {
    title: '嵌入调用失败',
    hint: '检查 embedding 的凭据、模型名与网络。',
    retryable: true,
  },
  embedding_rate_limit: {
    title: '嵌入服务正在限流',
    hint: '稍后重试，或降低 config.toml 里的 embedding.max_concurrency。',
    retryable: true,
  },
  embedding_service: {
    title: '嵌入服务临时故障',
    hint: '稍后重试。',
    retryable: true,
  },

  // --- 写入与事务 ---------------------------------------------------------
  recovery_required: {
    title: '有未完成的写入事务，写入已被冻结',
    hint: '先执行 obsai transaction status 查看，再用 obsai transaction recover <ID> 恢复。',
    retryable: false,
  },
  lock_busy: {
    title: '另一个 ObsAgent 进程正在写入此 Vault',
    hint: '等它结束后重试。如果命令行正在跑 index update，等命令返回即可。',
    retryable: true,
  },
  conflict: {
    title: '笔记在预览之后被改动过',
    hint: '重新预览一次变更，确认后再批准。',
    retryable: false,
  },
  plan_drift: {
    title: '计划已发生变动',
    hint: '在确认期间索引数据或预估指标已变更，请核对更新后的预估并重新确认。',
    retryable: true,
  },
  plan_not_found: {
    title: '写入计划不存在或已失效',
    hint: '此变更计划未找到，可能已被执行或服务器已重启，请重新生成计划。',
    retryable: false,
  },
  plan_expired: {
    title: '写入计划已过期',
    hint: '变更计划已超过有效期，请重新生成写入计划后再确认。',
    retryable: false,
  },
  collision: {
    title: '目标位置已存在同名笔记',
    hint: '换一个路径或名称再试。',
    retryable: false,
  },
  transaction: {
    title: 'Vault 事务未能完成',
    hint: '执行 obsai transaction status 查看未完成的日志并恢复。',
    retryable: false,
  },
  safe_write: {
    title: '这次写入被拒绝',
    hint: '重新预览变更后再批准。',
    retryable: false,
  },
  invalid_encoding: {
    title: '笔记不是合法的 UTF-8 文本',
    hint: '用编辑器把该文件另存为 UTF-8 后重试。',
    retryable: false,
  },
  lock_unsafe: {
    title: '锁文件不可信',
    hint: '检查索引目录下是否存在异常的 index.db.lock 符号链接。',
    retryable: false,
  },

  // --- 检索与问答 ---------------------------------------------------------
  vault: {
    title: 'Vault 无法读取',
    hint: '确认 vault.path 指向一个存在的目录，且当前用户有读取权限。',
    retryable: false,
  },
  parse: {
    title: '有笔记无法安全解析',
    hint: '检查该笔记是否为合法 UTF-8 的 Markdown。',
    retryable: false,
  },
  context: {
    title: '证据放不进上下文预算',
    hint: '调高 config.toml 里的 ask.max_evidence_tokens，或把问题问得更具体。',
    retryable: false,
  },
  llm: {
    title: '答案生成失败',
    hint: '检查 ask.provider 的凭据与网络；也可以先用关键词检索。',
    retryable: true,
  },
  missing_credential: {
    title: '缺少 API Key，无法生成回答',
    // 与 `config` 的关键差别：配置文件本身没问题，缺的是环境变量。指向 config.toml
    // 会把用户引到一处已经正确的地方。
    hint: '设置环境变量 OPENAI_API_KEY 后重启后端。config.toml 无需改动。',
    retryable: false,
  },
  job_not_found: {
    title: '这个任务不存在',
    // 与 `not_found` 共用 404，所以文案只能靠 code 区分：那个说的是笔记，这个说的是
    // 任务。服务端的 `JobNotFoundError` 是 `NotFoundError` 的子类，状态码继承下来，
    // 码另起一个——MRO 那张表让"改文案"不必"改状态码"。
    hint: '任务记录与索引放在同一个目录，所以换了 index.database 之后就看不到旧任务了。刷新页面查看当前索引的任务列表。',
    retryable: false,
  },
  not_found: {
    title: '这篇笔记不在索引里',
    // 与 `http_error`（路径写错了）的区别：请求是对的，东西没了。最常见的原因是
    // 笔记被删掉或改过名之后重建了索引，而用户手上还是一个旧链接。
    hint: '它可能已被删除或改名。回到搜索页重新找一次，或运行 obsai index update 补齐索引。',
    retryable: false,
  },
  consent_required: {
    title: '这次查询还没有被批准',
    // 语义检索会把查询文本发往远程嵌入服务，所以每一次查询都要单独批准。这不是
    // 配置问题（配置文件是对的），也不是后端故障——用户只是还没做那个决定。
    hint: '语义检索需要把查询文本发往远程服务。批准后重新搜索，或改用关键词模式。',
    retryable: false,
  },
  consent_expired: {
    title: '这次批准已经过期',
    // 挑战本身有 15 分钟有效期，对话框开着超过这个时间就会到这里。重试按钮没有
    // 意义——同一个批准再发一次还是过期，必须重新搜索拿一个新的挑战。
    hint: '批准请求有有效期。重新搜索一次会拿到新的批准请求。',
    retryable: false,
  },
  semantic_index_missing: {
    title: '索引里还没有向量',
    // 与 `config` 的区别：配置文件是对的，缺的是派生物。下一步是一条命令，不是
    // 一次编辑。
    hint: '运行 obsai index embeddings 生成向量，然后重新搜索。',
    retryable: false,
  },
  semantic_unavailable: {
    title: '语义检索后端不可用',
    // 与 `semantic_index_missing` 的区别：向量在，是嵌入服务没响应。这是暂时的，
    // 所以可以重试。
    hint: '嵌入服务没有响应。检查网络与嵌入 provider 配置后重试。',
    retryable: true,
  },

  // --- 适配层 -------------------------------------------------------------
  invalid_request: {
    title: '请求参数不合法',
    hint: '这通常意味着界面与后端的契约不一致，请附上 request id 反馈。',
    retryable: false,
  },
  http_error: {
    title: '请求失败',
    retryable: false,
  },
  internal: {
    title: '服务端出现未预期的错误',
    hint: '查看后端日志；若可复现，请附上 request id。',
    retryable: true,
  },
  obs_ai: {
    title: 'ObsAgent 操作失败',
    retryable: false,
  },
}

/** 目录里没有的码退到这里，并附上服务端原文，绝不静默吞掉信息。 */
const FALLBACK: Wording = {
  title: '操作失败',
  retryable: false,
}

/** 可展示的错误描述。`title` 与 `hint` 是中文；`detail` 是服务端原文。 */
export type ErrorPresentation = {
  title: string
  hint?: string
  /** 服务端（或浏览器）的原始描述，供排查用。 */
  detail?: string
  retryable: boolean
  /** 与服务端日志对齐用；出错时把它显示出来能让用户一句"附上 request id"就够。 */
  requestId?: string
  status?: number
}

/**
 * 按码取文案，`detail` 由调用方给（通常是服务端原文）。
 *
 * 单独导出是因为**任务失败不走错误信封**。`JobView.error_code` 存的是异常类名，
 * 记录落在 `<index>.jobs.db` 里，失败发生在过去某个进程里，已经没有响应可解析了。
 * 调用方拿类名换到码之后，需要的正是这张表。
 */
export function wordingFor(code: string, detail?: string): ErrorPresentation {
  const wording = CATALOGUE[code] ?? FALLBACK
  return { title: wording.title, hint: wording.hint, detail, retryable: wording.retryable }
}

/**
 * 领域异常类名 → 目录的键。
 *
 * 与 `src/obsai/api/errors.py::error_code()` 是同一个算法，在这里重写了一遍，因为
 * 任务记录存的是**类名**（`type(exc).__name__`）而目录的键是那个函数的输出。抄错
 * 不会报错，只会让某类失败退化成「操作失败」——所以 `tests/unit/test_web_contract.py`
 * 拿服务端的真实输出对每一个 `ObsAIError` 子类逐个比对，服务端改了规则这里就会红。
 */
export function codeFromType(type: string): string {
  return type
    .replace(/Error$/, '')
    .replace(/(?<=[a-z0-9])(?=[A-Z])/g, '_')
    .replace(/(?<=[A-Z])(?=[A-Z][a-z])/g, '_')
    .toLowerCase()
}

/**
 * 服务端返回的错误。
 *
 * 继承 `Error` 是为了让 TanStack Query 的默认日志、`instanceof` 判断和
 * 堆栈都正常工作；额外的字段都是信封里的内容。
 */
export class ApiError extends Error {
  readonly code: string
  readonly type: string
  readonly status: number
  readonly requestId: string | null
  readonly details: unknown

  constructor(init: {
    code: string
    type: string
    message: string
    status: number
    requestId?: string | null
    details?: unknown
  }) {
    super(init.message)
    this.name = 'ApiError'
    this.code = init.code
    this.type = init.type
    this.status = init.status
    this.requestId = init.requestId ?? null
    this.details = init.details
  }

  get presentation(): ErrorPresentation {
    return present(this)
  }
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

/** 把任意抛出物翻译成可展示的中文。永远不抛。 */
export function present(error: unknown): ErrorPresentation {
  if (isAbort(error)) {
    return wordingFor('cancelled')
  }

  if (error instanceof ApiError) {
    return {
      ...wordingFor(error.code, error.message),
      requestId: error.requestId ?? undefined,
      status: error.status,
    }
  }

  if (error instanceof TypeError) {
    // fetch 在网络层失败时抛 TypeError（"Failed to fetch" / "fetch failed"）。
    // 这是本地工具最常见的失败：后端没启动。
    return wordingFor('offline', error.message)
  }

  return { title: FALLBACK.title, detail: String(error), retryable: false }
}

/** 判断一个未知响应体是不是错误信封——不能只看状态码，也要看形状。 */
export function isErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as { error?: unknown }
  if (typeof candidate.error !== 'object' || candidate.error === null) return false
  const inner = candidate.error as Record<string, unknown>
  return (
    typeof inner.code === 'string' &&
    typeof inner.type === 'string' &&
    typeof inner.message === 'string'
  )
}

/** 目录的键集合，供测试断言"服务端新增的码都在这里"用。 */
export const KNOWN_ERROR_CODES: readonly string[] = Object.keys(CATALOGUE)
