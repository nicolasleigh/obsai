import {
  AlertTriangle,
  CheckCircle2,
  FileCheck2,
  Loader2,
  ShieldAlert,
  ShieldCheck,
  XCircle,
} from 'lucide-react'
import { useMemo, useState } from 'react'

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
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  formatApprovalKind,
  parseUnifiedDiff,
  summarizeUnifiedDiff,
  type AgentDiffLine,
} from '@/lib/agent'
import type { AgentApprovalView } from '@/lib/api-types'
import { cn } from '@/lib/utils'

export type AgentApprovalCardProps = {
  /** 待审批的写入检查点信息 */
  approval: AgentApprovalView
  /** 当前工作流 run_id */
  runId: string
  /** 用户点击批准并确认执行后的回调 */
  onApproved: () => Promise<void> | void
  /** 用户点击拒绝作废变更后的回调 */
  onDeclined: () => Promise<void> | void
  /** 执行中加载状态 */
  loading?: boolean
  /** 自定义外层样式类名 */
  className?: string
}

/**
 * 人机协同（HITL）写入审批与差异审阅卡片组件。
 *
 * 核心安全机制：
 * 1. 批准前必须重新展示预览：二次确认弹窗中必须再次完整呈现待变更 diff，绝不使用过期的历史快照；
 * 2. 二次确认防误触：唤起 AlertDialog 弹窗，且默认焦点强制锁定在【放弃/取消】（<AlertDialogCancel autoFocus>）；
 * 3. 拒绝即绝对安全：拒绝操作（approved=false）会使工作流优雅终止，Vault 文件目录树保持字节级 100% 不变。
 */
