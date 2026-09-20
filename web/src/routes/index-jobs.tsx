import { Database, TriangleAlert } from 'lucide-react'
import { Link } from 'react-router'

import { ErrorPanel } from '@/components/error-panel'
import { Detail, Details, StatusCard } from '@/components/status-card'
import { StatusDot } from '@/components/status-dot'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import {
  useCancelJob,
  useIndexRebuild,
  useIndexUpdate,
  useJobs,
  type JobsFeed,
} from '@/hooks/use-jobs'
import { useStatus } from '@/hooks/use-status'
import type { JobView } from '@/lib/api-types'
import { present, type ErrorPresentation } from '@/lib/errors'
import { formatTimestamp } from '@/lib/format'
import {
  describeJobsFeed,
  jobCanBeCancelled,
  jobCounts,
  jobFailure,
  jobHasUnknownStep,
  jobIsTerminal,
  jobKindLabel,
  jobProgress,
  jobStatusLabel,
  needsEmbeddingRegeneration,
  summarizeJob,
} from '@/lib/jobs'
import { summarizeIndex } from '@/lib/status'

/**
 * 索引页。
 *
 * 计划 §6 C-3：展示进度、`progress` 条、可取消；重建后明确提示向量需重新生成。
 * 验收标准是「取消保留旧索引；rebuild 中断不破坏线上库」——两条都由后端保证
 * （见 `src/obsai/application/index_jobs.py`），这一页要做的是**把保证说清楚**，
 * 而不是重新实现一遍。
 *
 * 三条贯穿全页的规则：
 *
 * 1. **不假装知道进度。** 两个索引任务只报步骤、不报分数（`lib/jobs.ts::jobProgress`），
 *    所以进度条画的是"确实在动"，不是"完成了百分之多少"。画一个假的百分比是这一页
 *    最容易犯、也最没意义的错误。
 * 2. **"读不到"和"没有"必须分开写。** 事件流没连上时 `jobs` 是空数组，而那不是
 *    "没有任务"。判断收在 `describeJobsFeed`，本文件只负责渲染。
 * 3. **需要用户先处理的事放在最上面。** 向量没了、Vault 不可读、事务待恢复——这些
 *    会让下面的按钮必然失败，所以它们不是卡片之一，是这一页的结论。
 */

/** 最近任务最多列这么多。更早的属于"翻记录"，不属于"看状态"。 */
const RECENT_LIMIT = 10

/**
 * 不确定进度的进度条。
 *
 * Radix 的 `Progress` 需要一个 `value`，没有值就渲染成一条空的——那看起来像"卡住了"。
 * 这里用一条来回跑的窄条表示"在动，但我不知道还剩多少"。
 */
function IndeterminateBar() {
  return (
    <div
      className="bg-primary/20 relative h-2 w-full overflow-hidden rounded-full"
      role="progressbar"
      aria-label="进行中"
    >
      <div className="animate-indeterminate bg-primary absolute inset-y-0 w-1/3 rounded-full" />
    </div>
  )
}

/** 任务报的计数。只显示 `lib/jobs.ts` 认识的那几个键。 */
function Counts({ job }: { job: JobView }) {
  const counts = jobCounts(job)
  if (counts.length === 0) return null
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
      {counts.map((count) => (
        <li key={count.key} className="text-muted-foreground">
          {count.label} <span className="text-foreground font-medium">{count.value}</span>
        </li>
      ))}
    </ul>
  )
}

/**
 * 一个正在跑的任务：步骤 + 进度 + 取消。
 *
 * 取消按钮是**唯一**的取消入口，它做的事只是发一个 `POST`——关页面、刷新、断开事件流
 * 都不会取消任何任务（见 `lib/sse.ts` 的模块说明）。所以按钮旁边写明了"协作式"：
 * 按下去不等于立刻停，任务会在下一个安全点退出。
 */
