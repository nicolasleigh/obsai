/**
 * C-2 验收：进度流断了能续上，而**断线不等于取消**。
 *
 * 用替身 `EventSource` 而不是真连接，因为要测的恰恰是"服务端没有按预期回答"的那几种
 * 情况：非 2xx 让浏览器彻底放弃、负载不是数组、断开之后又收到 error。这些在真后端上
 * 很难构造，而它们正是"进度条永远停在 0%"的来源。
 *
 * 最后一条测试守的是这件事的反面：**断开订阅不发任何请求**。取消只有一个入口
 * （`api.cancelJob` 那个 POST），所以这条通道一旦哪天自己发了请求，那条断言就会红。
 * 断网、关页面、刷新都不该停下任何任务，而"这个模块碰不到取消"就是它成立的原因。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { subscribeToJobs } from './sse'

/** `EventSource.readyState` 里"浏览器不会再重连了"的那个值。 */
const READY_STATE_CLOSED = 2
/** 还在重试中的那个值：此时浏览器自己会接着试。 */
const READY_STATE_CONNECTING = 0

class FakeEventSource {
  static instances: FakeEventSource[] = []

  static latest(): FakeEventSource {
    const source = FakeEventSource.instances[FakeEventSource.instances.length - 1]
    if (source === undefined) throw new Error('没有创建过 EventSource')
    return source
  }

  static reset(): void {
    FakeEventSource.instances = []
  }

  readonly url: string
  readyState = READY_STATE_CONNECTING
  closed = false
  private readonly listeners = new Map<string, Array<(event: Event) => void>>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (event: Event) => void): void {
    const existing = this.listeners.get(type) ?? []
    existing.push(listener)
    this.listeners.set(type, existing)
  }

  close(): void {
    this.closed = true
    this.readyState = READY_STATE_CLOSED
  }

  emit(type: string, data?: string): void {
    // 只要 `data`，所以不必依赖运行时真的存在 `MessageEvent`。
    const event = (data === undefined ? {} : { data }) as MessageEvent
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }

  /** 服务端回非 2xx 时浏览器的表现：派发 error，并把连接置为彻底关闭。 */
  fail(): void {
    this.readyState = READY_STATE_CLOSED
    this.emit('error')
  }
}

function fakeSources() {
  return (url: string) => new FakeEventSource(url)
}

function recorder() {
  const snapshots: unknown[][] = []
  const changes: unknown[][] = []
  const errors: unknown[] = []
  const statuses: string[] = []
  return {
    snapshots,
    changes,
    errors,
    statuses,
    value: {
      onSnapshot: (jobs: unknown[]) => snapshots.push(jobs),
      onJobs: (jobs: unknown[]) => changes.push(jobs),
      onError: (error: unknown) => errors.push(error),
      onStatus: (status: string) => statuses.push(status),
    },
  }
}

