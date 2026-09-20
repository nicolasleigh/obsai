import type { ReactNode } from 'react'

import { StatusDot } from '@/components/status-dot'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import type { StatusSummary } from '@/lib/status'

/**
 * 一张状态卡：状态点 + 标题 + 一句话结论 + 下一步 + 可选的明细。
 *
 * 抽出来是因为它有两个消费者（概览页与索引页），而两处必须长得一样：颜色只来自
 * `StatusSummary.tone`，语义只来自 `label` / `hint`。各写一份的结果是"概览页说黄的、
 * 索引页说红的"——用户没法判断哪个才对，也没人会主动去查。
 *
 * `hint` 为空串时整块不渲染，而不是留一行空白：`lib/status.ts` 用空串表示"不需要动作"，
 * 渲染出一个空的段落会让卡片高度随状态跳动。
 */
function StatusCard({
  title,
  summary,
  children,
}: {
  title: string
  summary: StatusSummary
  children?: ReactNode
}) {
  return (
    <Card className="gap-4">
      <CardHeader className="gap-1.5">
        <CardTitle className="flex items-center gap-2 text-sm font-medium">
          <StatusDot tone={summary.tone} />
          {title}
        </CardTitle>
        <CardDescription className="text-foreground">{summary.label}</CardDescription>
      </CardHeader>
      {(summary.hint !== '' || children !== undefined) && (
        <CardContent className="flex flex-col gap-3 text-xs">
          {summary.hint !== '' && (
            <p className="text-muted-foreground leading-relaxed">{summary.hint}</p>
          )}
          {children}
        </CardContent>
      )}
    </Card>
  )
}

/** 卡片里的一行「标签 / 值」。用 `<dl>` 是为了让屏幕阅读器读出配对关系。 */
function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="break-all">{children}</dd>
    </>
  )
}

function Details({ children }: { children: ReactNode }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] items-baseline gap-x-4 gap-y-1.5">{children}</dl>
  )
}

export { Detail, Details, StatusCard }