function RunningJob({
  job,
  failure,
  onCancel,
  cancelling,
}: {
  job: JobView
  failure: ErrorPresentation | null
  onCancel: (jobId: string) => void
  cancelling: boolean
}) {
  const summary = summarizeJob(job)
  const progress = jobProgress(job)

  return (
    <li className="flex flex-col gap-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <StatusDot tone={summary.tone} />
        <span className="text-sm font-medium">{jobKindLabel(job) ?? job.kind}</span>
        <span className="text-muted-foreground text-xs">{jobStatusLabel(job)}</span>
        <span className="text-muted-foreground ml-auto text-xs">
          {formatTimestamp(job.created_at)}
        </span>
      </div>

      <p className="text-sm">{summary.label}</p>

      {progress.kind === 'fraction' ? (
        <Progress value={(progress.value / progress.max) * 100} />
      ) : (
        <IndeterminateBar />
      )}

      {jobHasUnknownStep(job) && (
        <p className="text-muted-foreground text-xs">
          服务端报了一个这个界面还不认识的步骤：
          <code className="bg-muted ml-1 rounded px-1 py-0.5 font-mono">{job.message}</code>
        </p>
      )}

      <Counts job={job} />

      {failure !== null && (
        <pre className="bg-muted overflow-x-auto rounded-md p-2 text-xs">{failure.detail}</pre>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <Button
          size="sm"
          variant="outline"
          onClick={() => onCancel(job.job_id)}
          disabled={cancelling || !jobCanBeCancelled(job)}
        >
          取消
        </Button>
        <span className="text-muted-foreground text-xs">
          取消是协作式的：任务会在下一个安全点停下，已经在事务里的一步会先回滚。
        </span>
      </div>
    </li>
  )
}

/** 一个已经结束的任务。 */
function FinishedJob({ job }: { job: JobView }) {
  const summary = summarizeJob(job)
  const failure = jobFailure(job)

  return (
    <li className="flex flex-col gap-1.5 border-b pb-3 last:border-b-0 last:pb-0">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <StatusDot tone={summary.tone} />
        <span className="text-sm">{summary.label}</span>
        <span className="text-muted-foreground ml-auto text-xs">
          {formatTimestamp(job.finished_at ?? job.created_at)}
        </span>
      </div>
      {summary.hint !== '' && (
        <p className="text-muted-foreground text-xs leading-relaxed">{summary.hint}</p>
      )}
      <Counts job={job} />
      {failure !== null && failure.detail !== undefined && (
        // 服务端原文刻意保留而不折叠：本地工具的使用者就是它的作者。
        <pre className="bg-muted overflow-x-auto rounded-md p-2 text-xs">{failure.detail}</pre>
      )}
    </li>
  )
}

/**
 * 任务区。
 *
 * 两种空状态的区别就是这一块存在的理由：`received === false` 时说"还读不到"，
 * `received === true` 而列表为空时才说"还没有跑过任务"。
 */
function JobsSection({ feed }: { feed: JobsFeed }) {
  const cancel = useCancelJob()
  const running = feed.jobs.filter((job) => !jobIsTerminal(job))
  const finished = feed.jobs.filter((job) => jobIsTerminal(job)).slice(0, RECENT_LIMIT)
  const summary = describeJobsFeed(feed)

  return (
    <Card className="gap-4">
      <CardHeader className="gap-1.5">
        <CardTitle className="flex items-center gap-2 text-sm font-medium">
          <StatusDot tone={summary.tone} />
          任务
        </CardTitle>
        <CardDescription className="text-foreground">{summary.label}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 text-xs">
        {summary.hint !== '' && (
          <p className="text-muted-foreground leading-relaxed">{summary.hint}</p>
        )}

        {running.length > 0 && (
          <ul className="flex flex-col gap-3">
            {running.map((job) => (
              <RunningJob
                key={job.job_id}
                job={job}
                failure={jobFailure(job)}
                onCancel={(jobId) => cancel.mutate(jobId)}
                cancelling={cancel.isPending}
              />
            ))}
          </ul>
        )}

        {running.length > 0 && finished.length > 0 && <Separator />}

        {finished.length > 0 && (
          <ul className="flex flex-col gap-3">
            {finished.map((job) => (
              <FinishedJob key={job.job_id} job={job} />
            ))}
          </ul>
        )}

        {feed.received && feed.jobs.length === 0 && (
          <p className="text-muted-foreground">
            这个索引还没有跑过任务。上面两个按钮起的任务会出现在这里。
          </p>
        )}

        {cancel.error != null && <ErrorPanel failure={present(cancel.error)} />}
      </CardContent>
    </Card>
  )
}

/** 重建之后、向量还没生成时的提示。放在最上面，因为它是这一页唯一需要用户动手的事。 */
function EmbeddingsStaleAlert() {
  return (
    <Alert className="border-amber-500/60">
      <TriangleAlert />
      <AlertTitle>重建后的索引里没有向量</AlertTitle>
      <AlertDescription>
        <p>
          重建写出的是一个全新的库，向量表是空的。笔记与分块都在，但语义检索现在会一直
          降级为关键词检索——混合检索也拿不到语义那一半的结果。
        </p>
        <p>向量必须重新生成。生成它们会调用嵌入服务，可能产生费用。</p>
        <pre className="bg-muted w-full overflow-x-auto rounded-md p-3 font-mono">
          obsai index embeddings
        </pre>
        <div className="flex flex-wrap items-center gap-3 pt-2">
          <Button size="sm" asChild>
            <Link to="/embedding">前往向量生成计划</Link>
          </Button>
          <span className="text-muted-foreground text-xs">
            在生成前审查分块数、费用预估与预算限制，经批准后启动后台生成。
          </span>
        </div>
      </AlertDescription>
    </Alert>
  )
}

/**
 * 操作区。
 *
 * 两个按钮都可能因为**请求层面**的原因被拒：没有 Vault（400）、Vault 目录不存在（400）、
 * 有未完成事务（423）。这些在点下去之前就能知道，所以这里禁用按钮并把原因写出来——
 * 让用户点一个必然失败的按钮，再给他看一条 400，是更差的体验。
 */
function MaintenanceCard({
  vaultReady,
  recoveryRequired,
  busy,
}: {
  vaultReady: boolean
  recoveryRequired: boolean
  busy: boolean
}) {
  const update = useIndexUpdate()
  const rebuild = useIndexRebuild()
  const blocked = !vaultReady || recoveryRequired
  // 两个按钮共用一个错误位：同时只会有一个在跑（另一个被 `busy` 挡住），所以
  // "谁失败了"这个问题不需要用户去分辨。
  const failure =
    update.error != null
      ? present(update.error)
      : rebuild.error != null
        ? present(rebuild.error)
        : null

  return (
    <Card className="gap-4">
      <CardHeader className="gap-1.5">
        <CardTitle className="text-sm font-medium">维护</CardTitle>
        <CardDescription>
          两个操作都只写索引，不改 Vault 里的任何文件——索引是可重建的派生物。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-3">
            <Button size="sm" onClick={() => update.mutate()} disabled={blocked || busy || update.isPending}>
              增量更新
            </Button>
            <span className="text-muted-foreground text-xs">
              把改过的笔记同步进索引。索引还没建时，这个按钮就是"建索引"。
            </span>
          </div>
        </div>

        <Separator />

        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-3">
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button size="sm" variant="outline" disabled={blocked || busy || rebuild.isPending}>
                  全量重建
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>全量重建索引？</AlertDialogTitle>
                  <AlertDialogDescription>
                    会新建一个索引库、校验通过之后原子替换现有的那个。过程中断不会损坏当前
                    索引——旧的那个会原样留在原地。
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <p className="text-muted-foreground text-sm leading-relaxed">
                  代价是<strong>已生成的向量会被丢掉</strong>（生成它们可能花过钱），完成后需要
                  重新运行
                  <code className="bg-muted mx-1 rounded px-1 py-0.5 font-mono text-xs">
                    obsai index embeddings
                  </code>
                  。索引损坏、或怀疑它和 Vault 不一致时用这个；平时用增量更新就够了。
                </p>
                <AlertDialogFooter>
                  <AlertDialogCancel>取消</AlertDialogCancel>
                  <AlertDialogAction onClick={() => rebuild.mutate()}>重建</AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
            <span className="text-muted-foreground text-xs">
              丢掉索引重新建一遍。比增量更新慢，但能修掉增量同步修不好的不一致。
            </span>
          </div>
        </div>

        <Separator />

        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-3">
            <Button size="sm" variant="outline" asChild>
              <Link to="/embedding">向量生成计划</Link>
            </Button>
            <span className="text-muted-foreground text-xs">
              审查未向量化的分块、预估成本与请求数，确认后在后台生成嵌入。
            </span>
          </div>
        </div>

        {blocked && (
          <p className="text-muted-foreground text-xs leading-relaxed">
            {recoveryRequired
              ? '有未完成的写入事务，写入已被冻结，索引操作现在会被拒绝。先到「恢复」页处理，或运行 obsai transaction status。'
              : 'Vault 不可读，索引操作会被拒绝。先在 config.toml 里把 vault.path 指向一个存在的目录。'}
          </p>
        )}

        {!blocked && busy && (
          <p className="text-muted-foreground text-xs">
            有任务正在运行。任务运行器一次只跑一个，它结束之后才能开始下一个。
          </p>
        )}

        {failure !== null && <ErrorPanel failure={failure} />}
      </CardContent>
    </Card>
  )
}

function IndexJobsPage() {
  const status = useStatus()
  const feed = useJobs()

  const data = status.data
  const statusFailure = status.error == null ? null : present(status.error)
  const busy = feed.jobs.some((job) => !jobIsTerminal(job))
  const staleEmbeddings =
    data !== undefined && needsEmbeddingRegeneration(feed.jobs, data.index)

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Database className="text-muted-foreground size-5" />
          索引
        </h1>
        <p className="text-muted-foreground text-sm">
          索引是从 Vault 派生的：删掉它不会丢任何笔记，重建一次就回来了。所有索引操作都在
          后台跑，可以离开这一页，也可以取消。
        </p>
      </header>

      {status.isPending && (
        <div className="grid gap-4">
          {[0, 1].map((key) => (
            <Skeleton key={key} className="h-40 w-full rounded-xl" />
          ))}
        </div>
      )}

      {statusFailure !== null && (
        <ErrorPanel failure={statusFailure} onRetry={() => void status.refetch()} />
      )}

      {data !== undefined && (
        <>
          {staleEmbeddings && <EmbeddingsStaleAlert />}

          <StatusCard title="索引" summary={summarizeIndex(data.index)}>
            <Details>
              <Detail label="笔记 / 分块 / 向量">
                {data.index.note_count} / {data.index.chunk_count} / {data.index.vector_count}
              </Detail>
              <Detail label="索引代">
                {data.index.generation ?? <span className="text-muted-foreground">未登记</span>}
              </Detail>
              <Detail label="语义检索">
                {data.index.semantic_ready ? '可用' : '不可用（会降级为关键词检索）'}
              </Detail>
              <Detail label="索引文件">{data.index.path}</Detail>
            </Details>

            {data.index.dirty_notes.length > 0 && (
              <div className="flex flex-col gap-1.5">
                <span className="text-muted-foreground">
                  这 {data.index.dirty_notes.length} 篇笔记改了但还没进索引，增量更新会处理它们
                </span>
                <ul className="flex flex-col gap-0.5">
                  {data.index.dirty_notes.map((note) => (
                    <li key={note.path} className="break-all font-mono">
                      {note.path}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </StatusCard>

          <MaintenanceCard
            vaultReady={data.vault_path !== null && data.vault_ready}
            recoveryRequired={data.recovery_required}
            busy={busy}
          />
        </>
      )}

      <JobsSection feed={feed} />
    </div>
  )
}

export default IndexJobsPage
