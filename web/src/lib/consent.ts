/**
 * 远程同意对话框的纯逻辑：现在该显示什么、价格怎么说。
 *
 * 不碰 React、不碰 fetch。判定全部基于**结构化字段**——`semantic.consent`
 * （存在即"可以批准"）、`semantic.failure`（"不可批准"，原因在枚举里）、以及调用方
 * 是否已经带过批准。Ollama 等本地 provider 会返回两个空字段，表示无需批准即可使用；
 * 其余远程路径仍按同意状态判断。`reason` 是给人看的散文，两种成因读起来几乎一样，
 * 拿它分支迟早会错；B-5 已经为这件事立过规矩，这里只是照着做。
 *
 * 唯一一处不得不认字的地方写在 `NOT_APPROVED` 旁边。
 */

import type {
  ConsentApproval,
  RemoteConsent,
  SearchResponse,
  SemanticFailure,
} from './api-types'
import type { ApiError } from './errors'

/**
 * 服务端在"这次查询没有有效批准"时给出的原话。
 *
 * 认字符串是下策，这里没有更好的选择：远程服务端**总是**返回挑战（只要探针成功），
 * 所以"带了批准却仍被要求批准"这件事在响应里没有别的痕迹。`approved` 与
 * `rejected` 的差别对用户是有意义的——一个说"正在用语义检索"，另一个说"你批准
 * 的那次已经不适用了，因为查询或索引变了"——所以不能合并掉了事。
 *
 * 它的来源是 `application/search.py::SEMANTIC_NOT_APPROVED`，与 B-6 的
 * `classifyAskOutcome` 认引用诊断那句话是同一类妥协。改动那边就要改这里。
 */
const NOT_APPROVED = 'Semantic query was not approved'

/**
 * 这条 warning 是不是"还没批准"。
 *
 * 搜索页用它把那条提示从"检索已降级"里滤掉：它与同意提示条说的是同一件事，而提示条
 * 更完整——它带着两个出路（批准 / 换模式）。两条一起摆出来就是一次红色"检索已降级"
 * 加一次"你可以启用语义检索"，像两个部件在互相打架，而用户能做的事只有提示条上那件。
 * 其余 warning（索引缺向量、后端故障）照旧进告警，因为那些不是用户能"批准"掉的。
 *
 * 判断只写在这一处，`consentOutcome` 也用它，于是上面那个认字符串的妥协只有一份。
 */
export function isApprovalNotice(warning: string): boolean {
  return warning === NOT_APPROVED
}

/** 这一次查询在同意协议里处于什么状态。 */
export type ConsentOutcome =
  /** keyword 模式，或还没发请求：没有探针，也就无所谓同意。 */
  | { kind: 'not_needed' }
  /** 探针说语义腿跑不了，而且不是批准能解决的问题（索引缺向量 / 后端故障）。 */
  | { kind: 'unavailable'; failure: SemanticFailure }
  /** 可以批准，但还没批。这是该弹对话框的状态。 */
  | { kind: 'pending'; consent: RemoteConsent }
  /** 批准过，而且服务端接受了。 */
  | { kind: 'approved'; consent: RemoteConsent }
  /** 批准过，但服务端没有接受——查询或嵌入代次变了，或者批准本身过期了。 */
  | { kind: 'rejected'; consent: RemoteConsent }

export function consentOutcome(
  response: SearchResponse | undefined,
  approval: ConsentApproval | null,
): ConsentOutcome {
  const semantic = response?.semantic
  if (!semantic) return { kind: 'not_needed' }
  if (semantic.consent === null) {
    // 本地 Ollama 有意返回两个空字段，表示可以直接使用；远程 provider 在这里
    // 才会携带 failure。两种情况都不应显示一个点不动的批准按钮。
    return semantic.failure === null
      ? { kind: 'not_needed' }
      : { kind: 'unavailable', failure: semantic.failure }
  }
  if (approval === null) return { kind: 'pending', consent: semantic.consent }
  const honoured = !response!.warnings.some(isApprovalNotice)
  return honoured
    ? { kind: 'approved', consent: semantic.consent }
    : { kind: 'rejected', consent: semantic.consent }
}

