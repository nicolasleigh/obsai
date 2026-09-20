import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import type { NavItem } from '@/lib/navigation'

/**
 * 尚未实现的页面占位。
 *
 * 内容不是"敬请期待"，而是把计划文档里那一步的**验收标准原文**贴出来。理由有两个：
 * 一是用户（或下一个接手的人）能知道这个页面承诺做什么；二是实现它的时候，
 * 验收标准就在屏幕上，不需要来回翻文档。
 *
 * 每个页面在自己的步骤里被替换掉，替换时同步把 `navigation.ts` 的 `status`
 * 翻成 `'ready'` 并注册到 `router.tsx` 的 `READY_PAGES`——两处不同步会在启动时抛错。
 */
function PendingPage({ item }: { item: NavItem }) {
  const Icon = item.icon

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <header className="flex flex-wrap items-center gap-3">
        <Icon className="text-muted-foreground size-5" />
        <h1 className="text-2xl font-semibold tracking-tight">{item.label}</h1>
        <Badge variant="secondary">计划步骤 {item.step}</Badge>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>这个页面还没有实现</CardTitle>
          <CardDescription>{item.summary}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4 text-sm">
          <div className="flex flex-col gap-1">
            <span className="text-muted-foreground text-xs">计划文档里的验收标准</span>
            <p>{item.criteria}</p>
          </div>
          <Separator />
          <p className="text-muted-foreground text-xs leading-relaxed">
            路由、侧栏与面包屑已在 <code>B-3</code> 就位，页面本身由{' '}
            <code>{item.step}</code> 实现。对应的后端端点若尚未开放，请求会返回归一化的
            错误信封并在此处显示中文提示，而不是空白页。
          </p>
        </CardContent>
      </Card>
    </div>
  )
}

export { PendingPage }
