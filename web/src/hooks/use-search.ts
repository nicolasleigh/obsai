import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { ConsentApproval, SearchRequest } from '@/lib/api-types'

export const SEARCH_QUERY_KEY = 'search' as const

/**
 * 搜索查询。
 *
 * **批准进入 queryKey，但用的是 `consent_id` 而不是 `nonce`。** 这个选择依赖 B-8
 * 修好的那条性质：`consent_id` 只由查询与嵌入代次决定，**跨探测稳定**，所以对同一次
 * 查询反复批准只会命中同一条缓存。换成 `nonce` 就会每批一次在缓存里堆一份内容相同的
 * 结果——`nonce` 每次都新，缓存键于是每次都新。
 *
 * **不用 `refetch()` 而是换键**，是为了不依赖一条时序假设：批准是在事件处理器里设的，
 * 在同一个处理器里紧接着调 `refetch()` 时组件还没重渲染，`queryFn` 闭包读到的仍是
 * 上一次的 `approval`——那次会发一个没有批准的请求，用户看到的结果又降级回去。换键则
 * 由 TanStack 自己驱动取数，顺序是确定的。
 *
 * 代价是批准的那一刻新键还没有缓存，结果区会先显示一次骨架屏。本地后端下这是几十
 * 毫秒；相比"批准了却仍然拿到关键词结果"，一次骨架屏是更小的代价。
 */
export function useSearch(request: SearchRequest | null, approval: ConsentApproval | null) {
  return useQuery({
    queryKey: [SEARCH_QUERY_KEY, request, approval?.consent_id ?? null],
    queryFn: ({ signal }) => {
      if (!request) throw new Error('useSearch called with null request')
      return api.search(approval === null ? request : { ...request, approval }, signal)
    },
    enabled: request !== null && request.query.trim().length > 0,
  })
}
