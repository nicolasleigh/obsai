/**
 * 本地 API 的 fetch 封装。
 *
 * 只有一处发请求，理由和 CLI 侧一样：信封解析、request id 注入、非 JSON 响应的
 * 兜底如果散在每个调用点，迟早会有一处漏掉，而漏掉的表现是"某个页面偶尔白屏"。
 *
 * 三件事在这里统一做掉：
 *
 * - **注入 `X-Request-ID`。** 服务端会把它回显在响应头与错误体里，也会写进日志；
 *   用户截图报错时带上它就能直接定位。服务端只接受 `[A-Za-z0-9._-]{1,64}`，
 *   `crypto.randomUUID()` 的 36 字符连字符形式正好合法（127.0.0.1 属于安全上下文，
 *   该 API 一定可用）。
 * - **解析统一错误信封。** 4xx/5xx 一律变成 `ApiError`，调用方只需要处理一种失败。
 * - **非 JSON 响应不静默通过。** 开发代理打错端口、或 8000 上跑着别的服务时，
 *   拿到的是 HTML；把它当 JSON 解析会抛出一个毫无线索的 `SyntaxError`。
 *
 * 成功路径刻意不做 `zod` 校验：契约由 `api-types.ts` 的静态类型表达，运行时再校验
 * 一遍只会让"后端加了字段"变成前端报错。真正需要运行时校验的是**错误响应**——
 * 因为那时我们已经不能假设对面是本项目了。
 *
 * **任务进度不在这里。** 那条通道是 `lib/sse.ts` 的 `EventSource`，它不能发 POST，
 * 也因此**不能取消任何任务**——取消只有 `cancelJob` 一个入口，由人点。
 */

import type {
  AgentResumeRequest,
  AgentRunRequest,
  AgentRunView,
  ApiErrorEnvelope,
  ApproveChangePlanRequest,
  AskRequest,
  AskResponse,
  ChangeOutcome,
  ChangePlanView,
  ConsentApproval,
  CreateChangePlanRequest,
  EmbeddingPlanView,
  HealthResponse,
  JobView,
  JournalView,
  NoteView,
  OrganizePlanRequest,
  OrganizePreview,
  RecoverTransactionRequest,
  RecoveryView,
  RemoteConsent,
  SearchRequest,
  SearchResponse,
  StatusResponse,
} from './api-types'
import { ApiError, isErrorEnvelope } from './errors'

/**
 * 开发期由 Vite 代理转发，发布时同源挂载（F-2），两种情况都用同一个前缀。
 *
 * 导出是因为 `lib/sse.ts` 也要拼同一个前缀。事件流走的是另一条通道，但前缀必须一致，
 * 而两处各写一遍迟早会在某次改动后分叉——症状是页面其它请求都正常、只有进度不动。
 */
export const API_PREFIX = '/api/v1'

const REQUEST_ID_HEADER = 'X-Request-ID'

export type RequestOptions = {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** 会被 JSON 序列化；`undefined` 表示不带请求体。 */
  body?: unknown
  signal?: AbortSignal
  /** 复用同一个 id（例如重试），便于把多次尝试关联到一条后端日志。 */
  requestId?: string
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const requestId = options.requestId ?? crypto.randomUUID()
  const headers: Record<string, string> = {
    Accept: 'application/json',
    [REQUEST_ID_HEADER]: requestId,
  }
  let body: string | undefined
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  const response = await fetch(`${API_PREFIX}${path}`, {
    method: options.method ?? 'GET',
    headers,
    body,
    signal: options.signal,
  })

  if (!response.ok) throw await toApiError(response, requestId)
  // 204 没有响应体；其余端点都应返回 JSON。
  if (response.status === 204) return undefined as T
  return (await toJson(response, requestId)) as T
}

