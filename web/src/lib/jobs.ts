/**
 * 任务记录 → 可以直接显示的中文，以及页面要做的几个判断。
 *
 * 服务端的任务记录里**没有一句给人看的话**：`kind` 是标识符，`message` 是步骤名，
 * `detail` 是标量。这是有意的——同一条记录要同时服务终端和中文界面，所以后端只发
 * 标识符，词由适配层给（见 `src/obsai/application/index_jobs.py` 的模块说明）。
 * 这个模块就是那个适配层。
 *
 * 四件事在这里定：
 *
 * - **三张词表。** `JOB_KIND_LABELS` / `JOB_STATUS_LABELS` / `JOB_STEP_LABELS`，
 *   分别对应后端的 kind 常量、`JobStatus` 联合类型和步骤常量。漏一条不会报错，只会让
 *   用户看到一个英文标识符，所以 `tests/unit/test_web_contract.py` 双向比对这三张表。
 * - **未知的报 `null`，既不报原文也不报空白。** 调用方必须自己决定怎么显示一个没翻译
 *   的步骤；把它悄悄吞掉，页面上就会出现"任务在跑，但什么都不说"。状态不在此列——
 *   它是个封闭联合，编译器已经保证了完整性，所以那里退化成原文即可。
 * - **进度条画哪一种。** 两个索引任务只报步骤、不报分数，所以是"不确定"的那一种。
 *   这不是前端偷懒，见 `jobProgress` 的说明。
 * - **失败文案从类名反推。** 任务失败没有响应信封可解析，只有一条落在日志里的记录，
 *   所以走 `errors.ts` 的 `codeFromType` + `wordingFor`。
 *
 * 全部是纯函数，不碰 React，因此可以在 node 环境下逐个状态验证。
 */

import type { JobStatus, JobView } from '@/lib/api-types'
import { codeFromType, wordingFor, type ErrorPresentation } from '@/lib/errors'
import type { JobStreamStatus } from '@/lib/sse'
import type { StatusTone } from '@/lib/status'

/** 与 `application/index_jobs.py` 的 `EMBEDDINGS_STALE` 一致。 */
const EMBEDDINGS_STALE = 'embeddings_stale'

/** 与 `application/index_jobs.py` 的 kind 常量一致。 */
export const JOB_KIND_LABELS: Record<string, string> = {
  'index-update': '索引更新',
  'index-rebuild': '索引重建',
  embedding: '向量生成',
}

/** 与 `dto.py` 的 `JobStatus` 一致。`Record<JobStatus, …>` 让漏一个就编译不过。 */
export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  queued: '排队中',
  running: '进行中',
  awaiting_approval: '等待确认',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '被中断',
}

/** 与 `application/index_jobs.py` 的步骤常量一致。 */
export const JOB_STEP_LABELS: Record<string, string> = {
  scanning: '正在扫描 Vault',
  synchronizing: '正在同步笔记',
  building: '正在重建索引',
  embedding: '正在生成向量',
  done: '已完成',
}

/**
 * 与 `dto.py` 的 `JobView.terminal` 一致。
 *
 * `interrupted` 在里面，而它不是 `cancelled`：没有人要求它停，它也没有机会回滚。
 * 两者都不该被画成成功，但下一步动作完全不同。
 */
const TERMINAL_STATUSES: readonly JobStatus[] = [
  'succeeded',
  'failed',
  'cancelled',
  'interrupted',
]

/** `detail` 里要显示的那些计数，以及它们的名字。顺序就是显示顺序。 */
const COUNT_FIELDS: readonly (readonly [string, string])[] = [
  ['notes', '笔记'],
  ['created', '新增'],
  ['modified', '修改'],
  ['renamed', '重命名'],
  ['moved', '移动'],
  ['deleted', '删除'],
  ['unchanged', '未变'],
  ['affected_links', '受影响的链接'],
]

export const EMBEDDINGS_STALE_HINT =
  '重建后的索引里没有向量，语义检索会一直降级。运行 obsai index embeddings 重新生成。'

/**
 * `detail` 里的一个数字。
 *
 * `detail` 的类型是 `Record<string, unknown>`，因为它按任务不同而不同；这里只认真的
 * 是数字的值。字符串 `"3"` 不算——服务端发的是 JSON 数字，一个字符串说明契约漂了，
 * 而把它 `Number()` 一下只会把漂移藏起来。
 */