/**
 * 从 409 的 `details` 里取出挑战。
 *
 * `mode=semantic` 的第一次请求走的就是这条路：它不能降级，所以服务端直接拒绝，
 * 并把挑战塞进错误信封。没有这个函数，选"语义"模式的用户会看到一个"需要批准"的
 * 提示却没有任何东西可批准。
 *
 * `details` 来自网络，所以逐字段校验而不是 `as` 断言——一个形状不对的对象会让
 * 对话框渲染出一串 `undefined`。
 */
export function consentFromError(error: ApiError): RemoteConsent | null {
  if (error.code !== 'consent_required') return null
  return readConsent(error.details)
}

function readConsent(details: unknown): RemoteConsent | null {
  if (typeof details !== 'object' || details === null) return null
  const candidate = (details as { consent?: unknown }).consent
  if (typeof candidate !== 'object' || candidate === null) return null
  const consent = candidate as Partial<RemoteConsent>
  if (
    typeof consent.consent_id !== 'string' ||
    typeof consent.query_hash !== 'string' ||
    typeof consent.generation_id !== 'string' ||
    typeof consent.expires_at !== 'string' ||
    typeof consent.query_tokens !== 'number' ||
    typeof consent.estimated_cost_usd !== 'string'
  ) {
    return null
  }
  return candidate as RemoteConsent
}

/** token 数。 */
export function formatTokens(tokens: number): string {
  return `${tokens} 个 token`
}

/**
 * 成本的美元写法。
 *
 * `estimated_cost_usd` 是 Pydantic 把 `Decimal` 序列化后的字符串，实测长这样：
 * `"1E-7"`。直接显示会让用户读到科学计数法，而"这次查询花 1E-7 美元"什么也说明
 * 不了，所以先 `Number()`。
 *
 * **不用 `Intl.NumberFormat`**：它会把任何小于半分钱的东西显示成 `$0.00`，把
 * "很便宜"变成"免费"。这个对话框存在的全部意义就是让用户看见价格，显示成免费比
 * 显示成科学计数法更糟。
 */
export function formatCost(costUsd: string): string {
  const value = Number(costUsd)
  if (!Number.isFinite(value)) return costUsd
  if (value === 0) return '$0'
  if (value < 0.000001) return '不足 $0.000001'
  return `$${value.toFixed(6)}`
}

/**
 * 挑战还剩几分钟失效，`null` 表示时间戳读不出来。
 *
 * 显示剩余时间而不是绝对时刻：用户需要判断的是"我现在要不要决定"，而不是
 * "15:42 是什么时候"。
 */
export function minutesUntil(expiresAt: string, now: Date): number | null {
  const at = new Date(expiresAt)
  if (Number.isNaN(at.getTime())) return null
  return Math.round((at.getTime() - now.getTime()) / 60_000)
}

/** 对话框里逐行显示的字段。 */
export type ConsentLine = { label: string; value: string }

/**
 * 对话框要展示的内容。
 *
 * **第一行是查询原文**，这一条不是装饰：对话框的职责是让用户看清"什么东西将要离开
 * 本机"，而那件东西就是查询文本。只显示 token 数和价格，用户批准的是一个自己看不
 * 见的字符串。
 */
export function describeConsent(consent: RemoteConsent, query: string): readonly ConsentLine[] {
  return [
    { label: '将发送的查询', value: query },
    { label: '预估 tokens', value: formatTokens(consent.query_tokens) },
    { label: '预估成本', value: formatCost(consent.estimated_cost_usd) },
    { label: '请求数', value: String(consent.request_count) },
  ]
}

const FAILURE_NOTICES: Record<SemanticFailure, string> = {
  index_missing:
    '索引里还没有向量，这次只用了关键词检索。运行 obsai index embeddings 后即可启用语义检索。',
  backend_unavailable: '语义检索后端不可用，这次只用了关键词检索。',
}

/**
 * "为什么没有语义结果"的中文说明。
 *
 * 与 `errors.ts` 里同名的两个错误码文案是**两套**：那套用在请求失败时（错误面板），
 * 这套用在请求成功但降级时（结果区上方的提示）。同一个成因，用户看到的位置不同。
 */
export function failureNotice(failure: SemanticFailure): string {
  return FAILURE_NOTICES[failure]
}
