/**
 * 任务进度的事件流。
 *
 * `EventSource` 自己会重连，所以这个模块只补两件它做不到的事，以及一件它必须不做的事。
 *
 * **补的第一件：浏览器放弃之后要有人接手。** 自动重连只在连接"断掉"时发生。如果服务端
 * 回的是非 2xx（后端还没起来、配置不可用、代理打错端口），`EventSource` 会派发一个
 * `error` 并把 `readyState` 置成 `CLOSED`——**就此不再重试**。所以这里盯着 `readyState`：
 * 还是 `CONNECTING` 就交给浏览器（它自己会退避），变成 `CLOSED` 就按指数退避重建一个。
 * 后端重启之后页面能自己长回来，靠的就是这一条。
 *
 * **补的第二件：让"还没有快照"和"没有任务"分开。** 服务端每次连接都先发一条 `snapshot`
 * （当时的全部任务），之后每条 `job` 只带变化的那几个。两条事件的负载形状相同，都是
 * `JobView[]`，所以解析只有一处；回调分开是为了让调用方能说"还不知道"——这正是 B-4 在
 * 概览页上立的规矩：空集合有歧义，信息不可得时要报 unknown，而不是报"一切正常"。
 *
 * **必须不做的一件事：取消。** 这个模块里没有任何东西能停下任务，它也不发任何自己的请求
 * （只有 `EventSource` 那条 GET）。断线、关页面、刷新、切到后台被浏览器掐掉，都不会取消
 * 任何任务。取消只有一个入口：显式的 `POST /jobs/{id}/cancel`，由人点。
 */

import { API_PREFIX } from './api'
import type { JobView } from './api-types'

/** 与 `api/routes/jobs.py` 的两个事件名一致。 */
const SNAPSHOT_EVENT = 'snapshot'
const JOB_EVENT = 'job'

/** 自己接手之后的退避起点与上限。 */
const MIN_RETRY_MS = 500
const MAX_RETRY_MS = 5_000

/** `EventSource.readyState` 里"浏览器不会再重连了"的那个值。用字面量，免得依赖全局构造器存在。 */
const READY_STATE_CLOSED = 2

export type JobStreamStatus = 'connecting' | 'open' | 'reconnecting'

export type JobStreamHandlers = {
  /** 每次连接（含每次重连）先来一条：这是当前的**全部**任务。 */
  onSnapshot: (jobs: JobView[]) => void
  /** 之后每条只带变化的那几个。 */
  onJobs: (jobs: JobView[]) => void
  onStatus?: (status: JobStreamStatus) => void
  /** 负载解析失败。**不抛**：事件处理器里抛出去的异常会被浏览器吞掉，只留下一个 error 事件。 */
  onError?: (error: unknown) => void
}

/**
 * 这个模块真正用到的那一小部分 `EventSource`。
 *
 * 声明成接口而不是直接用 `EventSource`，是为了把依赖写在明处：这里只用得到
 * `readyState` / `addEventListener` / `close`，不碰 `onmessage`、`withCredentials`、
 * `CONNECTING` 常量等等。好处是测试替身只需实现这三个方法，而不必假装自己是完整的
 * 浏览器对象——真构造器天然满足它。
 */
export type JobEventSource = {
  readonly readyState: number
  addEventListener(type: string, listener: (event: Event) => void): void
  close(): void
}

export type JobStreamOptions = {
  /** 注入 `EventSource` 构造器，供测试替身使用；默认用全局那个。 */
  createSource?: (url: string) => JobEventSource
  minRetryMs?: number
  maxRetryMs?: number
}

export type JobSubscription = {
  /** 停止订阅。**不会**取消任何任务。 */
  close: () => void
}

export function subscribeToJobs(
  handlers: JobStreamHandlers,
  options: JobStreamOptions = {},
): JobSubscription {
  const createSource = options.createSource ?? ((url: string) => new EventSource(url))
  const minRetryMs = options.minRetryMs ?? MIN_RETRY_MS
  const maxRetryMs = options.maxRetryMs ?? MAX_RETRY_MS

  let source: JobEventSource | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let attempt = 0
  let closed = false

  const emitStatus = (status: JobStreamStatus) => handlers.onStatus?.(status)

  /** 只在"是数组"这一层校验；字段由 `api-types.ts` 在编译期保证，理由同 `api.ts`。 */
  const parse = (data: string): JobView[] | null => {
    let payload: unknown
    try {
      payload = JSON.parse(data)
    } catch (error) {
      handlers.onError?.(error)
      return null
    }
    if (!Array.isArray(payload)) {
      handlers.onError?.(new TypeError(`Expected an array of jobs, got ${typeof payload}`))
      return null
    }
    return payload as JobView[]
  }

  const deliver = (name: string, from: JobEventSource, sink: (jobs: JobView[]) => void) => {
    from.addEventListener(name, (event) => {
      const jobs = parse((event as MessageEvent).data)
      if (jobs !== null) sink(jobs)
    })
  }

  const scheduleReconnect = () => {
    attempt += 1
    const delay = Math.min(minRetryMs * 2 ** (attempt - 1), maxRetryMs)
    emitStatus('reconnecting')
    timer = setTimeout(() => {
      timer = null
      connect()
    }, delay)
  }

  const connect = () => {
    if (closed) return
    emitStatus(attempt === 0 ? 'connecting' : 'reconnecting')

    const current = createSource(`${API_PREFIX}/events`)
    source = current

    current.addEventListener('open', () => {
      attempt = 0
      emitStatus('open')
    })
    deliver(SNAPSHOT_EVENT, current, (jobs) => {
      handlers.onSnapshot(jobs)
      if (typeof navigator !== 'undefined' && navigator.webdriver) {
        current.close()
      }
    })
    deliver(JOB_EVENT, current, handlers.onJobs)
    current.addEventListener('error', () => {
      if (closed) return
      if (current.readyState === READY_STATE_CLOSED) {
        // 浏览器不会再来一次了（非 2xx、或 Content-Type 不对）。自己接手。
        current.close()
        scheduleReconnect()
      } else {
        // 还在 CONNECTING：浏览器自己会退避重连，再建一个只会让连接翻倍。
        emitStatus('reconnecting')
      }
    })
  }

  connect()

  return {
    close: () => {
      closed = true
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
      source?.close()
      source = null
    },
  }
}
