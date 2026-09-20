/**
 * 向量生成计划与审批相关的 React Query hooks。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { EmbeddingPlanView, JobView } from '@/lib/api-types'

export const EMBEDDING_PLAN_KEY = ['embedding-plan'] as const

/** 获取最新的向量生成计划预估。 */
export function useEmbeddingPlan() {
  return useQuery<EmbeddingPlanView>({
    queryKey: EMBEDDING_PLAN_KEY,
    queryFn: ({ signal }) => api.embeddingPlan(signal),
    staleTime: 0,
    retry: false,
  })
}

/** 批准执行向量生成计划。 */
export function useApproveEmbeddingPlan() {
  const queryClient = useQueryClient()

  return useMutation<JobView, Error, { planId: string; nonce: string }>({
    mutationFn: ({ planId, nonce }) => api.approveEmbeddingPlan(planId, nonce),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: EMBEDDING_PLAN_KEY })
      queryClient.invalidateQueries({ queryKey: ['status'] })
    },
  })
}
