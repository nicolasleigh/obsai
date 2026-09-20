/**
 * 服务端 DTO 的 TypeScript 镜像。
 *
 * 唯一真相源是 `src/obsai/application/dto.py` 与 `src/obsai/api/routes/status.py`；
 * 本文件只是它的读侧投影。后端改字段时这里必须同步改。
 *
 * **字段名保持 snake_case，不做 camelCase 转换。** 任何转换层都是一处会静默漂移的
 * 地方：后端改名后 TS 侧仍能编译，只在运行时表现为 `undefined`。保持原样意味着
 * 一个拼错的字段名会在 `tsc` 阶段就被发现——因为类型上根本没有那个键。
 */

/** 与 `dto.py` 的 `DiffStyle` 一致。 */
export type DiffStyle = 'added' | 'removed' | 'hunk' | 'notice'

/** 与 `dto.py` 的 `JournalOriginal` 一致。 */
export type JournalOriginal = {
  path: string
  snapshot: string | null
}

/** 与 `dto.py` 的 `JournalView` 一致。 */
export type JournalView = {
  transaction_id: string
  status: string
  originals: JournalOriginal[]
}

/** 与 `dto.py` 的 `DirtyNoteView` 一致。 */
export type DirtyNoteView = {
  path: string
  reason: string
  marked_at: string
}

/** 与 `dto.py` 的 `IndexStatusView` 一致。 */
export type IndexStatusView = {
  path: string
  /** 索引路径上有没有东西——**不等于**可用。 */
  exists: boolean
  usable: boolean
  /** 索引存在但打不开时的原因；否则为 null。 */
  error: string | null
  note_count: number
  chunk_count: number
  vector_count: number
  generation: string | null
  semantic_ready: boolean
  dirty_notes: DirtyNoteView[]
}

/** 与 `dto.py` 的 `StatusView` 一致。 */
export type StatusView = {
  version: string
  vault_path: string | null
  vault_ready: boolean
  index: IndexStatusView
  /** `prepared` / `applying` / `rolling_back` / `recovery_required`。 */
  unfinished_transactions: JournalView[]
  /** `committed` / `index_dirty`——Vault 已改完，只差索引。 */
  index_dirty_transactions: JournalView[]
  recovery_required: boolean
  locked: boolean
}

/** 与 `api/routes/status.py` 的 `StatusResponse` 一致：状态 + 进程事实。 */
export type StatusResponse = StatusView & {
  started_at: string
  uptime_seconds: number
}

/** 与 `api/routes/status.py` 的 `health()` 一致。 */
export type HealthResponse = {
  status: string
}

// --------------------------------------------------------------------------- //
// Search
// --------------------------------------------------------------------------- //

/** 与 `dto.py` 的 `SearchMode` 一致。 */
export type SearchMode = 'hybrid' | 'keyword' | 'semantic' | 'graph'

/** 与 `dto.py` 的 `SearchFilters` 一致。 */
export type SearchFilters = {
  tags?: string[]
  folder?: string | null
  modified_after?: string | null
  modified_before?: string | null
  frontmatter?: Record<string, JsonScalar>
  dataview?: Record<string, string>
}

/** ``JsonScalar`` 来自 ``retrieval/models.py``，是 frontmatter 值的类型。 */
export type JsonScalar = string | number | boolean | null

/** 与 `dto.py` 的 `SearchResult` 一致。 */
export type SearchResult = {
  chunk_id: string
  note_id: string
  path: string
  title: string
  heading_path: string[]
  snippet: string
  score: number
  source: string
  sources: string[]
}

/** 与 `dto.py` 的 `SearchOutcome` 一致。 */
export type SearchOutcome = {
  results: SearchResult[]
  warnings: string[]
}

/** 与 `dto.py` 的 `RemoteConsent` 一致。
 *
 * ``estimated_cost_usd`` 是 ``Decimal`` 序列化后的字符串（如 ``"1E-7"``），
 * 前端需要用 ``parseFloat`` 或 ``Intl.NumberFormat`` 格式化。
 */
export type RemoteConsent = {
  consent_id: string
  query_hash: string
  generation_id: string
  expires_at: string
  query_tokens: number
  /** 精确金额，但 JSON 里是字符串形式，避免浮点精度漂移。 */
  estimated_cost_usd: string
  request_count: number
}

