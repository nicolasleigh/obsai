import { useEffect } from 'react'
import { Outlet, useLocation } from 'react-router'

import { AppBreadcrumb } from '@/components/app-breadcrumb'
import { AppSidebar } from '@/components/app-sidebar'
import { Separator } from '@/components/ui/separator'
import { SidebarInset, SidebarProvider, SidebarTrigger } from '@/components/ui/sidebar'
import { Toaster } from '@/components/ui/sonner'
import { documentTitleFor } from '@/lib/navigation'

/**
 * 应用外壳：侧栏 + 页头 + 内容区 + 全局提示。
 *
 * 它是路由树里唯一的布局路由，页面本身只渲染内容——因此"每个页面都要记得加面包屑"
 * 这种约定不需要靠人遵守。
 *
 * 全局 `Toaster` 挂在这里而不是各页面里：阶段 C 的 SSE 事件流（索引进度、任务失败）
 * 会在任何页面上冒出来，提示通道必须是全局的。
 */
function AppShell() {
  const { pathname } = useLocation()

  useEffect(() => {
    document.title = documentTitleFor(pathname)
  }, [pathname])

  return (
    <SidebarProvider>
      <a
        href="#main"
        className="bg-background focus:ring-ring sr-only rounded-md px-3 py-2 text-sm focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:ring-2"
      >
        跳到主内容
      </a>
      <AppSidebar />
      <SidebarInset>
        <header className="flex h-14 shrink-0 items-center gap-2 border-b px-4">
          <SidebarTrigger className="-ml-1" />
          <Separator orientation="vertical" className="mr-1" />
          <AppBreadcrumb />
        </header>
        <main id="main" className="flex flex-1 flex-col gap-6 p-4 md:p-6">
          <Outlet />
        </main>
      </SidebarInset>
      <Toaster position="bottom-right" richColors closeButton />
    </SidebarProvider>
  )
}

export { AppShell }
