import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { ChangePlanView } from '@/lib/api-types'

export const CHANGE_PLAN_QUERY_KEY = 'change-plan' as const

/**
 * 变更计划获取 Hook。
 *
 * 变更计划在服务端具备 10 分钟活跃 TTL，审阅时无需持续轮询。
 */
export function useChangePlan(planId: string | null) {
  const normalizedId = planId?.trim() ?? ''
  return useQuery<ChangePlanView>({
    queryKey: [CHANGE_PLAN_QUERY_KEY, normalizedId],
    queryFn: ({ signal }) => {
      if (!normalizedId) throw new Error('useChangePlan called with empty planId')
      return api.getChangePlan(normalizedId, signal)
    },
    enabled: normalizedId !== '',
    staleTime: 30_000,
  })
}