/** 与 `dto.py` 的 `SemanticFailure` 一致。 */
export type SemanticFailure = 'index_missing' | 'backend_unavailable'

/** 与 `dto.py` 的 `SemanticProbe` 一致。
 *
 * 远程 provider 成功探测时 `consent` 非空；本地 Ollama provider 则会返回
 * `consent=null`、`reason=""`、`failure=null`，表示不需要同意即可使用语义检索。
 * 调用方按 `consent` 判断"要不要弹同意对话框"，按 `failure` 判断"该给用户哪条
 * 下一步"。`reason` 是给人看的散文——"索引没有向量"与"后端连不上"读起来几乎
 * 一样——所以**不能拿它分支**。
 */
export type SemanticProbe = {
  consent: RemoteConsent | null
  reason: string
  failure: SemanticFailure | null
}

/** 与 `dto.py` 的 `EmbeddingPlanView` 一致。 */
export type EmbeddingPlanView = {
  plan_id: string
  generation_id: string
  chunks_requiring_embeddings: number
  cache_hits: number
  estimated_tokens: number
  request_count: number
  /** 精确金额，但 JSON 里是字符串形式，避免浮点精度漂移。 */
  estimated_cost_usd: string
  expires_at: string
  nonce: string
}

/** 与 `dto.py` 的 `EmbeddingApproveRequest` 一致。 */
export type EmbeddingApproveRequest = {
  nonce: string
}

/** 与 `dto.py` 的 `ConsentApproval` 一致：对一个挑战的决定。
 *
 * `nonce` 不是凭据，是"这次批准发生在什么时候"的记录；服务端靠
 * `consent_id` / `query_hash` / `generation_id` 三重比对来决定它是否覆盖某次查询。
 * 保留它是为了让批准可追溯。
 */
export type ConsentApproval = {
  consent_id: string
  query_hash: string
  generation_id: string
  nonce: string
  approved_at: string
  approved: boolean
}

/** 与 `api/routes/search.py` 的 `SearchResponse` 一致。 */
export type SearchResponse = SearchOutcome & {
  semantic: SemanticProbe | null
}

/** 与 `dto.py` 的 `SearchRequest` 一致（服务端是它的子类 `SearchRequestBody`）。
 *
 * `approval` 只在对这次查询已经拿到批准时出现。第一次请求一律不带它——那正是
 * 服务端用来判断"用户还没被问过"的依据。
 */
export type SearchRequest = {
  query: string
  mode: SearchMode
  limit?: number
  filters?: SearchFilters
  strict_semantic?: boolean
  approval?: ConsentApproval
}

// --------------------------------------------------------------------------- //
// Ask
// --------------------------------------------------------------------------- //

/** 与 `dto.py` 的 `CitationView` 一致。
 *
 * ``location`` 由服务端拼好（``path > heading > heading ^block``），前端不再自己
 * 组合——两处拼法迟早会分叉，而分叉的表现是"复制出来的定位串在 Obsidian 里打不开"。
 */
export type CitationView = {
  citation_id: string
  note_id: string
  chunk_id: string
  path: string
  title: string
  heading_path: string[]
  block_id: string | null
  /** 已拼好的可读定位串，直接展示。 */
  location: string
  /** 证据被上下文预算截断过——引用仍在，但不是全文。 */
  truncated: boolean
}

/** 与 `dto.py` 的 `AskOutcome` 一致。 */
export type AskOutcome = {
  text: string
  citations: CitationView[]
  warnings: string[]
  /** 弃答：``text`` 是原因说明，``citations`` 必为空。见 `lib/ask.ts`。 */
  abstained: boolean
}

/** 与 `api/routes/ask.py` 的 `AskResponse` 一致。 */
export type AskResponse = AskOutcome & {
  semantic: SemanticProbe | null
}

/** 与 `dto.py` 的 `AskRequest` 一致。
 *
 * 只有 ``query``：检索宽度、证据条数与输出上限都由服务端 `ask.*` 配置决定，不接受
 * 每个请求单独覆盖——那会让"同一份 Vault、同一个问题"在不同页面给出不同答案。
 */
export type AskRequest = {
  query: string
}

// --------------------------------------------------------------------------- //
// Notes
// --------------------------------------------------------------------------- //

/** 与 `vault/models.py` 的 `BlockKind` 一致。 */
export type BlockKind =
  | 'heading'
  | 'paragraph'
  | 'list_item'
  | 'blockquote'
  | 'callout'
  | 'code'

