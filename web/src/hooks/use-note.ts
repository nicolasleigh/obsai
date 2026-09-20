import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'

export const NOTE_QUERY_KEY = 'note' as const

/**
 * 笔记读的是索引快照，所以它和 `useAsk` 用同一个新鲜期。
 *
 * 理由不是"请求很贵"，而是"同一份索引在一个会话里不会变"：索引只在 `obsai index
 * update` 或写入之后才更新，而那两件事都不会在这个页面上发生。短新鲜期的唯一效果
 * 是从引用跳出去再跳回来时闪一次骨架屏。
 */
const NOTE_STALE_TIME_MS = 5 * 60 * 1_000

/**
 * 一篇笔记。
 *
 * 传 `null` 表示还没有 note ID，此时不发请求——与 `useSearch`、`useAsk` 同一套约定。
 * 路由参数在匹配上之前是 `undefined`，页面因此不需要在渲染前做特判。
 */
export function useNote(noteId: string | null) {
  return useQuery({
    queryKey: [NOTE_QUERY_KEY, noteId],
    queryFn: ({ signal }) => {
      if (!noteId) throw new Error('useNote called with null id')
      return api.note(noteId, signal)
    },
    enabled: noteId !== null && noteId !== '',
    staleTime: NOTE_STALE_TIME_MS,
  })
}
