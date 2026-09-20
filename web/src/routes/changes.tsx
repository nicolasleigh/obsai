import { useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  CheckCircle2,
  FileCheck,
  FilePenLine,
  Loader2,
  RefreshCw,
  Search,
  ShieldAlert,
  Sparkles,
  XCircle,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'

import { ChangeApprovalSheet } from '@/components/change-approval-sheet'
import { DiffSummary } from '@/components/diff-summary'
import { DiffViewer } from '@/components/diff-viewer'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { CHANGE_PLAN_QUERY_KEY, useChangePlan } from '@/hooks/use-change-plan'
import { api } from '@/lib/api'
import type { ChangeOutcome } from '@/lib/api-types'
import { groupDiffByFile } from '@/lib/diff'
import { present, type ErrorPresentation } from '@/lib/errors'

export function ChangesPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const urlPlanId = searchParams.get('plan_id') ?? ''

  const [inputPlanId, setInputPlanId] = useState(urlPlanId)
  const [syncedFrom, setSyncedFrom] = useState(urlPlanId)

  // 当 URL 参数由外部改变时，同步更新输入框
  if (syncedFrom !== urlPlanId) {
    setSyncedFrom(urlPlanId)
    setInputPlanId(urlPlanId)
  }

  const queryClient = useQueryClient()
  const { data: currentPlan = null, isLoading: loadingPlan, error: queryError } = useChangePlan(urlPlanId)

  const [creatingDemo, setCreatingDemo] = useState(false)
  const [customError, setCustomError] = useState<ErrorPresentation | null>(null)
  const [outcome, setOutcome] = useState<ChangeOutcome | null>(null)

  // 审批抽屉开关
  const [sheetOpen, setSheetOpen] = useState(false)
  // 当前聚焦查看的文件
  const [focusedPath, setFocusedPath] = useState<string | null>(null)

  const error = customError ?? (queryError ? present(queryError) : null)
  const loading = loadingPlan || creatingDemo

  // 按文件切分 Diff
  const fileDiffs = useMemo(() => {
    if (!currentPlan) return new Map()
    const chunks = groupDiffByFile(currentPlan.diff, currentPlan.affected_paths)
    return new Map(chunks.map((c) => [c.path, c.lines]))
  }, [currentPlan])

  const activePath = focusedPath ?? currentPlan?.affected_paths[0] ?? null
  const activeDiffLines = activePath ? (fileDiffs.get(activePath) ?? []) : []
  const activeSummaryItem = currentPlan?.diff_summary.find((s) => s.path === activePath)

  // 手动搜索 / 提交加载
  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = inputPlanId.trim()
    if (!trimmed) return
    setCustomError(null)
    setOutcome(null)
    setFocusedPath(null)
    setSearchParams({ plan_id: trimmed })
  }

  // 快速生成演示变更计划
  const handleCreateSamplePlan = async () => {
    setCreatingDemo(true)
    setCustomError(null)
    setOutcome(null)
    try {
      const plan = await api.createChangePlan({
        operations: [
          {
            kind: 'create',
            path: 'Inbox/Welcome-to-ObsAgent.md',
            new: '# Welcome to ObsAgent\n\nThis is an automated sample write proposal.\n',
          },
        ],
      })
      queryClient.setQueryData([CHANGE_PLAN_QUERY_KEY, plan.plan_id], plan)
      setFocusedPath(plan.affected_paths[0] ?? null)
      setSearchParams({ plan_id: plan.plan_id })
    } catch (err) {
      setCustomError(present(err))
    } finally {
      setCreatingDemo(false)
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
      {/* 头部标题区 */}
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <FilePenLine className="size-6 text-primary" />
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">写入管理与审批</h1>
            <p className="text-xs text-muted-foreground mt-0.5">
              Vault 写入事务两阶段审批：预演差异、Nonce 凭证确认与原子提交控制台。
            </p>
          </div>
        </div>

        <Badge variant="secondary" className="text-xs">
          阶段 D 写入协议
        </Badge>
      </header>

      {/* 计划检索与快捷动作 */}
      <Card className="border-border">
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-medium">变更计划检索</CardTitle>
          <CardDescription className="text-xs">
            输入服务端生成的写入计划 ID 进行差异审阅，或直接生成演示计划进行全流程验证。
          </CardDescription>
        </CardHeader>

        <CardContent className="flex flex-col sm:flex-row gap-2">
          <form onSubmit={handleSearch} className="flex-1 flex gap-2">
            <div className="relative flex-1">
              <Search className="size-4 absolute left-2.5 top-2.5 text-muted-foreground" />
              <Input
                placeholder="输入写入计划 ID (如 4a8b...)"
                value={inputPlanId}
                onChange={(e) => setInputPlanId(e.target.value)}
                className="pl-8 text-xs font-mono"
              />
            </div>
            <Button type="submit" size="sm" disabled={loading || !inputPlanId.trim()} className="text-xs gap-1">
              {loadingPlan ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
              查看计划
            </Button>
          </form>

          <Button
            variant="outline"
            size="sm"
            disabled={creatingDemo || loadingPlan}
            onClick={handleCreateSamplePlan}
            className="text-xs gap-1.5 shrink-0"
          >
            {creatingDemo ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5 text-primary" />}
            生成测试计划
          </Button>
        </CardContent>
      </Card>

      {/* 错误提示 */}
      {error && (
        <Alert variant="destructive" className="bg-rose-500/10 border-rose-200 dark:border-rose-900/50">
          <AlertCircle className="size-4 text-rose-600 dark:text-rose-400" />
          <AlertTitle className="text-xs font-semibold">{error.title}</AlertTitle>
          <AlertDescription className="text-xs mt-1">
            {error.hint || error.detail}
          </AlertDescription>
        </Alert>
      )}

      {/* 结果回执展示 */}
      {outcome && (
        <Alert
          variant={outcome.committed ? 'default' : 'destructive'}
          className={
            outcome.committed
              ? 'bg-emerald-500/10 border-emerald-300 dark:border-emerald-800 text-emerald-900 dark:text-emerald-100'
              : 'bg-amber-500/10 border-amber-300 dark:border-amber-800 text-amber-900 dark:text-amber-100'
          }
        >
          {outcome.committed ? (
            <CheckCircle2 className="size-4 text-emerald-600" />
          ) : (
            <XCircle className="size-4 text-amber-600" />
          )}
          <AlertTitle className="text-xs font-semibold">
            {outcome.committed ? '写入事务已成功提交！' : '变更计划已安全撤销并作废'}
          </AlertTitle>
          <AlertDescription className="text-xs mt-1 space-y-1">
            <p>
              计划 ID: <code className="font-mono">{outcome.plan_id}</code>
            </p>
            {outcome.transaction_id && (
              <p>
                事务日志 ID: <code className="font-mono">{outcome.transaction_id}</code>
              </p>
            )}
            <p className="text-[11px] opacity-80">
              {outcome.committed
                ? '修改已原子持久化至 Vault 磁盘文件，并触发了快照备份。'
                : 'Vault 处于 100% 原始字节状态未作任何修改。'}
            </p>
          </AlertDescription>
        </Alert>
      )}

      {/* 活跃计划看板 */}
      {currentPlan && (
        <div className="flex flex-col gap-4">
          {/* 计划元数据栏 */}
          <div className="flex flex-wrap items-center justify-between gap-3 p-3 bg-card border rounded-lg text-xs">
            <div className="flex flex-wrap items-center gap-3">
              <div>
                <span className="text-muted-foreground mr-1.5">计划 ID:</span>
                <span className="font-mono font-medium text-foreground">{currentPlan.plan_id}</span>
              </div>
              <Badge variant="outline" className="font-mono text-[10px]">
                Rev.{currentPlan.revision}
              </Badge>
              <div className="flex items-center gap-1 text-muted-foreground">
                <ShieldAlert className="size-3.5 text-emerald-600" />
                <span>有效期至: {new Date(currentPlan.expires_at).toLocaleTimeString()}</span>
              </div>
            </div>

            <Button
              size="sm"
              disabled={outcome !== null}
              onClick={() => setSheetOpen(true)}
              className="text-xs gap-1.5"
            >
              <FileCheck className="size-4" />
              打开审批抽屉
            </Button>
          </div>

          {/* 两列布局：左侧文件摘要列表，右侧 Diff 代码视图 */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-start">
            <div className="md:col-span-1">
              <DiffSummary
                summary={currentPlan.diff_summary}
                selectedPath={activePath}
                onSelectPath={setFocusedPath}
              />
            </div>

            <div className="md:col-span-2 flex flex-col gap-2">
              <div className="flex items-center justify-between text-xs px-1">
                <span className="font-medium text-foreground">统一 Diff 视图</span>
                {activePath && (
                  <span className="text-muted-foreground font-mono text-[11px] truncate max-w-sm">
                    {activePath}
                  </span>
                )}
              </div>

              <DiffViewer
                diff={activeDiffLines}
                path={activePath}
                operation={activeSummaryItem?.operation}
                destination={activeSummaryItem?.destination}
              />
            </div>
          </div>
        </div>
      )}

      {/* 未载入计划时的空态指引 */}
      {!currentPlan && !loading && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center p-12 text-center text-muted-foreground">
            <FilePenLine className="size-12 mb-3 opacity-30 text-primary" />
            <h3 className="text-sm font-semibold text-foreground">当前无活跃的写入计划</h3>
            <p className="text-xs max-w-md mt-1.5 leading-relaxed">
              写入计划通常由命令行执行（带 --preview）、Inbox 归类建议或 Agent 写操作发起。
              输入已有的计划 ID 或点击上方“生成测试计划”即可体验差异审阅与安全审批闭环。
            </p>
          </CardContent>
        </Card>
      )}

      {/* 审批抽屉组件 */}
      <ChangeApprovalSheet
        plan={currentPlan}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        onApproved={(res) => {
          setOutcome(res)
          setSheetOpen(false)
          if (currentPlan) {
            queryClient.removeQueries({ queryKey: [CHANGE_PLAN_QUERY_KEY, currentPlan.plan_id] })
          }
          setSearchParams({})
        }}
        onDeclined={(res) => {
          setOutcome(res)
          setSheetOpen(false)
          if (currentPlan) {
            queryClient.removeQueries({ queryKey: [CHANGE_PLAN_QUERY_KEY, currentPlan.plan_id] })
          }
          setSearchParams({})
        }}
      />
    </div>
  )
}