/**
 * 与 `dto.py` 的 `NoteSegment` 一致：块里的一段文本，或一个已经解析过的 WikiLink。
 *
 * `kind === 'link'` 且 `target_note_id === null` 表示**目标不在 Vault 内**，此时必须
 * 渲染成普通文本。这不是"没查到"，是服务端的结论——见 `lib/note.ts` 的 `linkHref`。
 * 服务端只会发这两个字段组合，`kind === 'text'` 时后面五个字段恒为默认值。
 */
export type NoteSegment = {
  kind: 'text' | 'link'
  text: string
  target_note_id: string | null
  target_path: string | null
  target_heading: string | null
  target_block_id: string | null
  is_embed: boolean
}

/**
 * 与 `dto.py` 的 `NoteBlockView` 一致。
 *
 * 没有 `content`：`segments` 就是块的全部内容。两个来源会分叉，所以只留一个。
 * `level` 只有标题块有值——`Block` 不带层级，`Heading` 带，服务端已经合好了。
 */
export type NoteBlockView = {
  kind: BlockKind
  level: number | null
  segments: NoteSegment[]
  block_id: string | null
  language: string | null
  line: number
}

/**
 * 与 `dto.py` 的 `NoteView` 一致。
 *
 * **没有 `raw_content`。** 笔记原文永远不过网线：B-7 的"不执行任何内容"靠的是
 * 前端拿不到可以变成标记的字符串，而不是靠某个清洗器配对。改这个类型之前先读
 * `src/obsai/application/notes.py` 的模块说明。
 */
export type NoteView = {
  note_id: string
  path: string
  title: string
  frontmatter: Record<string, unknown>
  tags: string[]
  blocks: NoteBlockView[]
  /** 指向 Vault 外、因此不可点击的链接目标；已去重。 */
  unresolved_links: string[]
}

// --------------------------------------------------------------------------- //
// Jobs
// --------------------------------------------------------------------------- //

/** 与 `dto.py` 的 `JobStatus` 一致。
 *
 * `interrupted` 是终态，但**不是** `cancelled`：没有人要求它停，它也没有机会回滚。
 * 进程崩溃后重启会把当时还在跑的任务标成它——"我们不知道它怎么结束的"不能被报成
 * "它成功了"，也不该被记到用户头上。
 */
export type JobStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

/** 与 `dto.py` 的 `JobView` 一致。
 *
 * `detail` 是**标量进度指标**（`{"notes": 12, "step": 3}`），不是笔记正文：服务端的
 * `_summary()` 只留标量与短字符串，所以"日志里不会出现笔记内容"是形状的性质，而不是
 * 一条要靠人记住的规矩。
 *
 * `error_code` 是异常类名，与 `error`（文案）分开——服务端存的是码，取出来也该是码，
 * 这样前端能用 `lib/errors.ts` 那套「码 → 中文 + 下一步」的映射，而不是去猜散文。
 */
export type JobView = {
  job_id: string
  kind: string
  status: JobStatus
  created_at: string
  started_at: string | null
  finished_at: string | null
  message: string | null
  detail: Record<string, unknown>
  error: string | null
  error_code: string | null
}

// --------------------------------------------------------------------------- //
// Changes & Transactions
// --------------------------------------------------------------------------- //

/** 与 `dto.py` 的 `DiffLine` 一致。 */
export type DiffLine = {
  text: string
  style: DiffStyle | null
  highlight: boolean
}

/** 与 `dto.py` 的 `PlannedChange` 一致。 */
export type PlannedChange = {
  operation: string
  path: string
  destination: string | null
  affected_backlinks: string[]
}

/** 与 `dto.py` 的 `DiffSummaryItem` 一致。 */
export type DiffSummaryItem = {
  path: string
  operation: string
  destination: string | null
  added_lines: number
  removed_lines: number
}

/** 与 `dto.py` 的 `ChangePlanView` 一致。 */
export type ChangePlanView = {
  plan_id: string
  revision: number
  expires_at: string
  nonce: string
  changes: PlannedChange[]
  affected_paths: string[]
  diff: DiffLine[]
  diff_summary: DiffSummaryItem[]
  ambiguous_backlinks: string[]
  batch: boolean
}

/** 与 `dto.py` 的 `ChangeOutcome` 一致。 */
export type ChangeOutcome = {
  plan_id: string
  committed: boolean
  cancelled: boolean
  index_dirty: boolean
  index_error: string | null
  transaction_id: string | null
}

