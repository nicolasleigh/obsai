import { AlertCircle, ChevronDown, Trash2 } from 'lucide-react'
import { useState } from 'react'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import type { DiffLine } from '@/lib/api-types'
import { DEFAULT_MAX_LINES, truncateDiffLines } from '@/lib/diff'
import { cn } from '@/lib/utils'

export type DiffViewerProps = {
  /** 统一 Diff 行数组 */
  diff: readonly DiffLine[]
  /** 该文件的变更操作类型（如 trash / move / create / update） */
  operation?: string | null
  /** 关联的目标路径（如废纸篓目标或移动目标） */
  destination?: string | null
  /** 文件路径 */
  path?: string | null
  /** 单次渲染最大行数（默认 1000 行），防止超大笔记造成浏览器主线程卡死 */
  maxLines?: number
  /** 自定义样式类名 */
  className?: string
}

/**
 * 统一 Diff 语法高亮与大文件安全渲染组件。
 *
 * 核心安全与体验机制：
 * 1. 语法高亮：准确区分新增行（绿底 +）、删除行（红底 -）、Hunk 块标记（青色 @@）与警告提示；
 * 2. 废纸篓安全指引：对 trash 操作展示语义化暂存目标路径与事务回滚提示，避免全篇红行恐慌；
 * 3. 超大笔记（>1MB / >1000 行）防卡死：采用分段安全截断，提供平滑展开能力。
 */
export function DiffViewer({
  diff,
  operation,
  destination,
  path,
  maxLines = DEFAULT_MAX_LINES,
  className,
}: DiffViewerProps) {
  const [lineLimit, setLineLimit] = useState(maxLines)
  const [showTrashRawDiff, setShowTrashRawDiff] = useState(false)

  // 1. 废纸篓操作：默认优先呈现安全废纸篓卡片
  if (operation === 'trash' && !showTrashRawDiff) {
    return (
      <div className={cn('flex flex-col gap-3', className)}>
        <Alert variant="destructive" className="bg-rose-500/10 border-rose-200 dark:border-rose-900/50">
          <Trash2 className="size-4 text-rose-600 dark:text-rose-400" />
          <AlertTitle className="text-rose-900 dark:text-rose-100 font-semibold text-xs">
            文件将移至废纸篓
          </AlertTitle>
          <AlertDescription className="text-xs text-rose-800 dark:text-rose-200 mt-1.5 flex flex-col gap-1.5">
            <p>
              笔记 <code className="font-mono bg-rose-100 dark:bg-rose-950 px-1 py-0.5 rounded">{path ?? '原文件'}</code> 将被安全移至内部废纸篓目录：
            </p>
            {destination && (
              <div className="font-mono text-[11px] bg-rose-100/70 dark:bg-rose-950/70 p-1.5 rounded border border-rose-300 dark:border-rose-800 break-all">
                {destination}
              </div>
            )}
            <p className="text-[11px] text-muted-foreground">
              注：ObsAgent 采用非破坏性移动并保留完整事务快照，确认后若需撤销可在“事务恢复”页一键回滚。
            </p>
          </AlertDescription>
        </Alert>

        {diff.length > 0 && (
          <div className="flex justify-end">
            <Button
              variant="ghost"
              size="sm"
              className="text-xs text-muted-foreground hover:text-foreground"
              onClick={() => setShowTrashRawDiff(true)}
            >
              查看被删除文本内容 ({diff.length} 行)
            </Button>
          </div>
        )}
      </div>
    )
  }

  // 2. 空内容或仅元数据变动
  if (diff.length === 0) {
    return (
      <div
        className={cn(
          'flex items-center justify-center p-8 text-xs text-muted-foreground border border-dashed rounded-lg bg-card',
          className,
        )}
      >
        <span>无文本差异（内容未修改或仅路径调整）</span>
      </div>
    )
  }

  // 3. 超大笔记截断计算
  const { displayedLines, totalCount, isTruncated, remainingCount } = truncateDiffLines(
    diff,
    lineLimit,
  )

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      {/* 废纸篓模式下的折叠返回入口 */}
      {operation === 'trash' && showTrashRawDiff && (
        <div className="flex items-center justify-between p-2 bg-muted/40 rounded border text-xs">
          <span className="text-muted-foreground">当前正在查看移至废纸篓笔记的原文本差异</span>
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-xs"
            onClick={() => setShowTrashRawDiff(false)}
          >
            返回废纸篓路径视图
          </Button>
        </div>
      )}

      {/* 截断提示横幅 */}
      {isTruncated && (
        <div className="flex items-center justify-between p-2.5 bg-amber-500/10 border border-amber-500/20 text-amber-900 dark:text-amber-200 rounded text-xs">
          <div className="flex items-center gap-1.5">
            <AlertCircle className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />
            <span>
              Diff 较长（共 {totalCount} 行），已优先渲染前 {displayedLines.length} 行以保证页面响应流畅。
            </span>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => setLineLimit((prev) => prev + 1000)}
            >
              加载后续 1,000 行
            </Button>
            <Button
              variant="secondary"
              size="sm"
              className="h-7 text-xs"
              onClick={() => setLineLimit(totalCount)}
            >
              展开全部 ({remainingCount} 行)
            </Button>
          </div>
        </div>
      )}

      {/* 统一 Diff 代码排版区 */}
      <div className="border rounded-lg bg-card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full font-mono text-[11px] leading-snug border-collapse select-text">
            <tbody>
              {displayedLines.map((line, index) => {
                const lineText = line.text
                const isHeader = lineText.startsWith('---') || lineText.startsWith('+++')
                const isHunk = line.style === 'hunk' || lineText.startsWith('@@')
                const isAdded = line.style === 'added' || (!isHeader && lineText.startsWith('+'))
                const isRemoved = line.style === 'removed' || (!isHeader && lineText.startsWith('-'))
                const isNotice = line.style === 'notice'

                return (
                  <tr
                    key={index}
                    className={cn(
                      'transition-colors',
                      isAdded && 'bg-emerald-500/10 dark:bg-emerald-950/30 text-emerald-800 dark:text-emerald-200',
                      isRemoved && 'bg-rose-500/10 dark:bg-rose-950/30 text-rose-800 dark:text-rose-200',
                      isHunk && 'bg-muted/70 text-cyan-700 dark:text-cyan-300 font-semibold',
                      isHeader && 'bg-muted/40 text-muted-foreground font-semibold',
                      isNotice && 'bg-amber-500/10 text-amber-800 dark:text-amber-200',
                      !isAdded && !isRemoved && !isHunk && !isHeader && !isNotice && 'text-foreground',
                    )}
                  >
                    {/* 行号列 */}
                    <td className="w-12 py-0.5 pr-2 pl-3 text-right select-none text-muted-foreground/50 border-r border-border/40 shrink-0">
                      {index + 1}
                    </td>

                    {/* 符号指示 */}
                    <td className="w-5 py-0.5 text-center select-none font-bold shrink-0">
                      {isAdded ? '+' : isRemoved ? '-' : isHunk ? '@' : ''}
                    </td>

                    {/* 正文行 */}
                    <td className="py-0.5 px-2 whitespace-pre break-all">
                      {lineText}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {/* 底部展开更多入口 */}
        {isTruncated && (
          <div className="p-2 border-t bg-muted/20 flex justify-center">
            <Button
              variant="ghost"
              size="sm"
              className="text-xs text-muted-foreground gap-1 hover:text-foreground"
              onClick={() => setLineLimit((prev) => prev + 1000)}
            >
              <ChevronDown className="size-3.5" />
              继续展开剩余 {remainingCount} 行
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}
