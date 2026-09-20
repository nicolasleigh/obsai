import { useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  CheckSquare,
  FileCheck,
  FolderSync,
  Inbox,
  Link2,
  Loader2,
  RefreshCw,
  Sparkles,
  Square,
  Tag,
  XCircle,
} from 'lucide-react'
import { useMemo, useState } from 'react'

import { ChangeApprovalSheet } from '@/components/change-approval-sheet'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { ORGANIZE_PROPOSALS_QUERY_KEY, useOrganizeProposals } from '@/hooks/use-organize'
import { api } from '@/lib/api'
import type { ChangeOutcome, ChangePlanView, OrganizePreview } from '@/lib/api-types'
import { present, type ErrorPresentation } from '@/lib/errors'
import { cn } from '@/lib/utils'

export function OrganizePage() {
  const queryClient = useQueryClient()
  const { data: preview, isLoading, error: queryError, refetch } = useOrganizeProposals()

  // 用户选择的提案序号集合
  const [userSelected, setUserSelected] = useState<Set<number> | null>(null)
  const [prevPreview, setPrevPreview] = useState<OrganizePreview | undefined>(undefined)

  // 当服务端返回新的提案列表时，重置并应用服务端的默认选择
  if (preview !== prevPreview) {
    setPrevPreview(preview)
    setUserSelected(preview ? new Set(preview.default_numbers) : new Set())
  }

  const selectedNumbers = userSelected ?? new Set<number>()

  // 规划与执行状态
  const [planning, setPlanning] = useState(false)
  const [customError, setCustomError] = useState<ErrorPresentation | null>(null)
  const [currentPlan, setCurrentPlan] = useState<ChangePlanView | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [outcome, setOutcome] = useState<ChangeOutcome | null>(null)

  const error = customError ?? (queryError ? present(queryError) : null)

  const proposals = useMemo(() => preview?.proposals ?? [], [preview])
  const actionableProposals = useMemo(
    () => proposals.filter((p) => p.actionable),
    [proposals]
  )
  const highConfidenceProposals = useMemo(
    () => proposals.filter((p) => p.actionable && p.confidence >= 0.7),
    [proposals]
  )
  const conflictProposals = useMemo(
    () => proposals.filter((p) => Boolean(p.issue)),
    [proposals]
  )

  // 切换单项勾选
  const handleToggle = (number: number, actionable: boolean) => {
    if (!actionable) return
    setUserSelected((prev) => {
      const next = new Set(prev ?? preview?.default_numbers ?? [])
      if (next.has(number)) {
        next.delete(number)
      } else {
        next.add(number)
      }
      return next
    })
  }

  // 全选推荐项（默认高置信度且可操作）
  const handleSelectRecommended = () => {
    if (!preview) return
    setUserSelected(new Set(preview.default_numbers))
  }

  // 全选所有可操作项
  const handleSelectAllActionable = () => {
    setUserSelected(new Set(actionableProposals.map((p) => p.number)))
  }

  // 清空选择
  const handleClearSelection = () => {
    setUserSelected(new Set())
  }

  // 发起选定提案的变更计划生成
  const handleCreatePlan = async () => {
    if (selectedNumbers.size === 0) return
    setPlanning(true)
    setCustomError(null)
    setOutcome(null)

    try {
      const sortedNumbers = Array.from(selectedNumbers).sort((a, b) => a - b)
      const plan = await api.organizePlan({ numbers: sortedNumbers })
      setCurrentPlan(plan)
      setSheetOpen(true)
    } catch (err) {
      setCustomError(present(err))
    } finally {
      setPlanning(false)
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 pb-20">
      {/* 头部标题与操作 */}
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Inbox className="size-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-semibold tracking-tight">Inbox 智能整理</h1>
              {preview?.inbox && (
                <Badge variant="outline" className="font-mono text-xs">
                  {preview.inbox}/
                </Badge>
              )}
            </div>
            <p className="text-xs text-muted-foreground mt-0.5">
              扫描待处理笔记，基于全文与标签聚类推荐归类路径、补充关联双链与标签。
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={isLoading || planning}
            onClick={() => void refetch()}
            className="text-xs gap-1.5"
          >
            {isLoading ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
            重新扫描
          </Button>
        </div>
      </header>

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

      {/* 提交结果回执 */}
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
            {outcome.committed ? '归类写入事务已原子生效！' : '归类计划已安全撤销'}
          </AlertTitle>
          <AlertDescription className="text-xs mt-1 space-y-1">
            <p>
              计划 ID: <code className="font-mono">{outcome.plan_id}</code>
            </p>
            {outcome.transaction_id && (
              <p>
                事务日志: <code className="font-mono">{outcome.transaction_id}</code>
              </p>
            )}
            <p className="text-[11px] opacity-80">
              {outcome.committed
                ? '已选笔记已移动至目标目录并更新了相关双链与标签，Inbox 目录已自动刷新。'
                : 'Vault 文件未做任何修改。'}
            </p>
          </AlertDescription>
        </Alert>
      )}

      {/* 指标看板 */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Card className="border-border">
          <CardContent className="p-4">
            <div className="text-xs text-muted-foreground">待整理总数</div>
            <div className="text-xl font-semibold mt-1">{proposals.length}</div>
          </CardContent>
        </Card>

        <Card className="border-border">
          <CardContent className="p-4">
            <div className="text-xs text-muted-foreground">可归类提案</div>
            <div className="text-xl font-semibold mt-1 text-emerald-600 dark:text-emerald-400">
              {actionableProposals.length}
            </div>
          </CardContent>
        </Card>

        <Card className="border-border">
          <CardContent className="p-4">
            <div className="text-xs text-muted-foreground">高置信度推荐</div>
            <div className="text-xl font-semibold mt-1 text-primary">
              {highConfidenceProposals.length}
            </div>
          </CardContent>
        </Card>

        <Card className={cn('border-border', conflictProposals.length > 0 && 'border-amber-500/50 bg-amber-500/5')}>
          <CardContent className="p-4">
            <div className="text-xs text-muted-foreground">目标冲突 / 异常</div>
            <div
              className={cn(
                'text-xl font-semibold mt-1',
                conflictProposals.length > 0 ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'
              )}
            >
              {conflictProposals.length}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* 快捷批量选择条 */}
      {proposals.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 p-3 bg-muted/40 border rounded-lg text-xs">
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground">批量操作:</span>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleSelectRecommended}
              className="h-7 px-2 text-xs gap-1"
            >
              <Sparkles className="size-3 text-primary" />
              勾选推荐项 ({preview?.default_numbers.length ?? 0})
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleSelectAllActionable}
              className="h-7 px-2 text-xs gap-1"
            >
              <CheckSquare className="size-3" />
              全选所有可操作 ({actionableProposals.length})
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleClearSelection}
              className="h-7 px-2 text-xs gap-1 text-muted-foreground"
            >
              <Square className="size-3" />
              清空
            </Button>
          </div>

          <div className="text-muted-foreground">
            已勾选 <span className="font-semibold text-foreground">{selectedNumbers.size}</span> / {proposals.length} 项
          </div>
        </div>
      )}

      {/* 提案卡片列表 */}
      <div className="flex flex-col gap-3">
        {proposals.map((proposal) => {
          const isSelected = selectedNumbers.has(proposal.number)
          const isConflict = Boolean(proposal.issue)
          const isActionable = proposal.actionable
          const isHighConfidence = proposal.confidence >= 0.7

          return (
            <Card
              key={proposal.number}
              className={cn(
                'transition-colors border',
                isConflict && 'border-amber-300 dark:border-amber-800 bg-amber-500/[0.02]',
                isSelected && 'border-primary/40 bg-primary/[0.02]'
              )}
            >
              <CardContent className="p-4 flex flex-col gap-3">
                {/* 顶部：勾选、路径转换与置信度 */}
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex items-start gap-3 flex-1 min-w-[280px]">
                    <div className="pt-0.5">
                      <Checkbox
                        id={`proposal-${proposal.number}`}
                        checked={isSelected}
                        disabled={!isActionable}
                        onCheckedChange={() => handleToggle(proposal.number, isActionable)}
                      />
                    </div>

                    <div className="flex flex-col gap-1 flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-semibold text-sm text-foreground truncate max-w-sm">
                          {proposal.title}
                        </span>
                        <span className="font-mono text-[11px] text-muted-foreground">
                          #{proposal.number}
                        </span>
                      </div>

                      <div className="flex items-center gap-1.5 text-xs text-muted-foreground font-mono flex-wrap">
                        <span className="truncate">{proposal.path}</span>
                        <ArrowRight className="size-3 text-muted-foreground shrink-0" />
                        <span className={cn('truncate font-medium', proposal.destination ? 'text-foreground' : 'text-amber-600 dark:text-amber-400')}>
                          {proposal.destination ?? '（无推荐目标，保持原位）'}
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    <Badge
                      variant="secondary"
                      className={cn(
                        'text-xs font-mono',
                        isHighConfidence
                          ? 'bg-emerald-500/10 text-emerald-600 border-emerald-300 dark:border-emerald-800'
                          : 'bg-amber-500/10 text-amber-600 border-amber-300 dark:border-amber-800'
                      )}
                    >
                      {Math.round(proposal.confidence * 100)}% {isHighConfidence ? '置信度' : '低置信度'}
                    </Badge>

                    {proposal.selected_by_default && (
                      <Badge variant="outline" className="text-[10px] text-primary border-primary/30">
                        系统推荐
                      </Badge>
                    )}
                  </div>
                </div>

                {/* 理由说明 */}
                <p className="text-xs text-muted-foreground pl-7">
                  {proposal.reason}
                </p>

                {/* 冲突告警 */}
                {proposal.issue && (
                  <div className="ml-7 flex items-center gap-2 p-2 rounded bg-rose-500/10 border border-rose-200 dark:border-rose-900/50 text-rose-700 dark:text-rose-400 text-xs">
                    <AlertTriangle className="size-4 shrink-0" />
                    <span>冲突不可选中：{proposal.issue}</span>
                  </div>
                )}

                {/* 拟增标签与双链元数据 */}
                {(proposal.add_tags.length > 0 || proposal.add_links.length > 0 || proposal.affected_backlinks.length > 0) && (
                  <div className="ml-7 flex flex-wrap items-center gap-2 pt-1 border-t border-border/60 text-xs">
                    {proposal.add_tags.length > 0 && (
                      <div className="flex items-center gap-1">
                        <Tag className="size-3 text-muted-foreground" />
                        <div className="flex flex-wrap gap-1">
                          {proposal.add_tags.map((tag) => (
                            <Badge key={tag} variant="secondary" className="text-[11px] font-normal px-1.5 py-0">
                              #{tag}
                            </Badge>
                          ))}
                        </div>
                      </div>
                    )}

                    {proposal.add_links.length > 0 && (
                      <div className="flex items-center gap-1">
                        <Link2 className="size-3 text-muted-foreground" />
                        <div className="flex flex-wrap gap-1">
                          {proposal.add_links.map((link) => (
                            <code key={link} className="text-[11px] bg-muted px-1.5 py-0.5 rounded font-mono">
                              {link}
                            </code>
                          ))}
                        </div>
                      </div>
                    )}

                    {proposal.affected_backlinks.length > 0 && (
                      <div className="flex items-center gap-1 text-muted-foreground text-[11px]">
                        <FolderSync className="size-3" />
                        <span>{proposal.affected_backlinks.length} 处反向链接将自动重写保持有效</span>
                      </div>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>
          )
        })}

        {/* 空态 */}
        {proposals.length === 0 && !isLoading && !error && (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center justify-center p-12 text-center text-muted-foreground">
              <CheckCircle2 className="size-12 mb-3 opacity-30 text-emerald-600" />
              <h3 className="text-sm font-semibold text-foreground">Inbox 目录已全部整理完毕</h3>
              <p className="text-xs max-w-md mt-1.5 leading-relaxed">
                当前 Inbox 目录下没有待分类的 Markdown 笔记。将新收集的碎片笔记放入 Inbox 目录后即可在此发起智能整理与归档。
              </p>
            </CardContent>
          </Card>
        )}
      </div>

      {/* 底部悬浮操作栏 */}
      {proposals.length > 0 && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 w-full max-w-2xl px-4 z-40">
          <div className="flex items-center justify-between gap-3 p-3 bg-card/95 backdrop-blur border rounded-xl shadow-lg">
            <div className="flex items-center gap-2 pl-2 text-xs">
              <span className="text-muted-foreground">已选</span>
              <Badge variant="secondary" className="font-mono text-xs">
                {selectedNumbers.size}
              </Badge>
              <span className="text-muted-foreground">项归类提案</span>
            </div>

            <div className="flex items-center gap-2">
              <Button
                disabled={selectedNumbers.size === 0 || planning}
                onClick={handleCreatePlan}
                size="sm"
                className="text-xs gap-1.5"
              >
                {planning ? <Loader2 className="size-3.5 animate-spin" /> : <FileCheck className="size-4" />}
                生成变更计划并审批
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* 审批抽屉组件（直接复用 D-3 沉淀的两阶段安全确认） */}
      <ChangeApprovalSheet
        plan={currentPlan}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        onApproved={(res) => {
          setOutcome(res)
          setSheetOpen(false)
          void queryClient.invalidateQueries({ queryKey: [ORGANIZE_PROPOSALS_QUERY_KEY] })
        }}
        onDeclined={(res) => {
          setOutcome(res)
          setSheetOpen(false)
          void queryClient.invalidateQueries({ queryKey: [ORGANIZE_PROPOSALS_QUERY_KEY] })
        }}
      />
    </div>
  )
}