/** 只读端点。写入端点随 D/E 阶段加入，同样走 `request()`。 */
export const api = {
  health: (signal?: AbortSignal) => request<HealthResponse>('/health', { signal }),
  status: (signal?: AbortSignal) => request<StatusResponse>('/status', { signal }),
  search: (body: SearchRequest, signal?: AbortSignal) =>
    request<SearchResponse>('/search', { method: 'POST', body, signal }),
  ask: (body: AskRequest, signal?: AbortSignal) =>
    request<AskResponse>('/ask', { method: 'POST', body, signal }),
  /**
   * 一篇笔记。
   *
   * `encodeURIComponent` 在这里是形式大于实质——note ID 是十六进制串，不会有需要
   * 转义的字符。留着是因为它是**路径段**而不是查询参数：将来 ID 换了形状（或有人
   * 手改地址栏），一个没转义的 `/` 会把请求打到另一个路由上，而那时错误会出现在
   * 服务端而不是这里。
   */
  note: (noteId: string, signal?: AbortSignal) =>
    request<NoteView>(`/notes/${encodeURIComponent(noteId)}`, { signal }),
  /**
   * 回答一次远程查询的同意挑战，拿到可以回传给 `/search` 的批准。
   *
   * 只有"批准"走这里。**"拒绝"刻意不发请求**：页面上本来就有第一次请求留下的
   * 降级结果，再问一次服务端只会换来同一批结果和一个多余的往返，还多一次把"拒绝"
   * 变成"批准"的机会。
   */
  consent: (consent: RemoteConsent, approved: boolean, signal?: AbortSignal) =>
    request<ConsentApproval>('/consent', {
      method: 'POST',
      body: { ...consent, approved },
      signal,
    }),
  /**
   * 最近的任务，以及它们此刻的状态。
   *
   * 读取**不会**创建任务日志：索引旁边没有日志时，答案是"没有任务"，而不是"刚建了
   * 一个空日志"。这条由服务端的 `GET /jobs` 保证（见 `application/jobs.py`）。
   */
  jobs: (signal?: AbortSignal) => request<JobView[]>('/jobs', { signal }),
  /**
   * 一个任务。刷新页面后凭 job ID 重新拉取走的就是这里。
   *
   * 状态活在进程之外（`<index>.jobs.db`），所以一个 id 在重启之后仍然解析得出来——
   * 这正是"刷新不等于丢掉进度"的依据。
   */
  job: (jobId: string, signal?: AbortSignal) =>
    request<JobView>(`/jobs/${encodeURIComponent(jobId)}`, { signal }),
  /**
   * 请求取消。**这是唯一的取消入口。**
   *
   * 断开事件流、关页面、刷新、切到后台被浏览器掐掉——都不会取消任何任务。取消是协作式
   * 的（任务在下一个安全点停下），所以返回的是请求**之后**的状态而不是布尔值：
   * "已接受"和"已经结束"在这里是同一个回答，而状态自己会说是哪一个。
   */
  cancelJob: (jobId: string, signal?: AbortSignal) =>
    request<JobView>(`/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: 'POST',
      signal,
    }),
  /**
   * 开始一次增量索引更新。
   *
   * 服务端回 202 加一条**已经排上队**的任务，而不是 200 加一个结果：工作是受理了，
   * 不是做完了。返回的 `JobView` 可以立刻显示，后续变化从事件流来。
   *
   * 索引不存在也能调——第一次建索引和增量同步是同一个操作。
   */
  indexUpdate: (signal?: AbortSignal) =>
    request<JobView>('/jobs/index-update', { method: 'POST', signal }),
  /**
   * 开始一次完整重建。
   *
   * 重建写出的是一个**全新的库**：向量表是空的，所以完成后要提示用户运行
   * `obsai index embeddings`。那条提示由 `detail.embeddings_stale` 决定，
   * 不由前端猜——见 `lib/jobs.ts::embeddingsAreStale`。
   */
  indexRebuild: (signal?: AbortSignal) =>
    request<JobView>('/jobs/index-rebuild', { method: 'POST', signal }),
  /**
   * 预估并准备一次向量生成计划。
   *
   * 返回计划 ID、模型代、待生成分块数、预估 Token 数与成本。若预算超限，服务端抛 429。
   */
  embeddingPlan: (signal?: AbortSignal) =>
    request<EmbeddingPlanView>('/embedding/plans', { method: 'POST', signal }),
  /**
   * 批准执行向量生成计划。
   *
   * 服务端会重新比对分块与预算；若在审阅期间发生漂移，抛出 409（plan_drift），
   * 携带更新后的新计划要求二次确认。成功则返回 202 Accepted 任务。
   */
  approveEmbeddingPlan: (planId: string, nonce: string, signal?: AbortSignal) =>
    request<JobView>(`/embedding/plans/${encodeURIComponent(planId)}/approve`, {
      method: 'POST',
      body: { nonce },
      signal,
    }),
  /**
   * 准备一次 Vault 多文件写入事务变更计划。
   */
  createChangePlan: (body: CreateChangePlanRequest, signal?: AbortSignal) =>
    request<ChangePlanView>('/changes/plans', { method: 'POST', body, signal }),
  /**
   * 获取一个处于活跃生命周期的变更计划。
   */
  getChangePlan: (planId: string, signal?: AbortSignal) =>
    request<ChangePlanView>(`/changes/plans/${encodeURIComponent(planId)}`, { signal }),
  /**
   * 确认或放弃执行该变更计划。凭 revision 与 nonce 凭证防重放与篡改。
   */
  approveChangePlan: (
    planId: string,
    body: ApproveChangePlanRequest,
    signal?: AbortSignal
  ) =>
    request<ChangeOutcome>(`/changes/plans/${encodeURIComponent(planId)}/approve`, {
      method: 'POST',
      body,
      signal,
    }),
  /**
   * 生成 Inbox 整理建议与分类提案列表（只读，不修改 Vault）。
   */
  organizeProposals: (signal?: AbortSignal) =>
    request<OrganizePreview>('/organize/proposals', { method: 'POST', signal }),
  /**
   * 将选中的整理提案序号转换为变更计划（ChangePlanView）。
   */
  organizePlan: (body: OrganizePlanRequest, signal?: AbortSignal) =>
    request<ChangePlanView>('/organize/plan', { method: 'POST', body, signal }),
  /**
   * 获取 Vault 中所有的事务日志及其状态。
   */
  getTransactions: (signal?: AbortSignal) =>
    request<JournalView[]>('/transactions', { signal }),
  /**
   * 获取指定事务的恢复快照与详细回滚 Unified Diff。
   */
  getTransaction: (transactionId: string, signal?: AbortSignal) =>
    request<RecoveryView>(`/transactions/${encodeURIComponent(transactionId)}`, { signal }),
  /**
   * 恢复未完成事务，将文件回滚至快照。
   */
  recoverTransaction: (
    transactionId: string,
    body: RecoverTransactionRequest = { approved: true },
    signal?: AbortSignal,
  ) =>
    request<ChangeOutcome>(`/transactions/${encodeURIComponent(transactionId)}/recover`, {
      method: 'POST',
      body,
      signal,
    }),
  /**
   * 启动智能体工作流（支持传入初始 prompt 与可选的 thread_id）。
   */
  startAgentRun: (body: AgentRunRequest, signal?: AbortSignal) =>
    request<AgentRunView>('/agent/runs', { method: 'POST', body, signal }),
  /**
   * 根据 run_id 检索智能体执行状态与工具时间线。
   */
  getAgentRun: (runId: string, signal?: AbortSignal) =>
    request<AgentRunView>(`/agent/runs/${encodeURIComponent(runId)}`, { signal }),
  /**
   * 人工审批恢复已中断的写入节点。
   */
  resumeAgentRun: (
    runId: string,
    body: AgentResumeRequest = { approved: true },
    signal?: AbortSignal,
  ) =>
    request<AgentRunView>(`/agent/runs/${encodeURIComponent(runId)}/resume`, {
      method: 'POST',
      body,
      signal,
    }),
}

function responseRequestId(response: Response, fallback: string): string {
  return response.headers.get(REQUEST_ID_HEADER) ?? fallback
}

async function toApiError(response: Response, fallbackId: string): Promise<ApiError> {
  const envelope = await readEnvelope(response)
  const requestId = responseRequestId(response, envelope?.request_id ?? fallbackId)
  if (envelope === null) {
    return new ApiError({
      code: 'unparsable_response',
      type: 'HttpError',
      message: `HTTP ${response.status} ${response.statusText}`.trim(),
      status: response.status,
      requestId,
    })
  }
  return new ApiError({
    code: envelope.error.code,
    type: envelope.error.type,
    message: envelope.error.message,
    status: response.status,
    requestId,
    details: envelope.error.details,
  })
}

/**
 * 读错误信封，读不到就返回 `null`。
 *
 * 先看 `Content-Type` 再解析：Vite 代理把请求转给了别的服务时，响应可能是
 * `text/html`，直接 `response.json()` 会抛 `SyntaxError`，而那个异常没有任何
 * 指向"端口被占用"的线索。
 */
async function readEnvelope(response: Response): Promise<ApiErrorEnvelope | null> {
  const contentType = response.headers.get('Content-Type') ?? ''
  if (!contentType.includes('application/json')) return null
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    return null
  }
  return isErrorEnvelope(payload) ? payload : null
}

async function toJson(response: Response, fallbackId: string): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    throw new ApiError({
      code: 'unparsable_response',
      type: 'HttpError',
      message: 'The response body is not valid JSON',
      status: response.status,
      requestId: responseRequestId(response, fallbackId),
    })
  }
}
