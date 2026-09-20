import { useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  Copy,
  FileCheck2,
  History,
  Loader2,
  Play,
  Sparkles,
  XCircle,
} from 'lucide-react'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { AgentApprovalCard } from '@/components/agent-approval-card'
import {
  AgentLimitsBadge,
  AgentStopReasonAlert,
} from '@/components/agent-limits-badge'
import { ErrorPanel } from '@/components/error-panel'
import { ToolTimeline } from '@/components/tool-timeline'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Separator } from '@/components/ui/separator'
import { AGENT_RUN_QUERY_KEY, useAgentRun } from '@/hooks/use-agent'
import { api } from '@/lib/api'
import type { AgentRunView } from '@/lib/api-types'
import { present, type ErrorPresentation } from '@/lib/errors'
import { noteHref } from '@/lib/note'

const MAX_STEPS = 15
const MAX_RETRIEVAL_STEPS = 5

const PRESET_QUERIES = [
  'search Redis',
  '搜索系统架构',
  '创建 Notes/AgentDemo.md',
]

export function AgentPage() {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const runIdFromUrl = searchParams.get('run_id')?.trim() || ''

  const [query, setQuery] = useState('')
  const [starting, setStarting] = useState(false)
  const [resuming, setResuming] = useState(false)
  const [copied, setCopied] = useState(false)
  const [customRunId, setCustomRunId] = useState('')
  const [localRun, setLocalRun] = useState<AgentRunView | null>(null)
  const [actionError, setActionError] = useState<ErrorPresentation | null>(null)

  // 通过 React Query 自动凭 URL run_id 加载快照（页面刷新后无损恢复）
  const {
    data: fetchedRun,
    isLoading: isFetchingRun,
    error: queryError,
    refetch,
  } = useAgentRun(runIdFromUrl || null)

  // 活跃工作流：优先采用最新返回的结果或 query 缓存
  const activeRun: AgentRunView | null = localRun ?? fetchedRun ?? null
  const isBusy = starting || isFetchingRun || resuming
  const effectiveError = actionError ?? (queryError ? present(queryError) : null)

  // 提交新运行
  const handleStartRun = async (promptToRun?: string) => {
    const text = (promptToRun ?? query).trim()
    if (!text || isBusy) return

    setStarting(true)
    setActionError(null)
    try {
      const newRun = await api.startAgentRun({ query: text })
      setLocalRun(newRun)
      queryClient.setQueryData([AGENT_RUN_QUERY_KEY, newRun.run_id], newRun)
      setSearchParams({ run_id: newRun.run_id }, { replace: true })
    } catch (err) {
      setActionError(present(err))
    } finally {
      setStarting(false)
    }
  }

  // 调取已有工作流快照
  const handleFetchHistory = () => {
    const targetId = customRunId.trim()
    if (!targetId || isBusy) return

    setActionError(null)
    setLocalRun(null)
    setSearchParams({ run_id: targetId }, { replace: true })
    setCustomRunId('')
  }

  // 复制 run_id
  const handleCopyRunId = () => {
    if (!activeRun?.run_id) return
    void navigator.clipboard.writeText(activeRun.run_id)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  // 人机协同审批恢复
  const handleResume = async (approved: boolean) => {
    if (!activeRun?.run_id || resuming) return

    setResuming(true)
    setActionError(null)
    try {
      const updated = await api.resumeAgentRun(activeRun.run_id, { approved })
      setLocalRun(updated)
      queryClient.setQueryData([AGENT_RUN_QUERY_KEY, updated.run_id], updated)
    } catch (err) {
      setActionError(present(err))
    } finally {
      setResuming(false)
    }
  }

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-6" data-testid="agent-page">
      {/* 头部标题与控制区 */}
      <div className="flex flex-col justify-between gap-4 md:flex-row md:items-center">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold tracking-tight">Agent</h1>
            <Badge variant="outline" className="text-xs">智能体</Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            有界 LangGraph 状态机驱动，支持结构化工具时间线跟踪与人机协同（HITL）审批闭环。
          </p>
        </div>

        {/* 顶部指标徽章 */}
        {activeRun && (
          <div className="flex flex-wrap items-center gap-2">
            {activeRun.status === 'completed' && (
              <Badge className="border-emerald-500/20 bg-emerald-500/10 text-emerald-600 hover:bg-emerald-500/20">
                <CheckCircle2 className="mr-1 h-3.5 w-3.5" />
                已完成
              </Badge>
            )}
            {activeRun.status === 'interrupted' && (
              <Badge className="border-amber-500/20 bg-amber-500/10 text-amber-600 hover:bg-amber-500/20">
                <AlertCircle className="mr-1 h-3.5 w-3.5" />
                等待审批
              </Badge>
            )}
            {activeRun.status === 'failed' && (
              <Badge className="border-rose-500/20 bg-rose-500/10 text-rose-600 hover:bg-rose-500/20">
                <XCircle className="mr-1 h-3.5 w-3.5" />
                执行中断
              </Badge>
            )}
            {activeRun.status === 'running' && (
              <Badge className="border-blue-500/20 bg-blue-500/10 text-blue-600 hover:bg-blue-500/20">
                <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                执行中
              </Badge>
            )}

            <AgentLimitsBadge
              stepCount={activeRun.step_count}
              maxSteps={MAX_STEPS}
              retrievalStepCount={activeRun.retrieval_step_count}
              maxRetrievalSteps={MAX_RETRIEVAL_STEPS}
              stopReason={activeRun.stop_reason}
              showStopReasonBadge={true}
            />
          </div>
        )}
      </div>

      {/* 错误展示 */}
      {effectiveError && (
        <ErrorPanel
          failure={effectiveError}
          onRetry={runIdFromUrl ? () => void refetch() : undefined}
        />
      )}

      {/* 会话发起卡片 */}
      <Card className="border-border/80 shadow-xs">
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <Bot className="h-4 w-4 text-primary" />
            会话指令与工作流控制
          </CardTitle>
          <CardDescription className="text-xs">
            输入自然语言目标，智能体会根据只读或写入意图调度相关工具。页面刷新后凭工作流 ID 自动恢复全部状态。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <form
            onSubmit={(e) => {
              e.preventDefault()
              void handleStartRun()
            }}
            className="flex gap-2"
          >
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="输入任务指令（例如：search Redis 或 创建 Notes/QuickStart.md）..."
              className="flex-1"
              disabled={isBusy}
              data-testid="agent-query-input"
            />
            <Button
              type="submit"
              disabled={isBusy || !query.trim()}
              data-testid="agent-run-button"
            >
              {starting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  执行中
                </>
              ) : (
                <>
                  <Play className="mr-2 h-4 w-4 fill-current" />
                  运行
                </>
              )}
            </Button>
          </form>

          {/* 快捷示例 */}
          <div className="flex flex-wrap items-center gap-2 pt-1 text-xs text-muted-foreground">
            <span className="flex items-center gap-1 font-medium">
              <Sparkles className="h-3.5 w-3.5 text-primary" />
              快捷尝试:
            </span>
            {PRESET_QUERIES.map((item) => (
              <button
                key={item}
                type="button"
                className="rounded-md border bg-muted/40 px-2 py-0.5 text-xs transition-colors hover:bg-muted"
                onClick={() => {
                  setQuery(item)
                  void handleStartRun(item)
                }}
              >
                {item}
              </button>
            ))}
          </div>

          <Separator className="my-2" />

          {/* 工作流 ID 信息条与快照检视 */}
          <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
            {activeRun ? (
              <div className="flex items-center gap-2 font-mono">
                <span className="text-foreground/70">Workflow ID:</span>
                <span className="rounded bg-muted px-1.5 py-0.5 font-semibold text-foreground">
                  {activeRun.run_id}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-1.5 text-xs"
                  onClick={handleCopyRunId}
                  title="复制工作流 ID"
                >
                  <Copy className="h-3 w-3" />
                  {copied ? '已复制' : '复制'}
                </Button>
              </div>
            ) : (
              <span>暂无活跃工作流</span>
            )}

            {/* 凭 ID 恢复历史 */}
            <div className="flex items-center gap-2">
              <History className="h-3.5 w-3.5" />
              <Input
                value={customRunId}
                onChange={(e) => setCustomRunId(e.target.value)}
                placeholder="调取已有 Workflow ID..."
                className="h-7 w-48 font-mono text-xs"
                disabled={isBusy}
                data-testid="agent-history-input"
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 px-2.5 text-xs"
                onClick={handleFetchHistory}
                disabled={isBusy || !customRunId.trim()}
                data-testid="agent-fetch-history-button"
              >
                {isFetchingRun ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : '恢复快照'}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* HITL 人机协同待审批卡片 */}
      {activeRun?.status === 'interrupted' && activeRun.pending_approval && (
        <AgentApprovalCard
          approval={activeRun.pending_approval}
          runId={activeRun.run_id}
          onApproved={() => void handleResume(true)}
          onDeclined={() => void handleResume(false)}
          loading={resuming}
        />
      )}

      {/* 最终结果与回答 */}
      {activeRun?.final_answer && (
        <Card className="border-border/90 shadow-xs" data-testid="agent-answer-card">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <CheckCircle2 className="h-4 w-4 text-emerald-500" />
              执行结果 / 智能体回答
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {activeRun.stop_reason && (
              <AgentStopReasonAlert reason={activeRun.stop_reason} />
            )}

            <div className="rounded-lg bg-muted/30 p-4 text-sm leading-relaxed text-foreground whitespace-pre-wrap">
              {activeRun.final_answer}
            </div>

            {/* 命中笔记摘要 */}
            {activeRun.selected_note_ids && activeRun.selected_note_ids.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 pt-1 text-xs text-muted-foreground">
                <span className="font-medium text-foreground/80">关联笔记:</span>
                {activeRun.selected_note_ids.map((noteId) => (
                  <Link
                    key={noteId}
                    to={noteHref(noteId, null)}
                    className="inline-flex items-center gap-1 rounded bg-primary/10 px-2 py-0.5 font-mono text-primary transition-colors hover:bg-primary/20"
                  >
                    <FileCheck2 className="h-3 w-3" />
                    {noteId}
                  </Link>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* 工具调用时间线 */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold tracking-tight">工具调用时间线</h2>
          {activeRun && (
            <span className="text-xs text-muted-foreground">
              共记录 {activeRun.timeline?.length ?? 0} 次工具操作
            </span>
          )}
        </div>

        <ToolTimeline timeline={activeRun?.timeline ?? []} />
      </div>
    </div>
  )
}

export default AgentPage