export function AgentApprovalCard({
  approval,
  runId,
  onApproved,
  onDeclined,
  loading = false,
  className,
}: AgentApprovalCardProps) {
  const [confirmOpen, setConfirmOpen] = useState(false)

  // 解析并汇总 Diff 数据
  const diffLines = useMemo(() => {
    return parseUnifiedDiff(approval.preview || '')
  }, [approval.preview])

  const diffStats = useMemo(() => {
    return summarizeUnifiedDiff(approval.preview || '')
  }, [approval.preview])

  const handleConfirmApprove = () => {
    setConfirmOpen(false)
    void onApproved()
  }

  const handleDecline = () => {
    void onDeclined()
  }

  return (
    <>
      <Card
        className={cn(
          'border-amber-500/40 bg-amber-500/5 shadow-xs transition-colors',
          className,
        )}
        data-testid="agent-approval-card"
      >
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2 text-base text-amber-700 dark:text-amber-400 font-semibold">
              <ShieldAlert className="h-5 w-5 shrink-0" />
              人机协同待审批 (Human-in-the-Loop Approval Required)
            </CardTitle>

            <div className="flex items-center gap-1.5 text-xs">
              <Badge variant="outline" className="border-amber-500/40 font-mono text-[11px] text-amber-800 dark:text-amber-300">
                {formatApprovalKind(approval.kind)}
              </Badge>
              {approval.plan_ref && (
                <Badge variant="secondary" className="font-mono text-[10px]">
                  Ref: {approval.plan_ref}
                </Badge>
              )}
            </div>
          </div>

          <CardDescription className="text-xs text-amber-600/90 dark:text-amber-400/80 leading-relaxed">
            智能体提议对 Vault 执行写操作，已在安全检查点中断。批准前请仔细审阅下方拟定变更预览。
            拒绝操作将安全终止写操作，不会对 Vault 产生任何修改。
          </CardDescription>
        </CardHeader>

        <CardContent className="space-y-4">
          {/* Diff 统计摘要条 */}
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-amber-500/10 px-3 py-1.5 text-xs text-amber-900 dark:text-amber-200">
            <div className="flex items-center gap-3">
              <span className="font-medium">变更统计:</span>
              <span className="font-mono text-emerald-600 dark:text-emerald-400">
                +{diffStats.additions} 行
              </span>
              <span className="font-mono text-rose-600 dark:text-rose-400">
                -{diffStats.deletions} 行
              </span>
              {diffStats.hunks > 0 && (
                <span className="font-mono text-sky-600 dark:text-sky-400">
                  {diffStats.hunks} 个差异区块
                </span>
              )}
            </div>

            <span className="font-mono text-[11px] text-muted-foreground">
              Workflow ID: {runId}
            </span>
          </div>

          {/* 统一 Diff 语法高亮预览区 */}
          <div
            className="overflow-hidden rounded-lg border border-border/80 bg-background/95 font-mono text-xs shadow-inner"
            data-testid="agent-approval-diff"
          >
            <div className="flex items-center justify-between border-b bg-muted/40 px-3 py-1.5 text-[11px] text-muted-foreground uppercase tracking-wide">
              <span className="flex items-center gap-1.5 font-medium">
                <FileCheck2 className="h-3.5 w-3.5 text-primary" />
                拟定变更差异预览 (Proposed Diff Preview)
              </span>
              <span>{diffLines.length} 行</span>
            </div>

            {diffLines.length === 0 ? (
              <div className="p-6 text-center text-xs text-muted-foreground">
                （无差异预览内容）
              </div>
            ) : (
              <div className="max-h-72 overflow-auto divide-y divide-border/20 text-[12px] leading-relaxed">
                {diffLines.map((line: AgentDiffLine) => {
                  let lineClass = 'text-foreground/90'
                  if (line.type === 'add') {
                    lineClass =
                      'bg-emerald-500/10 text-emerald-800 dark:text-emerald-300 font-medium'
                  } else if (line.type === 'delete') {
                    lineClass =
                      'bg-rose-500/10 text-rose-800 dark:text-rose-300 font-medium'
                  } else if (line.type === 'hunk') {
                    lineClass =
                      'bg-sky-500/10 text-sky-800 dark:text-sky-300 font-semibold'
                  } else if (line.type === 'header') {
                    lineClass = 'bg-muted/30 text-muted-foreground font-semibold'
                  }

                  return (
                    <div
                      key={line.lineNum}
                      className={cn('flex items-stretch hover:bg-muted/30', lineClass)}
                    >
                      <span className="w-10 shrink-0 select-none border-r border-border/40 pr-2 text-right font-mono text-[11px] text-muted-foreground/60">
                        {line.lineNum}
                      </span>
                      <pre className="flex-1 overflow-x-auto px-3 py-0.5 whitespace-pre font-mono">
                        {line.text}
                      </pre>
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          {/* 操作按钮区 */}
          <div className="flex flex-wrap items-center justify-end gap-3 pt-1">
            <Button
              variant="outline"
              size="sm"
              onClick={handleDecline}
              disabled={loading}
              className="border-rose-500/30 text-rose-600 hover:bg-rose-500/10 hover:text-rose-700 text-xs gap-1.5"
              data-testid="agent-decline-button"
            >
              {loading ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <XCircle className="h-3.5 w-3.5" />
              )}
              拒绝并取消变更
            </Button>

            <Button
              variant="default"
              size="sm"
              onClick={() => setConfirmOpen(true)}
              disabled={loading}
              className="bg-emerald-600 text-white hover:bg-emerald-700 text-xs gap-1.5"
              data-testid="agent-approve-button"
            >
              {loading ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <CheckCircle2 className="h-3.5 w-3.5" />
              )}
              审阅并批准写入...
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* 核心安全防误触二次确认弹窗：批准前必须重新展示预览且焦点强制锁定在取消按钮上 */}
      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent className="sm:max-w-xl">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2 text-base">
              <ShieldCheck className="h-5 w-5 text-emerald-600 shrink-0" />
              确认将变更提交至 Vault？
            </AlertDialogTitle>
            <AlertDialogDescription className="text-xs space-y-3 pt-1 text-muted-foreground">
              <p>
                即将对 Vault 应用以下写操作（Workflow ID: <code className="font-mono font-semibold text-foreground">{runId}</code>）。
                写入操作将在跨进程排他锁与事务日志保护下执行。
              </p>

              {/* 核心安全红线：批准前强制重新展示实时预览，杜绝用户在不看 diff 的情况下盲批 */}
              <div className="rounded border bg-muted/30 p-2.5 font-mono text-[11px] text-foreground">
                <div className="mb-1 text-[10px] font-semibold uppercase text-muted-foreground flex items-center justify-between">
                  <span>实时待变更预览 (Live Proposed Diff)</span>
                  <span className="text-emerald-600 dark:text-emerald-400">
                    +{diffStats.additions} / -{diffStats.deletions}
                  </span>
                </div>
                <div className="max-h-40 overflow-y-auto whitespace-pre-wrap leading-tight text-foreground/90 divide-y divide-border/20">
                  {approval.preview || '（无差异内容）'}
                </div>
              </div>

              <div className="flex items-start gap-2 rounded bg-amber-500/10 p-2 text-amber-800 dark:text-amber-300 text-[11px]">
                <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600 mt-0.5" />
                <span>
                  安全提示：若发生异常或意外冲突，系统将建立事务快照，支持在“事务恢复”页安全回滚。
                </span>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>

          <AlertDialogFooter className="pt-2">
            {/* 关键安全约束：默认焦点锁定在取消按钮，防止键盘敲击回车或空格误触批准写入 */}
            <AlertDialogCancel
              autoFocus
              className="text-xs"
              data-testid="agent-confirm-cancel-button"
            >
              放弃 / 重新检查
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmApprove}
              className="text-xs bg-emerald-600 text-white hover:bg-emerald-700"
              data-testid="agent-confirm-approve-button"
            >
              确认原子写入
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
