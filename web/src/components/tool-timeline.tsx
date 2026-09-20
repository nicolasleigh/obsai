import {
  ArrowLeftRight,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  FilePen,
  FilePlus,
  FileText,
  FolderInput,
  Search,
  Sparkles,
  Trash2,
  Wrench,
} from 'lucide-react'
import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { formatToolName } from '@/lib/agent'
import type { AgentTimelineItem } from '@/lib/api-types'
import { cn } from '@/lib/utils'

interface ToolMeta {
  label: string
  icon: typeof Search
  colorClass: string
}

function resolveToolMeta(tool: string): ToolMeta {
  const label = formatToolName(tool)
  switch (tool) {
    case 'search_notes':
      return { label, icon: Search, colorClass: 'text-blue-500 bg-blue-500/10 border-blue-500/20' }
    case 'read_note':
      return { label, icon: FileText, colorClass: 'text-indigo-500 bg-indigo-500/10 border-indigo-500/20' }
    case 'get_backlinks':
      return { label, icon: ArrowLeftRight, colorClass: 'text-purple-500 bg-purple-500/10 border-purple-500/20' }
    case 'get_outgoing_links':
      return { label, icon: ExternalLink, colorClass: 'text-cyan-500 bg-cyan-500/10 border-cyan-500/20' }
    case 'create_note':
      return { label, icon: FilePlus, colorClass: 'text-amber-500 bg-amber-500/10 border-amber-500/20' }
    case 'update_note':
      return { label, icon: FilePen, colorClass: 'text-amber-500 bg-amber-500/10 border-amber-500/20' }
    case 'move_note':
      return { label, icon: FolderInput, colorClass: 'text-orange-500 bg-orange-500/10 border-orange-500/20' }
    case 'trash_note':
      return { label, icon: Trash2, colorClass: 'text-rose-500 bg-rose-500/10 border-rose-500/20' }
    case 'apply_write':
      return { label, icon: CheckCircle2, colorClass: 'text-emerald-500 bg-emerald-500/10 border-emerald-500/20' }
    case 'rag_answer':
      return { label, icon: Sparkles, colorClass: 'text-teal-500 bg-teal-500/10 border-teal-500/20' }
    default:
      return { label, icon: Wrench, colorClass: 'text-muted-foreground bg-muted border-border' }
  }
}

interface TimelineItemProps {
  item: AgentTimelineItem
  stepNumber: number
  isLast: boolean
}

function TimelineItemRow({ item, stepNumber, isLast }: TimelineItemProps) {
  const [expanded, setExpanded] = useState(false)
  const meta = resolveToolMeta(item.tool)
  const Icon = meta.icon
  const hasArgs = item.args && Object.keys(item.args).length > 0

  return (
    <div className="relative flex gap-4 pb-6 last:pb-0" data-testid={`timeline-item-${stepNumber}`}>
      {/* Vertical connecting line */}
      {!isLast && (
        <div
          className="absolute left-4 top-8 -bottom-1 w-px bg-border/80"
          aria-hidden="true"
        />
      )}

      {/* Step Icon Bubble */}
      <div
        className={cn(
          'relative z-10 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border text-xs font-semibold shadow-xs',
          meta.colorClass
        )}
        title={`步骤 ${stepNumber}: ${meta.label}`}
      >
        <Icon className="h-4 w-4" />
      </div>

      {/* Step Content */}
      <div className="flex-1 pt-0.5">
        <div className="rounded-lg border bg-card/60 p-3.5 shadow-xs transition-colors hover:bg-card/90">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Badge variant="outline" className={cn('text-xs font-medium', meta.colorClass)}>
                {meta.label}
              </Badge>
              <span className="font-mono text-xs text-muted-foreground">
                step-{stepNumber.toString().padStart(2, '0')} · {item.tool}
              </span>
            </div>

            {hasArgs && (
              <Button
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-xs text-muted-foreground hover:text-foreground"
                onClick={() => setExpanded((prev) => !prev)}
                aria-label={expanded ? '折叠入参' : '查看入参'}
              >
                {expanded ? (
                  <>
                    <ChevronDown className="mr-1 h-3.5 w-3.5" />
                    隐藏参数
                  </>
                ) : (
                  <>
                    <ChevronRight className="mr-1 h-3.5 w-3.5" />
                    入参详情
                  </>
                )}
              </Button>
            )}
          </div>

          {/* Summary */}
          <div className="mt-2 text-sm leading-relaxed text-foreground/90 whitespace-pre-wrap">
            {item.summary || '无文本摘要'}
          </div>

          {/* Expanded Args */}
          {hasArgs && expanded && (
            <div className="mt-3 overflow-hidden rounded border bg-muted/40 p-2.5">
              <div className="mb-1 text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">
                调用参数 (Arguments)
              </div>
              <pre className="overflow-x-auto font-mono text-xs text-foreground/80">
                {JSON.stringify(item.args, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export interface ToolTimelineProps {
  timeline: AgentTimelineItem[]
  className?: string
}

export function ToolTimeline({ timeline, className }: ToolTimelineProps) {
  if (!timeline || timeline.length === 0) {
    return (
      <Card className={cn('border-dashed bg-muted/20 text-center text-muted-foreground', className)}>
        <CardContent className="py-8">
          <Wrench className="mx-auto mb-2 h-8 w-8 opacity-40" />
          <p className="text-sm font-medium">暂无工具调用记录</p>
          <p className="mt-1 text-xs text-muted-foreground/80">
            当工作流执行时，检索、分析与写入规划的每一步工具活动都会呈现在此时间线中。
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className={cn('flex flex-col', className)} data-testid="tool-timeline">
      {timeline.map((item, index) => (
        <TimelineItemRow
          key={`${item.tool}-${index}`}
          item={item}
          stepNumber={index + 1}
          isLast={index === timeline.length - 1}
        />
      ))}
    </div>
  )
}
