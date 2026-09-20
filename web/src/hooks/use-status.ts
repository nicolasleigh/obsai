import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'

/**
 * `/status` 的查询键。
 *
 * 所有页面（概览、侧栏状态灯、将来的索引页）都必须从这里取键，而不是各自写
 * `['status']`。拼错一个字符串不会报错，只会让缓存悄悄分裂成两份、各自轮询。
 */
export const STATUS_QUERY_KEY = ['status'] as const

/** 后端状态的唯一读取入口。轮询间隔由调用方决定，默认不轮询。 */
export function useStatus() {
  return useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: ({ signal }) => api.status(signal),
  })
}
