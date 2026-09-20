import {
  AlertCircle,
  Clock,
  Database,
  FileQuestion,
  RotateCcw,
  Search,
  ShieldBan,
  XCircle,
} from 'lucide-react'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import {
  AGENT_MAX_RETRIEVAL_STEPS,
  AGENT_MAX_STEPS,
  describeStopReason,
  getRetrievalHealthTone,
  getStepHealthTone,
  type StopReasonMeta,
} from '@/lib/agent'
import { cn } from '@/lib/utils'

export type AgentLimitsBadgeProps = {
  /** 当前执行步数 */
  stepCount: number
  /** 单次执行最大步数上限（默认 15） */
  maxSteps?: number
  /** 当前向量/关键词检索次数 */
  retrievalStepCount: number
  /** 最大检索次数上限（默认 5） */
  maxRetrievalSteps?: number
  /** 停止原因原始字符串（可选） */
  stopReason?: string | null
  /** 是否展示停止原因徽章（默认 false，仅展示计数徽章） */
  showStopReasonBadge?: boolean
  /** 自定义类名 */
  className?: string
}

/**
 * 智能体有界性上限监控徽章组件。
 *
 * 核心功能：
 * 1. 监控步数预算（step_count / 15）与检索预算（retrieval / 5）；
 * 2. 动态健康色调呈现：安全（默认/浅灰）、接近阈值（琥珀色告警）、超限耗尽（玫瑰红危险）；
 * 3. 支持展示停止原因紧凑徽章与悬浮说明。
 */
export function AgentLimitsBadge({
  stepCount,
  maxSteps = AGENT_MAX_STEPS,
  retrievalStepCount,
  maxRetrievalSteps = AGENT_MAX_RETRIEVAL_STEPS,
  stopReason,
  showStopReasonBadge = false,
  className,
}: AgentLimitsBadgeProps) {
  const stepTone = getStepHealthTone(stepCount, maxSteps)
  const retrievalTone = getRetrievalHealthTone(retrievalStepCount, maxRetrievalSteps)
  const reasonMeta = describeStopReason(stopReason)

  return (
    <TooltipProvider>
      <div
        className={cn('flex flex-wrap items-center gap-2', className)}
        data-testid="agent-limits-badge"
      >
        {/* 步数上限监控徽章 */}
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge
              variant="outline"
              className={cn(
                'font-mono text-xs cursor-default transition-colors',
                stepTone === 'normal' && 'border-border/80 bg-muted/20 text-muted-foreground',
                stepTone === 'warning' &&
                  'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400 font-semibold',
                stepTone === 'danger' &&
                  'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-400 font-bold animate-pulse',
              )}
              data-testid="agent-step-limit-badge"
            >
              <Clock className="mr-1 h-3 w-3 inline-block" />
              步数: {stepCount} / {maxSteps}
            </Badge>
          </TooltipTrigger>
          <TooltipContent className="text-xs max-w-xs">
            {stepCount >= maxSteps
              ? `已达到最大步数上限（${maxSteps} 步），工作流强制终止`
              : `单次会话最大执行步数限制为 ${maxSteps} 步，防止执行死循环与过度消耗`}
          </TooltipContent>
        </Tooltip>

        {/* 检索上限监控徽章 */}
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge
              variant="outline"
              className={cn(
                'font-mono text-xs cursor-default transition-colors',
                retrievalTone === 'normal' && 'border-border/80 bg-muted/20 text-muted-foreground',
                retrievalTone === 'warning' &&
                  'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400 font-semibold',
                retrievalTone === 'danger' &&
                  'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-400 font-bold',
              )}
              data-testid="agent-retrieval-limit-badge"
            >
              <Search className="mr-1 h-3 w-3 inline-block" />
              检索: {retrievalStepCount} / {maxRetrievalSteps}
            </Badge>
          </TooltipTrigger>
          <TooltipContent className="text-xs max-w-xs">
            {retrievalStepCount >= maxRetrievalSteps
              ? `已达到检索次数上限（${maxRetrievalSteps} 次），停止过度检索`
              : `单次会话最大检索限制为 ${maxRetrievalSteps} 次，保护语义索引与系统响应`}
          </TooltipContent>
        </Tooltip>

        {/* 停止原因紧凑徽章 */}
        {showStopReasonBadge && reasonMeta && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Badge
                variant="outline"
                className={cn(
                  'text-xs font-medium cursor-default',
                  reasonMeta.tone === 'rose' &&
                    'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-400',
                  reasonMeta.tone === 'amber' &&
                    'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
                  reasonMeta.tone === 'slate' &&
                    'border-border/80 bg-muted/40 text-muted-foreground',
                )}
                data-testid="agent-stop-reason-badge"
              >
                {getStopReasonIcon(reasonMeta)}
                <span className="ml-1">{reasonMeta.title}</span>
              </Badge>
            </TooltipTrigger>
            <TooltipContent className="text-xs max-w-sm">
              {reasonMeta.description}
            </TooltipContent>
          </Tooltip>
        )}
      </div>
    </TooltipProvider>
  )
}

