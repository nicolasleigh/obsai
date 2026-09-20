import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { AgentRunView } from '@/lib/api-types'

export const AGENT_RUN_QUERY_KEY = 'agent-run' as const

/**
 * 根据 run_id 获取 Agent 工作流快照状态的 Hook。
 */
export function useAgentRun(runId: string | null) {
  return useQuery<AgentRunView>({
    queryKey: [AGENT_RUN_QUERY_KEY, runId],
    queryFn: ({ signal }) => {
      if (!runId) {
        throw new Error('runId is required')
      }
      return api.getAgentRun(runId, signal)
    },
    enabled: Boolean(runId),
    staleTime: 5_000,
  })
}
