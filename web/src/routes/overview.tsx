import { FolderX, TriangleAlert } from 'lucide-react'

import { ErrorPanel } from '@/components/error-panel'
import { Detail, Details, StatusCard } from '@/components/status-card'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { useStatus } from '@/hooks/use-status'
import type { JournalView, StatusResponse } from '@/lib/api-types'
import { present } from '@/lib/errors'
import { formatTimestamp } from '@/lib/format'
import {
  summarizeDirtyNotes,
  summarizeIndex,
  summarizeLock,
  summarizeRecovery,
  summarizeVault,
  type StatusSummary,
} from '@/lib/status'

/**
 * 概览页。
 *
 * 计划 §5 B-4：用 `card` 网格展示 Vault 路径、索引/向量代状态、dirty notes、
 * 未完成事务、最近任务；恢复状态用 `alert` 醒目提示。验收标准是"无 Vault 时给出
 * 明确引导（对应 CLI 的 `No vault configured`）"。
 *
 * 三条贯穿全页的规则：
 *
 * 1. **需要用户先处理的事放在卡片上面。** 恢复告警与无 Vault 引导不是卡片之一，
 *    它们是这一页的结论。用户在别处（搜索页看到空结果）遇到问题时，需要一条一眼
 *    就能看到的路径通向原因。
 * 2. **"读不到"和"没有"必须分开写。** `read_status()` 在 Vault 不可读时不读事务
 *    日志、索引打不开时不读脏笔记清单，两种情况服务端返回的都是空集合。把空集合
 *    说成"一切正常"是这一页最容易犯、也最难被发现的错误——判断逻辑全部收在
 *    `lib/status.ts`，本文件只负责渲染。
 * 3. **不渲染笔记正文。** 事务日志里带 `snapshot`（文件被改动前的原文），这里只
 *    列受影响的路径。快照差异属于 D-5 的恢复页，需要单独确认，不该顺手摊在这里。
 */

/** 后端运行时长。只用于页头的一句话，不做秒级精度。 */
function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours} 小时` : `${hours} 小时 ${rest} 分钟`
}

/** 受影响的路径清单。只列路径，不碰 `snapshot`。 */
function JournalPaths({ journal }: { journal: JournalView }) {
  if (journal.originals.length === 0) {
    return <span className="text-muted-foreground">（日志里没有记录受影响的文件）</span>
  }
  return (
    <ul className="flex flex-col gap-0.5">
      {journal.originals.map((original) => (
        <li key={original.path} className="break-all font-mono">
          {original.path}
        </li>
      ))}
    </ul>
  )
}

/**
 * 恢复告警。计划 §5 B-4 点名要用 `alert`，理由充分：写入被冻结时，用户接下来
 * 无论点哪个写入入口都会失败，而失败原因不在那个页面上。
 */
function RecoveryAlert({ status }: { status: StatusResponse }) {
  const pending = status.unfinished_transactions
  return (
    <Alert variant="destructive">
      <TriangleAlert />
      <AlertTitle>有 {pending.length} 个未完成的写入事务，写入已被冻结</AlertTitle>
      <AlertDescription>
        <p>
          这些事务在改到一半时中断了。先看清楚它们动了哪些文件，再逐个恢复——恢复会按
          快照回滚，前提是那些文件没有被手工改过。
        </p>
        <ul className="flex w-full flex-col gap-2">
          {pending.map((journal) => (
            <li key={journal.transaction_id} className="flex flex-col gap-0.5">
              <span className="font-mono">
                {journal.transaction_id} · {journal.status}
              </span>
              <JournalPaths journal={journal} />
            </li>
          ))}
        </ul>
        <pre className="bg-muted w-full overflow-x-auto rounded-md p-3 font-mono">
          {`obsai transaction status
obsai transaction recover <ID>`}
        </pre>
        <p>事务恢复页（D-5）会把这些差异画出来并在覆盖前独立确认；在那之前用 CLI。</p>
      </AlertDescription>
    </Alert>
  )
}

/**
 * 无 Vault 引导 —— B-4 的验收标准就是这一段。
 *
 * 这是**唯一**一个用户什么都没配就打开界面会看到的东西，所以它必须自己把话说完：
 * 配置文件在哪、写什么、为什么不自动创建、以及 CLI 在同样情况下说的是哪句英文。
 * 只说"未配置 Vault"然后留一片空白，等于把用户推去翻文档。
 */
function NoVaultGuidance() {
  return (
    <Alert className="border-amber-500/60">
      <FolderX />
      <AlertTitle>还没有配置 Vault</AlertTitle>
      <AlertDescription>
        <p>
          ObsAgent 不猜你的 Obsidian 库在哪。在{' '}
          <code className="bg-muted rounded px-1 py-0.5 font-mono text-xs">
            ~/.config/obsai/config.toml
          </code>{' '}
          里写上库的根目录（也可以用 <code className="font-mono text-xs">$XDG_CONFIG_HOME</code>
          ）：
        </p>
        <pre className="bg-muted w-full overflow-x-auto rounded-md p-3 font-mono">
          {`[vault]
