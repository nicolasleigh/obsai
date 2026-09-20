import { isRouteErrorResponse, Link, useRouteError } from 'react-router'

import { ErrorPanel } from '@/components/error-panel'
import { Button } from '@/components/ui/button'
import { present, type ErrorPresentation } from '@/lib/errors'
import { DEFAULT_PATH } from '@/lib/navigation'

/**
 * 路由级错误页。
 *
 * 挂在每个子路由上（见 `router.tsx`），因此页面组件抛错时**外壳仍然在**——侧栏还能
 * 点，用户可以直接切走，而不是看到一个白屏后去刷新。
 *
 * 复用 `lib/errors.ts` 的 `present()`，所以即使崩溃发生在前端，提示仍然是中文的，
 * 并且与服务端错误的措辞一致。
 */

/** react-router 自己的 404 / 重定向错误是 Response 形状，不是 `Error` 实例。 */
function toPresentation(error: unknown): ErrorPresentation {
  if (isRouteErrorResponse(error)) {
    return {
      title: `页面加载失败（HTTP ${error.status}）`,
      hint: error.statusText === '' ? undefined : error.statusText,
      retryable: false,
    }
  }
  return present(error)
}

function RouteErrorPage() {
  const error = useRouteError()

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">这个界面出错了</h1>
        <p className="text-muted-foreground text-sm">
          错误被路由层接住，侧栏仍然可用，不需要刷新整个页面。
        </p>
      </header>
      <ErrorPanel
        failure={toPresentation(error)}
        retryLabel="重新加载"
        onRetry={() => window.location.reload()}
      />
      <div>
        <Button asChild variant="outline">
          <Link to={DEFAULT_PATH}>回到概览</Link>
        </Button>
      </div>
    </div>
  )
}

export { RouteErrorPage }
