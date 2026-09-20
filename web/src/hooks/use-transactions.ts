import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { JournalView, RecoveryView } from '@/lib/api-types'

export const TRANSACTIONS_QUERY_KEY = 'transactions' as const
export const TRANSACTION_RECOVERY_QUERY_KEY = 'transaction-recovery' as const

/**
 * 获取 Vault 中所有事务日志列表的 Hook。
 */
export function useTransactions() {
  return useQuery<JournalView[]>({
    queryKey: [TRANSACTIONS_QUERY_KEY],
    queryFn: ({ signal }) => api.getTransactions(signal),
    staleTime: 10_000,
  })
}

/**
 * 获取特定事务恢复快照与回滚 Diff 预览的 Hook。
 */
export function useTransactionRecovery(transactionId: string | null) {
  return useQuery<RecoveryView>({
    queryKey: [TRANSACTION_RECOVERY_QUERY_KEY, transactionId],
    queryFn: ({ signal }) => {
      if (!transactionId) {
        throw new Error('transactionId is required')
      }
      return api.getTransaction(transactionId, signal)
    },
    enabled: Boolean(transactionId),
    staleTime: 10_000,
  })
}
