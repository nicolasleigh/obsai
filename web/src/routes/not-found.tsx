import { Link, useLocation } from 'react-router'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { DEFAULT_PATH, SIDEBAR_GROUPS } from '@/lib/navigation'

/**
 * 404。
 *
 * 本地应用里出现 404 几乎只有一种原因：手改地址栏（或用了过期的书签）。因此这里
 * 直接把现有的入口列出来，而不是只说一句"页面不存在"让人自己回去点侧栏。
 */
function NotFoundPage() {
  const { pathname } = useLocation()
  const items = SIDEBAR_GROUPS.flatMap((group) => group.items)

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">没有这个页面</h1>
        <p className="text-muted-foreground text-sm">
          <code className="text-xs">{pathname}</code> 不对应任何界面。
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">可用的入口</CardTitle>
          <CardDescription>地址栏里的路径区分大小写。</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-2 sm:grid-cols-2">
          {items.map((item) => {
            const Icon = item.icon
            return (
              <Link
                key={item.path}
                to={item.path}
                className="hover:bg-accent hover:text-accent-foreground flex items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors"
              >
                <Icon className="text-muted-foreground size-4 shrink-0" />
                <span>{item.label}</span>
                <code className="text-muted-foreground ml-auto text-xs">{item.path}</code>
              </Link>
            )
          })}
        </CardContent>
      </Card>

      <div>
        <Button asChild>
          <Link to={DEFAULT_PATH}>回到概览</Link>
        </Button>
      </div>
    </div>
  )
}

export { NotFoundPage }