function numericDetail(job: JobView, key: string): number | null {
  const value = job.detail[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function jobKindLabel(job: JobView): string | null {
  return JOB_KIND_LABELS[job.kind] ?? null
}

/**
 * 状态的中文。
 *
 * 没有"未知"这一支：`JobStatus` 是封闭联合，词表是 `Record<JobStatus, string>`，
 * 所以查得到是编译期的事。运行时兜底回原文，只在服务端加了状态而前端还没跟上时发生，
 * 那时显示 `queued_v2` 也比显示空白好。
 */
export function jobStatusLabel(job: JobView): string {
  return (JOB_STATUS_LABELS as Record<string, string>)[job.status] ?? job.status
}

/** 当前步骤的中文；没有步骤、或步骤不认识时是 `null`。 */
export function jobStepLabel(job: JobView): string | null {
  if (job.message === null) return null
  return JOB_STEP_LABELS[job.message] ?? null
}

/** 服务端报了一个前端不认识的步骤。页面应该把原始 token 显示出来，而不是留白。 */
export function jobHasUnknownStep(job: JobView): boolean {
  return job.message !== null && !(job.message in JOB_STEP_LABELS)
}

export function jobIsTerminal(job: JobView): boolean {
  return TERMINAL_STATUSES.includes(job.status)
}

/**
 * 能不能取消。
 *
 * 与"是不是终态"是同一件事，写成一个函数是因为**协作式取消**这条语义值得一个名字：
 * 按下去不等于停下来，任务会在下一个安全点退出，所以按钮按下之后仍然要显示状态。
 * 已经结束的任务按下去，服务端会回一个终态不变的任务——那是有意的，不是 409。
 */
export function jobCanBeCancelled(job: JobView): boolean {
  return !jobIsTerminal(job)
}

export type JobProgress =
  | { readonly kind: 'indeterminate' }
  | { readonly kind: 'fraction'; readonly value: number; readonly max: number }

/**
 * 进度条画哪一种。
 *
 * 两个索引任务都只报**步骤**，不报分数，所以答案是 `indeterminate`：画一条来回跑的
 * 条，真正传达信息的是步骤文案。
 *
 * 为什么服务端不报分数：`IncrementalIndexer.update()` 和
 * `ShadowIndexRebuilder.rebuild()` 都不接受进度回调，而 `rebuild()` 内部的建库、校验、
 * 指纹比对、替换四段从外面完全看不见（见 `application/index_jobs.py` 的模块说明）。
 * 与其编一个"第 3/7 步"，不如说"正在重建 412 条笔记的索引"——`detail.notes` 就是那个数。
 *
 * `fraction` 这一支目前没有任何生产者，留着是因为"这个任务能不能量"是任务的性质而不是
 * 页面的性质：真出现能报分子分母的任务（embedding 批次是唯一的候选），页面不用动。
 */
export function jobProgress(job: JobView): JobProgress {
  const completed = numericDetail(job, 'completed')
  const total = numericDetail(job, 'total')
  if (completed === null || total === null || total <= 0) return { kind: 'indeterminate' }
  return { kind: 'fraction', value: Math.min(completed, total), max: total }
}

export type JobCount = {
  readonly key: string
  readonly label: string
  readonly value: number
}

/**
 * 这次任务改了多少东西。
 *
 * 只认 `COUNT_FIELDS` 里那几个键，其余一律不显示。这不是洁癖：`detail` 在失败时还带着
 * `traceback`，把它摊在页面上既没用又难看，而"哪些键是给人看的"只能由这里说了算。
 */
export function jobCounts(job: JobView): readonly JobCount[] {
  return COUNT_FIELDS.flatMap(([key, label]) => {
    const value = numericDetail(job, key)
    return value === null ? [] : [{ key, label, value }]
  })
}

/** 重建完成的标志：这个索引里的向量全没了，得重新生成。 */
export function embeddingsAreStale(job: JobView): boolean {
  return job.detail[EMBEDDINGS_STALE] === true
}

/** 判断"向量没了"时用得上的那部分索引状态。取子集，免得为了两个字段把整个响应拖进来。 */
export type IndexVectorState = {
  readonly exists: boolean
  readonly note_count: number
  readonly vector_count: number
}

/**
 * 该不该提示"向量需要重新生成"。
 *
 * 两个条件缺一不可，而它们分别来自两个真相源：
 *
 * - **发生过一次成功的重建**（任务记录里带 `embeddings_stale`）。它解释了向量*为什么*
 *   没了——是重建清掉的，不是本来就还没建。
 * - **现在索引里确实一条向量都没有**（`/status`）。这是*当下*的事实。
 *
 * 只看前者，提示会在用户已经用 CLI 重新生成向量之后继续挂着，而用户没有任何办法
 * 让它消失；只看后者，就和概览页那句"索引为空"重复，也说不出"去运行 embeddings"。
 * 两个都看，提示只在真正需要动作的时候出现，并且动作做完就自己消失。
 *
 * `note_count === 0` 时不算：那是"索引是空的"，该由索引状态卡去说。
 */
export function needsEmbeddingRegeneration(
  jobs: readonly JobView[],
  index: IndexVectorState,
): boolean {
  if (!index.exists || index.note_count === 0 || index.vector_count > 0) return false
  return jobs.some((job) => job.status === 'succeeded' && embeddingsAreStale(job))
}

/**
 * 失败的中文，以及下一步。
 *
 * 只在 `failed` 时非空。`interrupted` 不是失败：没有人要求它停，也没有异常可解释，
 * 那是"我们不知道它做到哪一步"，属于另一种句子（见 `summarizeJob`）。
 */
export function jobFailure(job: JobView): ErrorPresentation | null {
  if (job.status !== 'failed') return null
  const detail = job.error ?? undefined
  if (job.error_code === null) {
    return { title: '任务失败', detail, retryable: false }
  }
  return wordingFor(codeFromType(job.error_code), detail)
}

export type JobSummary = {
  /**
   * 色调。`ok` 在这里的意思是"不需要你做什么"，不是"一切正常"——一个正在跑的任务
   * 也是 `ok`。`warning` 表示任务没有走完或正在等人，`danger` 表示失败。
   */
  tone: StatusTone
  /** 一句话说明现在是什么情况。 */
  label: string
  /** 下一步该做什么；不需要动作时是空串，而不是"请稍候"这种废话。 */
  hint: string
}

/** 一条任务记录 → 一行可以直接显示的状态。未知 kind 退化成原始标识符，不编名字。 */
export function summarizeJob(job: JobView): JobSummary {
  const kind = jobKindLabel(job) ?? job.kind
  const step = jobStepLabel(job)

  switch (job.status) {
    case 'queued':
      return { tone: 'ok', label: `${kind}已排队`, hint: '正在等待前一个任务结束。' }
    case 'running':
      return {
        tone: 'ok',
        label: step === null ? `${kind}进行中` : `${kind}：${step}`,
        hint: '',
      }
    case 'awaiting_approval':
      return { tone: 'warning', label: `${kind}正在等待确认`, hint: '确认或拒绝之后任务才会继续。' }
    case 'succeeded':
      return {
        tone: 'ok',
        label: `${kind}已完成`,
        hint: embeddingsAreStale(job) ? EMBEDDINGS_STALE_HINT : '',
      }
    case 'failed': {
      const failure = jobFailure(job)
      return {
        tone: 'danger',
        label: failure?.title ?? `${kind}失败`,
        hint: failure?.hint ?? '展开详情查看服务端原文。',
      }
    }
    case 'cancelled':
      return {
        tone: 'warning',
        label: `${kind}已取消`,
        hint: '取消是协作式的：正在进行的那一步会先回滚，所以索引仍然是上一个完整版本。',
      }
    case 'interrupted':
      return {
        tone: 'warning',
        label: `${kind}被中断`,
        hint: '后端进程在任务运行期间退出，无法得知它做到哪一步。重新运行一次即可。',
      }
  }
}

// --------------------------------------------------------------------------- //
// 流的合并
// --------------------------------------------------------------------------- //

export type JobsFeedState = {
  readonly received: boolean
  readonly status: JobStreamStatus
  readonly parseError: unknown
}

/**
 * 任务列表现在可不可信。
 *
 * 这一条是 B-4 立下的规矩在索引页上的延续：**空集合有歧义**。事件流还没连上、
 * 后端刚重启、负载解析失败——三种情况下 `jobs` 都是（或停在）一个列表，而它们要
 * 说的话完全不同。把任何一种说成"没有任务"，用户就会以为自己刚发起的重建悄悄失败了。
 *
 * `received` 是判断的锚：它只由**快照**置位，而快照是"当前的全部任务"。所以
 * `received === false` 严格等价于"还不知道有哪些任务"。
 */
export function describeJobsFeed(feed: JobsFeedState): JobSummary {
  if (feed.parseError !== null) {
    return {
      tone: 'warning',
      label: '任务进度可能不是最新的',
      hint: '事件流送来的负载解析失败，列表停在上一次成功的更新上。刷新页面可以重新拉一份快照。',
    }
  }
  if (!feed.received) {
    return {
      tone: 'unknown',
      label: '还读不到任务列表',
      hint:
        feed.status === 'open'
          ? '事件流已连上，但还没有收到第一份快照。'
          : '正在连接事件流。后端可能没有启动——这不等于没有任务。',
    }
  }
  return { tone: 'ok', label: '任务列表已连接', hint: '' }
}

/**
 * 最近的在最前面。
 *
 * 服务端的 `GET /jobs` 是**旧的在前**（它描述的是历史），而列表页要的是"刚才发生了什么"。
 * 排序放在这里而不是页面里，是因为概览页的"最近任务"和索引页的任务列表必须是同一个
 * 顺序，两处各排一次迟早会分叉。
 *
 * `job_id` 只是稳定排序的兜底：同一毫秒创建的两个任务，顺序不该随 Map 的迭代顺序变。
 */
export function sortJobs(jobs: readonly JobView[]): JobView[] {
  return [...jobs].sort((left, right) => {
    if (left.created_at !== right.created_at) return left.created_at < right.created_at ? 1 : -1
    return left.job_id < right.job_id ? 1 : -1
  })
}

/**
 * 变化到达：按 `job_id` 更新或追加。
 *
 * 服务端的 `job` 事件只带**变了的那几个**（`api/routes/jobs.py` 的 `_event`），所以这里
 * 是 upsert 而不是替换。整份列表的替换是快照事件的事，两者不能混——把一条变化当成全量
 * 会让列表在一次进度更新后只剩一个任务。
 */
export function mergeJobs(current: readonly JobView[], incoming: readonly JobView[]): JobView[] {
  const byId = new Map(current.map((job) => [job.job_id, job]))
  for (const job of incoming) byId.set(job.job_id, job)
  return sortJobs([...byId.values()])
}

