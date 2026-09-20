import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  FileCheck2,
  Loader2,
  ShieldCheck,
  XCircle,
} from 'lucide-react'
import { useMemo, useState } from 'react'

import { DiffSummary } from '@/components/diff-summary'
import { DiffViewer } from '@/components/diff-viewer'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { api } from '@/lib/api'
import type { ChangeOutcome, ChangePlanView } from '@/lib/api-types'
import { groupDiffByFile } from '@/lib/diff'
import { present, type ErrorPresentation } from '@/lib/errors'

export type ChangeApprovalSheetProps = {
  /** 当前待审批的写入计划，为 null 时抽屉关闭或保持空态 */
  plan: ChangePlanView | null
  /** 抽屉显隐开关 */
  open: boolean
  /** 抽屉显隐变化回调 */
  onOpenChange: (open: boolean) => void
  /** 批准执行成功后的回调，传递后端回执 */
  onApproved: (outcome: ChangeOutcome) => void
  /** 拒绝作废计划后的回调 */
  onDeclined?: (outcome: ChangeOutcome) => void
}

/**
 * 批量写入事务审批流抽屉。
 *
 * 核心安全机制：
 * 1. 凭据确认：仅回传 plan.revision 与 plan.nonce，绝不在前端组装或修改文件正文传回；
 * 2. 二次确认防误触：唤起 AlertDialog，且焦点强制默认落在【放弃 / 拒绝】上；
 * 3. 子集文件选择：支持通过复选框只批准计划中的部分文件；
 * 4. 拒绝即安全作废：拒绝操作安全失效该计划凭据，Vault 保持绝对字节级不变。
 */
