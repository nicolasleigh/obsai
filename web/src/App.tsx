import { RouterProvider } from 'react-router'

import { router } from '@/router'

/**
 * 应用根。
 *
 * 阶段 B-3 起根组件是路由而不是单个页面：外壳（侧栏、面包屑、全局提示）在
 * `components/app-shell.tsx`，路由表在 `router.tsx`，导航项在 `lib/navigation.ts`。
 *
 * `QueryClientProvider` 与 `ThemeProvider` 留在 `main.tsx`——它们比路由更外层，
 * 错误页也需要能读到查询缓存与主题。
 */
export default function App() {
  return <RouterProvider router={router} />
}