afterEach(() => {
  FakeEventSource.reset()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('subscribeToJobs', () => {
  it('连的是 API 前缀下的 /events，与 fetch 那条通道共用同一个前缀', () => {
    const recorded = recorder()
    const subscription = subscribeToJobs(recorded.value, { createSource: fakeSources() })

    expect(FakeEventSource.latest().url).toBe('/api/v1/events')
    subscription.close()
  })

  it('snapshot 与 job 分别交付，两者的负载都是数组', () => {
    const recorded = recorder()
    const subscription = subscribeToJobs(recorded.value, { createSource: fakeSources() })
    const source = FakeEventSource.latest()

    source.emit('open')
    source.emit('snapshot', JSON.stringify([{ job_id: 'j1' }]))
    source.emit('job', JSON.stringify([{ job_id: 'j1' }, { job_id: 'j2' }]))

    expect(recorded.snapshots).toEqual([[{ job_id: 'j1' }]])
    expect(recorded.changes).toEqual([[{ job_id: 'j1' }, { job_id: 'j2' }]])
    expect(recorded.errors).toEqual([])
    expect(recorded.statuses).toEqual(['connecting', 'open'])
    subscription.close()
  })

  it('负载不是数组时报错而不是抛出去', () => {
    const recorded = recorder()
    const subscription = subscribeToJobs(recorded.value, { createSource: fakeSources() })
    const source = FakeEventSource.latest()

    // 对象而不是数组：服务端换了形状，或代理把别的服务的响应转了过来。
    source.emit('snapshot', '{"job_id":"j1"}')
    // 根本不是 JSON。
    source.emit('job', '{')

    expect(recorded.errors).toHaveLength(2)
    expect(recorded.snapshots).toEqual([])
    expect(recorded.changes).toEqual([])
    subscription.close()
  })

  it('浏览器彻底放弃之后自己重建，并按指数退避放大间隔', () => {
    vi.useFakeTimers()
    const recorded = recorder()
    const subscription = subscribeToJobs(recorded.value, {
      createSource: fakeSources(),
      minRetryMs: 100,
      maxRetryMs: 400,
    })
    expect(FakeEventSource.instances).toHaveLength(1)

    FakeEventSource.latest().fail()
    vi.advanceTimersByTime(99)
    expect(FakeEventSource.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeEventSource.instances).toHaveLength(2)

    FakeEventSource.latest().fail()
    vi.advanceTimersByTime(199)
    expect(FakeEventSource.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeEventSource.instances).toHaveLength(3)

    // 上限封顶，不会无限放大。
    FakeEventSource.latest().fail()
    vi.advanceTimersByTime(400)
    expect(FakeEventSource.instances).toHaveLength(4)

    subscription.close()
  })

  it('连上过一次之后退避归零，不会越等越久', () => {
    vi.useFakeTimers()
    const recorded = recorder()
    const subscription = subscribeToJobs(recorded.value, {
      createSource: fakeSources(),
      minRetryMs: 100,
      maxRetryMs: 400,
    })

    FakeEventSource.latest().fail()
    vi.advanceTimersByTime(100)
    expect(FakeEventSource.instances).toHaveLength(2)

    // 这次连上了：一次成功的连接应当把"已经失败过几次"这件事忘掉。
    FakeEventSource.latest().emit('open')
    FakeEventSource.latest().fail()
    vi.advanceTimersByTime(100)
    expect(FakeEventSource.instances).toHaveLength(3)

    subscription.close()
  })

  it('还在 CONNECTING 时交给浏览器，不自己再建一个', () => {
    vi.useFakeTimers()
    const recorded = recorder()
    const subscription = subscribeToJobs(recorded.value, {
      createSource: fakeSources(),
      minRetryMs: 10,
      maxRetryMs: 40,
    })

    // 连接断了，但浏览器自己会重试——这时再建一个会让连接翻倍。
    FakeEventSource.latest().emit('error')
    vi.advanceTimersByTime(10_000)

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(recorded.statuses).toContain('reconnecting')
    subscription.close()
  })

  it('断开订阅之后不再重建，也不发任何请求', () => {
    vi.useFakeTimers()
    const fetchStub = vi.fn()
    vi.stubGlobal('fetch', fetchStub)
    const recorded = recorder()
    const subscription = subscribeToJobs(recorded.value, {
      createSource: fakeSources(),
      minRetryMs: 10,
      maxRetryMs: 40,
    })
    const source = FakeEventSource.latest()

    subscription.close()
    // 关闭之后到达的事件（连接刚断、错误刚回来）都不该复活订阅。
    source.emit('error')
    source.fail()
    vi.advanceTimersByTime(10_000)

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(source.closed).toBe(true)
    // 这条就是"断线不等于取消"：取消只有一个入口，是 `api.cancelJob` 的 POST，
    // 而这条通道自己从不发请求。
    expect(fetchStub).not.toHaveBeenCalled()
  })
})
