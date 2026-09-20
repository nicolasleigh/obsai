/**
 * TanStack Query 的全局策略。
 *
 * 决策 D5（见实施计划 §11）：服务端状态全部交给 TanStack Query，UI 局部状态用
 * `useState`，暂不引入 Zustand。这个文件是那条决策唯一的落地点。
 *
 * 两个策略值得说明：
 *
 * - **`staleTime` 不为 0。** 默认值 0 意味着每次挂载都重新拉取。对一个本地工具
 *   来说这是纯噪声：Vault 和索引不会被别人在后台改动，用户来回切页不该触发请求。
 *   5 秒足够覆盖"切走再切回来"，又不至于让用户手动改完配置后看不到变化。
 * - **重试只重试值得重试的。** `present(error).retryable` 把"网络抖动 / 5xx"与
 *   "4xx 契约问题"分开：`ConfigError` 重试一百次还是同一个 `ConfigError`，而重试
 *   会让错误提示延迟出现。写入（mutation）一律不自动重试——审批用的 nonce 是一次性
 *   的，重放会被服务端拒绝，自动重试只会制造一个更难懂的错误。
 *
 * 这里不挂 toast。通知是"布局与路由"（B-3）的职责，而 `present()` 已经把中文标题和
 * 下一步动作准备好了，调用方决定怎么显示。
 */

import { QueryClient } from '@tanstack/react-query'

import { present } from './errors'

/** 切页/切窗口后多久算"旧数据"。 */
const STALE_TIME_MS = 5_000

/** 自动重试次数上限。本地服务要么立刻好，要么需要人工介入。 */
const MAX_RETRIES = 2

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: STALE_TIME_MS,
        // 本地工具：切回窗口不该产生请求，用户点刷新才刷新。
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => present(error).retryable && failureCount < MAX_RETRIES,
        retryDelay: (attempt) => Math.min(500 * 2 ** attempt, 4_000),
      },
      mutations: {
        // 审批 nonce 一次性；自动重放会被服务端拒绝。
        retry: false,
      },
    },
  })
}

/**
 * 全局单例。`main.tsx` 把它交给 `QueryClientProvider`。
 *
 * 放在模块作用域（而不是 `useState(() => createQueryClient())`）是有意的：开发期
 * 的 HMR 会重建组件树，如果每次重建都换一个新 client，缓存和请求取消都会失效。
 */
export const queryClient = createQueryClient()
