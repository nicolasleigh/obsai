import type { ComponentType } from 'react'
import { createHashRouter, type RouteObject } from 'react-router'

import { AppShell } from '@/components/app-shell'
import { DEFAULT_PATH, NAV_ITEMS, type NavItem } from '@/lib/navigation'
import AgentPage from '@/routes/agent'
import AskPage from '@/routes/ask'
import { ChangesPage } from '@/routes/changes'
import EmbeddingPage from '@/routes/embedding'
import IndexJobsPage from '@/routes/index-jobs'
import { NotFoundPage } from '@/routes/not-found'
import NotePage from '@/routes/note'
import { OrganizePage } from '@/routes/organize'
import { OverviewPage } from '@/routes/overview'
import { PendingPage } from '@/routes/pending'
import { RecoveryPage } from '@/routes/recovery'
import { RouteErrorPage } from '@/routes/route-error'
import SearchPage from '@/routes/search'

/**
 * 路由表。
 *
 * 两个刻意的选择：
 *
 * **1. 路由由导航表生成，不手写。** `NAV_ITEMS.map(routeFor)` 意味着"侧栏有入口但
 * 路由没注册"在结构上不可能发生。反过来，往导航表里加一项就等于同时加了路由、
 * 侧栏项、面包屑与页面标题。
 *
 * **2. `createHashRouter` 而不是 `createBrowserRouter`。** `vite.config.ts` 把 `base`
 * 设成 `'./'`，为的是让 `dist/` 能被挂到任意路径下（本地预览、FastAPI 子路径均可）。
 * 浏览器路由在子路径挂载时需要配 `basename`，且刷新 `/search` 会要求服务端为未知
 * 路径回退到 `index.html`——而 F-2 的单端口托管还没做。哈希路由没有这两个前提，
 * 代价只是地址栏里多一个 `#`。本地单用户工具，这个取舍很划算。
 */

/**
 * 已实现的页面。
 *
 * 与导航表的 `status: 'ready'` 是**双向**约束：标了 ready 就必须有实现，反之亦然。
 * 这不是形式主义——它让"这个页面做完了没有"只有一个答案，且答案在启动时被检查，
 * 而不是靠用户点进去发现是空白页。
 */
const READY_PAGES: Record<string, ComponentType> = {
  [DEFAULT_PATH]: OverviewPage,
  '/search': SearchPage,
  '/ask': AskPage,
  '/notes': NotePage,
  '/index': IndexJobsPage,
  '/embedding': EmbeddingPage,
  '/changes': ChangesPage,
  '/organize': OrganizePage,
  '/recovery': RecoveryPage,
  '/agent': AgentPage,
}

function checkPagesAreInSync(): void {
  const ready = NAV_ITEMS.filter((item) => item.status === 'ready').map((item) => item.path)
  const missing = ready.filter((path) => !(path in READY_PAGES))
  const orphaned = Object.keys(READY_PAGES).filter((path) => !ready.includes(path))
  const problems = [
    ...missing.map((path) => `导航表标为 ready 但没有实现：${path}`),
    ...orphaned.map((path) => `已实现但没有在导航表里标为 ready：${path}`),
  ]
  if (problems.length > 0) {
    throw new Error(`路由表与导航表不一致：\n  ${problems.join('\n  ')}`)
  }
}

checkPagesAreInSync()

function pageFor(item: NavItem) {
  const Page = READY_PAGES[item.path]
  return Page === undefined ? <PendingPage item={item} /> : <Page />
}

function routeFor(item: NavItem): RouteObject {
  // 每个子路由都自带 errorElement，页面组件抛错时外壳（侧栏、页头）不会被拆掉。
  const shared = { element: pageFor(item), errorElement: <RouteErrorPage /> }
  if (item.path === DEFAULT_PATH) return { index: true, ...shared }
  return { path: item.routePath ?? item.path.replace(/^\//, ''), ...shared }
}

const routes: RouteObject[] = [
  {
    path: '/',
    element: <AppShell />,
    errorElement: <RouteErrorPage />,
    children: [
      ...NAV_ITEMS.map(routeFor),
      // 兜底：地址栏被手改过（本地应用里 404 几乎只有这一个来源）。
      { path: '*', element: <NotFoundPage />, errorElement: <RouteErrorPage /> },
    ],
  },
]

export const router = createHashRouter(routes)
