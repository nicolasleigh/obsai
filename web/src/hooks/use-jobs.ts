/**
 * 任务进度的唯一读取入口：订阅事件流，并把两条事件合并成一份列表。
 *
 * **为什么不走 TanStack Query。** 任务状态是**推**来的：服务端每次连接先发一条全量
 * 快照，之后只发变化的那几个（见 `api/routes/jobs.py`）。把它塞进 Query 的缓存意味着
 * 要么把每次推送当成一次"取数成功"（缓存键会一直变，`staleTime` 毫无意义），要么自己
 * 实现一个假的 `queryFn`。这里用 `useState` + 一个订阅，就是它本来的形状。
 *
 * **`received` 是这一层最重要的东西。** 空数组有歧义：流还没连上、后端刚重启、代理
 * 打错端口——三种情况都是"零个任务"，而只有第一种会自己好。B-4 在概览页上立的规矩是
 * 信息不可得时报 `unknown` 而不是报"一切正常"（见 `lib/status.ts`），这里把那个判断的
 * 依据暴露出来：`received === false` 就是"还不知道"，页面不该画成"没有任务"。
 *
 * **断线不等于取消。** 这个 hook 里没有任何东西能停下任务，它也不发任何自己的请求。
 * 取消只有 `useCancelJob` 一个入口，由人点。
 */

import { useMutation } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api } from '@/lib/api'
import type { JobView } from '@/lib/api-types'
import { mergeJobs, sortJobs } from '@/lib/jobs'
import { subscribeToJobs, type JobStreamStatus } from '@/lib/sse'

export type JobsFeed = {
  /** 已知的任务，最近的在最前面。**流还没连上时是空数组**——先看 `received`。 */
  jobs: readonly JobView[]
  /**
   * 是否已经收到过至少一次快照。
   *
   * `false` 表示"还不知道有哪些任务"，不是"没有任务"。两者在页面上必须是两句话。
   */
  received: boolean
  status: JobStreamStatus
  /**
   * 最近一次负载解析失败。非空说明前端和服务端的契约漂了——那时列表可能停在旧数据上，
   * 页面应该说"进度可能不是最新的"，而不是继续装作在实时更新。
   */
  parseError: unknown | null
}

const INITIAL: JobsFeed = { jobs: [], received: false, status: 'connecting', parseError: null }

/** 订阅任务流。组件卸载时断开——断开**不会**取消任何任务。 */
export function useJobs(): JobsFeed {
  const [feed, setFeed] = useState<JobsFeed>(INITIAL)

  useEffect(() => {
    const subscription = subscribeToJobs({
      // 快照是**全部**，所以是替换而不是合并；把快照并进旧列表，会让一个已经不在
      // 日志里的任务永远留在页面上。
      onSnapshot: (jobs) => setFeed((prev) => ({ ...prev, jobs: sortJobs(jobs), received: true })),
      onJobs: (jobs) => setFeed((prev) => ({ ...prev, jobs: mergeJobs(prev.jobs, jobs) })),
      onStatus: (status) => setFeed((prev) => (prev.status === status ? prev : { ...prev, status })),
      onError: (parseError) => setFeed((prev) => ({ ...prev, parseError })),
    })
    return () => subscription.close()
  }, [])

  return feed
}

/**
 * 开始一次增量索引更新。
 *
 * 不写 `retry`：全局策略里 mutation 一律不重试（见 `lib/queryClient.ts`）。这里也一样
 * 需要它——重试会排两个任务，第二个拿着第一个留下的状态跑。
 *
 * 成功后**不主动改列表**：事件流在半秒内会把这条任务送过来，而多一条"手动插进去"的
 * 路径就意味着列表有两个真相源。半秒的等待换来"列表永远只由流驱动"，值。
 */
export function useIndexUpdate() {
  return useMutation({ mutationFn: () => api.indexUpdate() })
}

export function useIndexRebuild() {
  return useMutation({ mutationFn: () => api.indexRebuild() })
}

/**
 * 请求取消一个任务。**这是唯一的取消入口。**
 *
 * 返回的是请求**之后**的任务状态，不是布尔值：取消是协作式的，"已接受"和"已经结束"
 * 是同一个回答，而状态自己会说是哪一个。列表由流更新，所以这里只把响应交给调用方
 * 做一次性提示。
 */
export function useCancelJob() {
  return useMutation({ mutationFn: (jobId: string) => api.cancelJob(jobId) })
}