path = "/Users/you/Documents/MyVault"`}
        </pre>
        <p>
          CLI 在同样的情况下打印的是 <code className="font-mono text-xs">No vault configured</code>
          ，这里是同一句话的中文版。
        </p>
        <p>
          配置文件不存在时 CLI 与 UI 都不会替你创建它。这是刻意的：一个会自己写配置的工具，
          没法解释"我明明没配过，它怎么知道我的库在哪"。
        </p>
        <p>写好后刷新这一页即可，不需要重启服务——后端每次请求都重新读配置。</p>
      </AlertDescription>
    </Alert>
  )
}

/** 配了路径但目录不在。与"没配"是两件事：这一个通常是路径写错或被移动了。 */
function VaultUnreadableAlert({ path }: { path: string }) {
  return (
    <Alert variant="destructive">
      <TriangleAlert />
      <AlertTitle>Vault 目录打不开</AlertTitle>
      <AlertDescription>
        <p>
          配置里写的是 <code className="font-mono">{path}</code>，但它不是一个可以读取的目录。
          检查路径是否写对、目录是否被移动或改名。
        </p>
        <p>
          在修好之前，事务状态与脏笔记这两项读不到，所以下面显示的是"无法检查"而不是"正常"。
        </p>
      </AlertDescription>
    </Alert>
  )
}

/**
 * 「最近任务」在阶段 B 没有数据源。
 *
 * 计划 §5 B-4 要求列出最近任务，但 job 的状态与进度要到阶段 C 才通过 HTTP 暴露：
 * C-1 把 `JobRunner` 落地（状态持久化到 SQLite），C-2 才加 `GET /api/v1/events`。
 * 阶段 B 只有 `/status` 一个端点——`api/app.py` 的 lifespan 注释写着
 * "the job runner arrives with phase C"，那行注释就是这条限制的出处。
 *
 * 所以这里不是"没有任务"，而是"还读不到"。两句话的区别不是措辞问题：说成"没有任务"，
 * 用户会以为自己刚发起的索引任务已经悄悄失败了。
 */
const RECENT_JOBS_SUMMARY: StatusSummary = {
  tone: 'unknown',
  label: '任务运行器尚未接入 API',
  hint: '阶段 B 只暴露了 /status。job 列表与进度由 C-1（状态持久化）和 C-2（GET /api/v1/events）接上，在那之前这里读不到任何任务——不是没有任务，是还看不见。',
}

function OverviewPage() {
  const status = useStatus()
  const failure = status.error == null ? null : present(status.error)
  const data = status.data

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">概览</h1>
        {data !== undefined && (
          <span className="text-muted-foreground text-xs">
            ObsAgent v{data.version} · 后端已运行 {formatUptime(data.uptime_seconds)}
          </span>
        )}
      </header>

      {status.isPending && <OverviewSkeleton />}

      {failure !== null && <ErrorPanel failure={failure} onRetry={() => void status.refetch()} />}

      {data !== undefined && (
        <>
          {data.recovery_required && <RecoveryAlert status={data} />}
          {data.vault_path === null && <NoVaultGuidance />}
          {data.vault_path !== null && !data.vault_ready && (
            <VaultUnreadableAlert path={data.vault_path} />
          )}

          <div className="grid gap-4 md:grid-cols-2">
            <StatusCard title="Vault" summary={summarizeVault(data)}>
              <Details>
                <Detail label="路径">
                  {data.vault_path ?? <span className="text-muted-foreground">未配置</span>}
                </Detail>
                <Detail label="可读">
                  {data.vault_ready ? '是' : '否'}
                </Detail>
              </Details>
            </StatusCard>

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
            </StatusCard>

            <StatusCard
              title="待重新索引的笔记"
              summary={summarizeDirtyNotes(data.index)}
            >
              {data.index.dirty_notes.length > 0 && (
                <ul className="flex flex-col gap-1.5">
                  {data.index.dirty_notes.map((note) => (
                    <li key={note.path} className="flex flex-wrap items-baseline gap-x-2">
                      <span className="break-all font-mono">{note.path}</span>
                      <span className="text-muted-foreground">{note.reason}</span>
                      <span className="text-muted-foreground ml-auto">
                        {formatTimestamp(note.marked_at)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </StatusCard>

            <StatusCard title="未完成事务" summary={summarizeRecovery(data)}>
              {data.unfinished_transactions.length > 0 && (
                <ul className="flex flex-col gap-2">
                  {data.unfinished_transactions.map((journal) => (
                    <li key={journal.transaction_id} className="flex flex-col gap-0.5">
                      <span className="font-mono">
                        {journal.transaction_id} · {journal.status}
                      </span>
                      <JournalPaths journal={journal} />
                    </li>
                  ))}
                </ul>
              )}
              {data.index_dirty_transactions.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  <span className="text-muted-foreground">笔记已改完、只差索引的事务</span>
                  <ul className="flex flex-col gap-0.5">
                    {data.index_dirty_transactions.map((journal) => (
                      <li key={journal.transaction_id} className="font-mono">
                        {journal.transaction_id} · {journal.status} · {journal.originals.length} 个文件
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </StatusCard>

            <StatusCard title="最近任务" summary={RECENT_JOBS_SUMMARY} />

            <StatusCard title="写入锁" summary={summarizeLock(data)} />
          </div>
        </>
      )}
    </div>
  )
}

/** 骨架屏按最终版式排：告警位 + 六张卡，避免加载完成时整页跳一下。 */
function OverviewSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-4 md:grid-cols-2">
        {[0, 1, 2, 3, 4, 5].map((key) => (
          <Skeleton key={key} className="h-32 w-full rounded-xl" />
        ))}
      </div>
    </div>
  )
}

export { OverviewPage }
