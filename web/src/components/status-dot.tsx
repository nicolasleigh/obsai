import type { StatusTone } from '@/lib/status'
import { cn } from '@/lib/utils'

/**
 * 状态色点。
 *
 * 抽出来是因为侧栏状态灯与概览卡片必须用同一套颜色——两处各写一份 `Record<StatusTone, string>`
 * 的结果是"侧栏是黄的、卡片是红的"这种没人会主动去查的不一致。
 *
 * 颜色不承载语义：语义在文案里（`lib/status.ts`），色点只是让人扫一眼就能定位到问题。
 */
const TONE_DOT: Record<StatusTone, string> = {
  ok: 'bg-emerald-500',
  warning: 'bg-amber-500',
  danger: 'bg-destructive',
  unknown: 'bg-muted-foreground',
}

function StatusDot({ tone, className }: { tone: StatusTone; className?: string }) {
  return (
    <span
      className={cn('size-2 shrink-0 rounded-full', TONE_DOT[tone], className)}
      aria-hidden
    />
  )
}

export { StatusDot }
