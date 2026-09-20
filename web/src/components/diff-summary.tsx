import { ArrowRight, FileText, Plus, Minus, Trash2 } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import type { DiffSummaryItem } from '@/lib/api-types'
import { computeDiffTotals, describeOperation } from '@/lib/diff'
import { cn } from '@/lib/utils'

export type DiffSummaryProps = {
  /** 变更计划中的文件统计摘要 */
  summary: readonly DiffSummaryItem[]
  /** 当前聚焦查看的文件路径 */
  selectedPath?: string | null
  /** 点击某个文件项时的回调 */
  onSelectPath?: (path: string) => void
  /** 容器自定义类名 */
  className?: string
}

/**
 * 变更计划的汇总看板与文件明细列表。
 *
 * 用于在批量事务执行前快速概览变更规模（受影响文件数、增减行数），
 * 并作为按需查看各文件 Diff 的导航入口。
 */
export function DiffSummary({
  summary,
  selectedPath,
  onSelectPath,
  className,
}: DiffSummaryProps) {
  const totals = computeDiffTotals(summary)

  if (summary.length === 0) {
    return (
      <div
        className={cn(
          'flex flex-col items-center justify-center p-6 text-sm text-muted-foreground border border-dashed rounded-lg',
          className,
        )}
      >
        <FileText className="size-8 mb-2 opacity-40" />
        <p>无任何待变更文件</p>
      </div>
    )
  }

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      {/* 顶部总体指标栏 */}
      <div className="flex flex-wrap items-center justify-between gap-2 p-3 bg-muted/40 rounded-lg border text-xs">
        <div className="flex items-center gap-1.5 font-medium">
          <FileText className="size-4 text-muted-foreground" />
          <span>共计 {totals.fileCount} 个文件变更</span>
        </div>

        <div className="flex items-center gap-2">
          {totals.addedLines > 0 && (
            <span className="inline-flex items-center gap-0.5 font-semibold text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded">
              <Plus className="size-3" />
              {totals.addedLines} 行
            </span>
          )}
          {totals.removedLines > 0 && (
            <span className="inline-flex items-center gap-0.5 font-semibold text-rose-600 dark:text-rose-400 bg-rose-500/10 px-2 py-0.5 rounded">
              <Minus className="size-3" />
              {totals.removedLines} 行
            </span>
          )}
          {totals.addedLines === 0 && totals.removedLines === 0 && (
            <span className="text-muted-foreground">行数无变动</span>
          )}
        </div>
      </div>

      {/* 各文件明细清单 */}
      <div className="flex flex-col gap-1.5" role="list">
        {summary.map((item) => {
          const meta = describeOperation(item.operation)
          const isSelected = selectedPath === item.path
          const isInteractive = Boolean(onSelectPath)

          return (
            <div
              key={item.path}
              role={isInteractive ? 'button' : 'listitem'}
              tabIndex={isInteractive ? 0 : undefined}
              onClick={() => onSelectPath?.(item.path)}
              onKeyDown={(e) => {
                if (isInteractive && (e.key === 'Enter' || e.key === ' ')) {
                  e.preventDefault()
                  onSelectPath?.(item.path)
                }
              }}
              className={cn(
                'flex flex-col gap-1.5 p-2.5 rounded-lg border text-xs transition-colors',
                isSelected
                  ? 'border-primary bg-primary/5 ring-1 ring-primary'
                  : 'bg-card hover:bg-muted/50 border-border',
                isInteractive && 'cursor-pointer select-none',
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <Badge variant={meta.variant} className="text-[10px] px-1.5 py-0">
                    {meta.label}
                  </Badge>

                  {/* 路径展示：移动操作包含目标位置 */}
                  {item.operation === 'move' && item.destination ? (
                    <div className="flex items-center gap-1.5 min-w-0 font-mono text-[11px]">
                      <span className="truncate text-muted-foreground" title={item.path}>
                        {item.path}
                      </span>
                      <ArrowRight className="size-3 shrink-0 text-muted-foreground" />
                      <span className="truncate font-medium text-foreground" title={item.destination}>
                        {item.destination}
                      </span>
                    </div>
                  ) : item.operation === 'trash' ? (
                    <div className="flex items-center gap-1.5 min-w-0 font-mono text-[11px]">
                      <Trash2 className="size-3 shrink-0 text-rose-500" />
                      <span className="truncate text-foreground" title={item.path}>
                        {item.path}
                      </span>
                    </div>
                  ) : (
                    <span className="truncate font-mono text-[11px] text-foreground" title={item.path}>
                      {item.path}
                    </span>
                  )}
                </div>

                {/* 行数增减指示 */}
                <div className="flex items-center gap-1.5 shrink-0 font-mono text-[11px]">
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
                  {item.added_lines === 0 && item.removed_lines === 0 && (
                    <span className="text-muted-foreground text-[10px]">-</span>
                  )}
                </div>
              </div>

              {/* 废纸篓操作补充说明路径 */}
              {item.operation === 'trash' && item.destination && (
                <div className="text-[10px] text-muted-foreground font-mono bg-muted/60 px-2 py-0.5 rounded truncate">
                  废纸篓目标: {item.destination}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