export type AgentStopReasonAlertProps = {
  /** 停止原因原始字符串 */
  reason: string
  /** 自定义类名 */
  className?: string
}

/**
 * 停止原因详细告警展示卡片组件。
 *
 * 针对越权写入拦截（Security Guard）、步数/检索超限、死循环熔断等场景展示结构化警示。
 */
export function AgentStopReasonAlert({ reason, className }: AgentStopReasonAlertProps) {
  const meta = describeStopReason(reason)
  if (!meta) return null

  const isSecurity = meta.category === 'security'
  const isDanger = meta.tone === 'rose'

  return (
    <Alert
      variant={isDanger ? 'destructive' : 'default'}
      className={cn(
        'py-2.5 transition-colors',
        meta.tone === 'amber' &&
          'border-amber-500/40 bg-amber-500/10 text-amber-900 dark:text-amber-200',
        meta.tone === 'rose' &&
          'border-rose-500/40 bg-rose-500/10 text-rose-900 dark:text-rose-200',
        className,
      )}
      data-testid="agent-stop-reason-alert"
    >
      <div className="flex items-start gap-2.5">
        <div className="mt-0.5 shrink-0">
          {getStopReasonIcon(meta, 'h-4 w-4')}
        </div>
        <div className="flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <AlertTitle className="text-xs font-semibold leading-none">
              {meta.title}
            </AlertTitle>
            <Badge
              variant="outline"
              className={cn(
                'text-[10px] py-0 px-1.5 h-4 font-normal',
                isSecurity && 'border-rose-500/40 text-rose-700 dark:text-rose-300',
                meta.category === 'limit' &&
                  'border-amber-500/40 text-amber-700 dark:text-amber-300',
                meta.category === 'loop' &&
                  'border-rose-500/40 text-rose-700 dark:text-rose-300',
                meta.category === 'system' &&
                  'border-border/80 text-muted-foreground',
              )}
            >
              {getCategoryLabel(meta.category)}
            </Badge>
          </div>
          <AlertDescription className="text-xs leading-relaxed text-foreground/80">
            {meta.description}
          </AlertDescription>
        </div>
      </div>
    </Alert>
  )
}

function getStopReasonIcon(meta: StopReasonMeta, sizeClass: string = 'h-3.5 w-3.5') {
  switch (meta.category) {
    case 'security':
      return <ShieldBan className={cn(sizeClass, 'text-rose-600 dark:text-rose-400')} />
    case 'limit':
      return meta.title.includes('检索') ? (
        <Database className={cn(sizeClass, 'text-amber-600 dark:text-amber-400')} />
      ) : (
        <Clock className={cn(sizeClass, 'text-amber-600 dark:text-amber-400')} />
      )
    case 'loop':
      return <RotateCcw className={cn(sizeClass, 'text-rose-600 dark:text-rose-400')} />
    case 'system':
      return meta.title.includes('证据') ? (
        <FileQuestion className={cn(sizeClass, 'text-amber-600 dark:text-amber-400')} />
      ) : (
        <XCircle className={cn(sizeClass, 'text-rose-600 dark:text-rose-400')} />
      )
    default:
      return <AlertCircle className={cn(sizeClass, 'text-muted-foreground')} />
  }
}

function getCategoryLabel(category: StopReasonMeta['category']): string {
  switch (category) {
    case 'security':
      return '安全防护'
    case 'limit':
      return '资源上限'
    case 'loop':
      return '防死循环熔断'
    case 'system':
      return '系统边界'
    default:
      return '系统状态'
  }
}
