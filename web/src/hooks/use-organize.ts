import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { OrganizePreview } from '@/lib/api-types'

export const ORGANIZE_PROPOSALS_QUERY_KEY = 'organize-proposals' as const

/**
 * Inbox 整理提案获取 Hook。
 *
 * 只读扫描请求，获取当前待整理笔记的提案列表与置信度建议。
 */
export function useOrganizeProposals() {
  return useQuery<OrganizePreview>({
    queryKey: [ORGANIZE_PROPOSALS_QUERY_KEY],
    queryFn: ({ signal }) => api.organizeProposals(signal),
    staleTime: 15_000,
  })
}
