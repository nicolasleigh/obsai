import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { AskRequest } from '@/lib/api-types'

export const ASK_QUERY_KEY = 'ask' as const

/**
 * 同一份 Vault、同一个问题，多问一次不会得到更好的答案，但每次都要花时间和钱。
 *
 * 所以这里的"新鲜期"比全局的 5 秒长得多：点开引用去看笔记、再切回来，不应该触发
 * 第二次模型调用。要重新生成就显式 `refetch()`——它不受 `staleTime` 限制。
 */
const ASK_STALE_TIME_MS = 5 * 60 * 1_000

/**
 * 一次提问。
 *
 * 传 `null` 表示"还没有问题"，此时不发请求——与 `useSearch` 同一套约定，页面因此
 * 不需要在空输入上做特判。
 */
export function useAsk(request: AskRequest | null) {
  return useQuery({
    queryKey: [ASK_QUERY_KEY, request],
    queryFn: ({ signal }) => {
      if (!request) throw new Error('useAsk called with null request')
      return api.ask(request, signal)
    },
    enabled: request !== null && request.query.trim().length > 0,
    staleTime: ASK_STALE_TIME_MS,
  })
}