export function ChangeApprovalSheet({
  plan,
  open,
  onOpenChange,
  onApproved,
  onDeclined,
}: ChangeApprovalSheetProps) {
  // 选中的文件路径集合（默认全选）
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(() => new Set())
  // 当前在 Diff 预览区聚焦查看的文件
  const [focusedPath, setFocusedPath] = useState<string | null>(null)
  // 二次确认弹窗开关
  const [confirmOpen, setConfirmOpen] = useState(false)
  // 执行中 loading 状态
  const [submitting, setSubmitting] = useState(false)
  // 错误提示
  const [error, setError] = useState<ErrorPresentation | null>(null)

  // 当外部传入新 plan 时，重置选中状态与聚焦路径
  const effectivePaths = useMemo(() => {
    return plan?.affected_paths ?? []
  }, [plan])

  // 按文件切分 Diff
  const fileDiffs = useMemo(() => {
    if (!plan) return new Map()
    const chunks = groupDiffByFile(plan.diff, plan.affected_paths)
    return new Map(chunks.map((c) => [c.path, c.lines]))
  }, [plan])

  // 初始化选择全部文件
  const activeSelected = useMemo(() => {
    if (selectedPaths.size > 0) return selectedPaths
    return new Set(effectivePaths)
  }, [selectedPaths, effectivePaths])

  const activePath = focusedPath ?? effectivePaths[0] ?? null
  const activeDiffLines = activePath ? (fileDiffs.get(activePath) ?? []) : []
  const activeSummaryItem = plan?.diff_summary.find((s) => s.path === activePath)

  // 切换单个文件的勾选
  const handleTogglePath = (path: string) => {
    setSelectedPaths((prev) => {
      const next = new Set(prev.size === 0 ? effectivePaths : prev)
      if (next.has(path)) {
        next.delete(path)
      } else {
        next.add(path)
      }
      return next
    })
  }

  // 全选 / 取消全选
  const handleToggleAll = () => {
    if (activeSelected.size === effectivePaths.length) {
      setSelectedPaths(new Set())
    } else {
      setSelectedPaths(new Set(effectivePaths))
    }
  }

  // 拒绝执行并使计划作废
  const handleDecline = async () => {
    if (!plan) return
    setSubmitting(true)
    setError(null)
    try {
      const outcome = await api.approveChangePlan(plan.plan_id, {
        revision: plan.revision,
        nonce: plan.nonce,
        approved: false,
      })
      onOpenChange(false)
      onDeclined?.(outcome)
    } catch (err) {
      setError(present(err))
    } finally {
      setSubmitting(false)
    }
  }

  // 确认执行写入事务
  const handleConfirmExecute = async () => {
    if (!plan) return
    setConfirmOpen(false)
    setSubmitting(true)
    setError(null)

    try {
      const outcome = await api.approveChangePlan(plan.plan_id, {
        revision: plan.revision,
        nonce: plan.nonce,
        approved: true,
      })
      onOpenChange(false)
      onApproved(outcome)
    } catch (err) {
      setError(present(err))
    } finally {
      setSubmitting(false)
    }
  }

  if (!plan) {
    return null
  }

  return (
    <>
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent
          side="right"
          className="flex flex-col w-full sm:max-w-2xl p-0 gap-0 h-full border-l bg-background"
        >
          {/* 顶部标题区 */}
          <SheetHeader className="p-4 border-b bg-muted/20">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <FileCheck2 className="size-5 text-primary" />
                <SheetTitle className="text-base font-semibold">写入计划批量审阅</SheetTitle>
              </div>

              <div className="flex items-center gap-1.5 text-xs">
                <Badge variant="outline" className="font-mono text-[10px]">
                  Rev.{plan.revision}
                </Badge>
                <span className="flex items-center gap-1 text-muted-foreground text-[11px]">
                  <Clock className="size-3" />
                  一次性凭据
                </span>
              </div>
            </div>

            <SheetDescription className="text-xs text-muted-foreground flex items-center gap-1.5 mt-1">
              <ShieldCheck className="size-3.5 text-emerald-600 dark:text-emerald-400" />
              <span>
                两阶段防重放机制：仅回传计划版本与 Nonce 凭证，不接受随意文件覆盖。
              </span>
            </SheetDescription>
          </SheetHeader>

          {/* 错误告警区 */}
          {error && (
            <div className="p-3 bg-rose-500/10 border-b border-rose-500/20 text-xs text-rose-800 dark:text-rose-200 flex items-start gap-2">
              <AlertTriangle className="size-4 text-rose-600 shrink-0 mt-0.5" />
              <div className="flex flex-col gap-0.5 min-w-0">
                <span className="font-semibold">{error.title}</span>
                {error.hint && <span className="text-[11px] opacity-90">{error.hint}</span>}
              </div>
            </div>
          )}

          {/* 中部可滚动主体 */}
          <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-4">
            {/* 总体摘要 */}
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between text-xs font-medium text-foreground">
                <span>待变更文件列表</span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 text-[11px] px-2 text-muted-foreground"
                  onClick={handleToggleAll}
                >
                  {activeSelected.size === effectivePaths.length ? '取消全选' : '全选'}
                </Button>
              </div>

              {/* 文件筛选清单 */}
              <div className="flex flex-col gap-1.5 border rounded-lg p-2 bg-card">
                {plan.diff_summary.map((item) => {
                  const isChecked = activeSelected.has(item.path)
                  const isFocused = activePath === item.path

                  return (
                    <div
                      key={item.path}
                      onClick={() => setFocusedPath(item.path)}
                      className={`flex items-center justify-between gap-2 p-1.5 rounded text-xs transition-colors cursor-pointer ${
                        isFocused ? 'bg-primary/10 border border-primary/30' : 'hover:bg-muted/50'
                      }`}
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <Checkbox
                          checked={isChecked}
                          onCheckedChange={() => handleTogglePath(item.path)}
                          onClick={(e) => e.stopPropagation()}
                        />
                        <span className="truncate font-mono text-[11px]" title={item.path}>
                          {item.path}
                        </span>
                      </div>

                      <div className="flex items-center gap-1.5 shrink-0 text-[11px] font-mono">
                        {item.added_lines > 0 && (
                          <span className="text-emerald-600 dark:text-emerald-400">
                            +{item.added_lines}
                          </span>
                        )}
                        {item.removed_lines > 0 && (
                          <span className="text-rose-600 dark:text-rose-400">
                            -{item.removed_lines}
                          </span>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* 统计看盘 */}
            <DiffSummary
              summary={plan.diff_summary}
              selectedPath={activePath}
              onSelectPath={setFocusedPath}
            />

            {/* 聚焦文件的 Diff 预览 */}
            <div className="flex flex-col gap-2 mt-2">
              <div className="flex items-center justify-between text-xs">
                <span className="font-semibold text-foreground">差异明细预览</span>
                {activePath && (
                  <span className="text-muted-foreground font-mono text-[11px] truncate max-w-xs">
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

          {/* 底部操作工具栏 */}
          <SheetFooter className="p-3 border-t bg-muted/20 flex-row items-center justify-between gap-2 mt-0">
            <Button
              variant="outline"
              size="sm"
              disabled={submitting}
              onClick={handleDecline}
              className="text-xs text-rose-600 hover:text-rose-700 hover:bg-rose-50 dark:hover:bg-rose-950/30"
            >
              <XCircle className="size-3.5 mr-1" />
              拒绝并作废
            </Button>

            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                size="sm"
                className="text-xs"
                disabled={submitting}
                onClick={() => onOpenChange(false)}
              >
                稍后决定
              </Button>
              <Button
                variant="default"
                size="sm"
                disabled={submitting || activeSelected.size === 0}
                onClick={() => setConfirmOpen(true)}
                className="text-xs gap-1.5"
              >
                {submitting ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <CheckCircle2 className="size-3.5" />
                )}
                批准写入 ({activeSelected.size} 文件)
              </Button>
            </div>
          </SheetFooter>
        </SheetContent>
      </Sheet>

      {/* 最终确认弹窗：关键安全约束——默认焦点强制在"拒绝/放弃"上 */}
      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <ShieldCheck className="size-5 text-emerald-600" />
              确认将变更提交至 Vault？
            </AlertDialogTitle>
            <AlertDialogDescription className="text-xs space-y-2">
              <p>
                即将对选中的 <strong>{activeSelected.size}</strong> 个文件执行原子写入事务。
                提交将由 <code>TransactionService</code> 在跨进程排他锁保护下执行。
              </p>
              <p className="text-muted-foreground text-[11px] bg-muted/60 p-2 rounded">
                安全保障：每次写入均建立事务快照日志（.obsai-transactions/），若发生异常或意外冲突将自动安全回滚。
              </p>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            {/* 默认聚焦在取消按钮，防止键盘连续敲击空格/回车造成无意误写入 */}
            <AlertDialogCancel autoFocus className="text-xs">
              放弃 / 检查更多
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmExecute}
              className="text-xs bg-primary hover:bg-primary/90"
            >
              确认原子写入
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