/** 与 `dto.py` 的 `ChangeOperationKind` 一致。 */
export type ChangeOperationKind =
  | 'create'
  | 'replace'
  | 'append'
  | 'frontmatter'
  | 'move'
  | 'trash'
  | 'rewrite_backlinks'

/** 与 `dto.py` 的 `ChangeOperationRequest` 一致。 */
export type ChangeOperationRequest = {
  kind: ChangeOperationKind
  path: string
  destination?: string | null
  old?: string | null
  new?: string | null
  updates?: Record<string, unknown>
}

/** 与 `dto.py` 的 `CreateChangePlanRequest` 一致。 */
export type CreateChangePlanRequest = {
  operations: ChangeOperationRequest[]
}

/** 与 `dto.py` 的 `ApproveChangePlanRequest` 一致。 */
export type ApproveChangePlanRequest = {
  revision: number
  nonce: string
  approved?: boolean
}

// --------------------------------------------------------------------------- //
// Organize
// --------------------------------------------------------------------------- //

/** 与 `dto.py` 的 `OrganizeProposalView` 一致。 */
export type OrganizeProposalView = {
  number: number
  path: string
  destination: string | null
  title: string
  add_tags: string[]
  add_links: string[]
  affected_backlinks: string[]
  confidence: number
  reason: string
  issue: string | null
  actionable: boolean
  selected_by_default: boolean
}

/** 与 `dto.py` 的 `OrganizePreview` 一致。 */
export type OrganizePreview = {
  proposals: OrganizeProposalView[]
  inbox: string
  default_numbers: number[]
}

/** 与 `dto.py` 的 `OrganizePlanRequest` 一致。 */
export type OrganizePlanRequest = {
  numbers: number[]
}

// --------------------------------------------------------------------------- //
// Transactions and Recovery
// --------------------------------------------------------------------------- //

/** 与 `dto.py` 的 `RecoveryView` 一致。 */
export type RecoveryView = {
  transaction_id: string
  status: string
  journal_directory: string
  originals: JournalOriginal[]
  diff: DiffLine[]
  recoverable: boolean
}

/** 与 `dto.py` 的 `RecoverTransactionRequest` 一致。 */
export type RecoverTransactionRequest = {
  approved: boolean
}

// --------------------------------------------------------------------------- //
// Agent
// --------------------------------------------------------------------------- //

/** 与 `dto.py` 的 `AgentTimelineItem` 一致。 */
export type AgentTimelineItem = {
  tool: string
  summary: string
  args: Record<string, unknown>
}

/** 与 `dto.py` 的 `AgentApprovalView` 一致。 */
export type AgentApprovalView = {
  kind: string
  preview: string
  plan_ref: string | null
}

/** 与 `dto.py` 的 `AgentRunView` 一致。 */
export type AgentRunView = {
  run_id: string
  status: string
  query: string
  step_count: number
  retrieval_step_count: number
  selected_note_ids: string[]
  retrieved_chunk_ids: string[]
  timeline: AgentTimelineItem[]
  final_answer: string | null
  stop_reason: string | null
  pending_approval: AgentApprovalView | null
}

/** 与 `dto.py` 的 `AgentRunRequest` 一致。 */
export type AgentRunRequest = {
  query: string
  thread_id?: string | null
}

/** 与 `dto.py` 的 `AgentResumeRequest` 一致。 */
export type AgentResumeRequest = {
  approved: boolean
}


// --------------------------------------------------------------------------- //
// Errors
// --------------------------------------------------------------------------- //

/**
 * 服务端统一错误信封，见 `src/obsai/api/errors.py::envelope`。
 *
 * 404、422 与领域异常共用这一个形状，因此前端只需要一个解析器。
 */
export type ApiErrorEnvelope = {
  error: {
    /** 机器可读，用于选中文提示；见 `lib/errors.ts` 的目录。 */
    code: string
    /** 领域异常类名，如 `RecoveryRequiredError`。 */
    type: string
    /** 服务端原文（英文），永远保留以便排查。 */
    message: string
    /** 仅 `invalid_request` 会带：pydantic 的字段级错误列表。 */
    details?: unknown
  }
  /** 与响应头 `X-Request-ID` 同值，用于和后端日志对齐。 */
  request_id: string | null
}
