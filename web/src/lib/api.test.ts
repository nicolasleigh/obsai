/**
 * B-2 验收：fetch 封装把四种响应都收敛成一种失败。
 *
 * 这里 mock 掉 `fetch` 而不是起真后端，因为要测的恰恰是**后端不按预期回答**时的
 * 行为：返回 HTML、返回 200 但正文不是 JSON、返回 FastAPI 默认的 `{"detail": ...}`。
 * 这些情况在真实后端上很难构造，而它们正是"页面偶尔白屏"的来源。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, request } from './api'
import { ApiError } from './errors'

const REQUEST_ID_HEADER = 'X-Request-ID'

/**
 * 替身 `fetch`。
 *
 * 接受工厂函数是因为 `Response` 的正文只能读一次：一个用例里发两次请求时，复用同一个
 * 实例会让第二次读取抛错，看起来像"响应不是合法 JSON"，而真正的原因是测试自己读过了。
 */
function mockFetch(
  response: Response | (() => Response) | Promise<never>,
): ReturnType<typeof vi.fn> {
  const stub = vi.fn(() => {
    if (response instanceof Promise) return response
    return Promise.resolve(typeof response === 'function' ? response() : response)
  })
  vi.stubGlobal('fetch', stub)
  return stub
}

function callOf(stub: ReturnType<typeof vi.fn>, index = 0): [string, RequestInit] {
  return stub.mock.calls[index] as unknown as [string, RequestInit]
}

function headerOf(init: RequestInit, name: string): string | undefined {
  return (init.headers as Record<string, string> | undefined)?.[name]
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('request', () => {
  it('拼上 /api/v1 前缀并注入 request id', async () => {
    const stub = mockFetch(new Response(JSON.stringify({ status: 'ok' }), { status: 200 }))
    await request('/health')

    const [url, init] = callOf(stub)
    expect(url).toBe('/api/v1/health')
    expect(init.method).toBe('GET')
    expect(headerOf(init, REQUEST_ID_HEADER)).toMatch(/^[0-9a-f-]{36}$/)
    expect(headerOf(init, 'Accept')).toBe('application/json')
  })

  it('复用调用方给的 request id，便于把重试关联到同一条后端日志', async () => {
    const stub = mockFetch(new Response('{}', { status: 200 }))
    await request('/status', { requestId: 'retry-2' })
    expect(headerOf(callOf(stub)[1], REQUEST_ID_HEADER)).toBe('retry-2')
  })

  it('每次请求用不同的 request id', async () => {
    const stub = mockFetch(() => new Response('{}', { status: 200 }))
    await request('/health')
    await request('/health')
    const [first, second] = stub.mock.calls.map((call) => headerOf(call[1] as RequestInit, REQUEST_ID_HEADER))
    expect(first).not.toBe(second)
  })

  it('把 JSON 请求体序列化并声明 Content-Type', async () => {
    const stub = mockFetch(new Response('{}', { status: 200 }))
    await request('/search', { method: 'POST', body: { query: 'redis', mode: 'keyword' } })

    const [, init] = callOf(stub)
    expect(init.method).toBe('POST')
    expect(headerOf(init, 'Content-Type')).toBe('application/json')
    expect(init.body).toBe(JSON.stringify({ query: 'redis', mode: 'keyword' }))
  })

  it('GET 不带请求体也不声明 Content-Type', async () => {
    const stub = mockFetch(new Response('{}', { status: 200 }))
    await request('/status')
    const [, init] = callOf(stub)
    expect(init.body).toBeUndefined()
    expect(headerOf(init, 'Content-Type')).toBeUndefined()
  })

  it('把 204 当作空响应，而不是尝试解析 JSON', async () => {
    mockFetch(new Response(null, { status: 204 }))
    await expect(request('/nothing')).resolves.toBeUndefined()
  })

  it('把 AbortSignal 原样透传，让取消真的能中断请求', async () => {
    const stub = mockFetch(new Response('{}', { status: 200 }))
    const controller = new AbortController()
    await request('/status', { signal: controller.signal })
    expect(callOf(stub)[1].signal).toBe(controller.signal)
  })
})

describe('request 的失败路径', () => {
  it('把服务端信封变成 ApiError', async () => {
    mockFetch(
      new Response(
        JSON.stringify({
          error: {
            code: 'recovery_required',
            type: 'RecoveryRequiredError',
            message: "Recovery required for transaction(s) t1; run 'obsai transaction recover ID'",
          },
          request_id: 'server-id',
        }),
        { status: 423, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    const failure = await request('/status').catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(ApiError)
    const error = failure as ApiError
    expect(error.code).toBe('recovery_required')
    expect(error.type).toBe('RecoveryRequiredError')
    expect(error.status).toBe(423)
    // 信封里的 request id 优先于本地生成的：它才是后端日志里那一条。
    expect(error.requestId).toBe('server-id')
  })

  it('优先信任响应头里的 request id', async () => {
    mockFetch(
      new Response(
        JSON.stringify({ error: { code: 'internal', type: 'RuntimeError', message: 'boom' }, request_id: 'body-id' }),
        { status: 500, headers: { 'Content-Type': 'application/json', [REQUEST_ID_HEADER]: 'header-id' } },
      ),
    )
    const error = (await request('/status').catch((e: unknown) => e)) as ApiError
    expect(error.requestId).toBe('header-id')
  })

  it('把 HTML 错误页识别成"对面不是本项目的服务"', async () => {
    // 开发代理打错端口时的典型响应。
    mockFetch(
      new Response('<!doctype html><title>502</title>', {
        status: 502,
        headers: { 'Content-Type': 'text/html' },
      }),
    )
    const error = (await request('/status').catch((e: unknown) => e)) as ApiError
    expect(error.code).toBe('unparsable_response')
    expect(error.status).toBe(502)
    // 不能抛 SyntaxError：那对排查毫无帮助。
    expect(error).toBeInstanceOf(ApiError)
  })

  it('把 FastAPI 默认的 {"detail": ...} 也识别成不可解析', async () => {
    // 未挂载错误处理器的第三方路由会返回这个形状；当成信封会让 code 变成 undefined。
    mockFetch(
      new Response(JSON.stringify({ detail: 'Not Found' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const error = (await request('/nope').catch((e: unknown) => e)) as ApiError
    expect(error.code).toBe('unparsable_response')
    expect(error.details).toBeUndefined()
  })

  it('把 200 但正文不是 JSON 的情况报出来，而不是返回 undefined', async () => {
    mockFetch(new Response('<html>', { status: 200, headers: { 'Content-Type': 'text/html' } }))
    const error = (await request('/status').catch((e: unknown) => e)) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('unparsable_response')
    expect(error.message).toBe('The response body is not valid JSON')
  })

  it('网络层失败原样抛出，交给 present() 翻译成"后端没启动"', async () => {
    mockFetch(Promise.reject(new TypeError('Failed to fetch')))
    await expect(request('/status')).rejects.toBeInstanceOf(TypeError)
  })

  it('取消原样抛出，不能被包装成 ApiError', async () => {
    const abort = new DOMException('The operation was aborted.', 'AbortError')
    mockFetch(Promise.reject(abort))
    const failure = await request('/status').catch((error: unknown) => error)
    expect(failure).toBe(abort)
  })
})

describe('api 端点', () => {
  it('status 打到 /api/v1/status 并原样返回字段名', async () => {
    const payload = {
      version: '0.1.0',
      vault_path: null,
      vault_ready: false,
      index: {
        path: '/tmp/index.db',
        exists: false,
        usable: false,
        error: null,
        note_count: 0,
        chunk_count: 0,
        vector_count: 0,
        generation: null,
        semantic_ready: false,
        dirty_notes: [],
      },
      unfinished_transactions: [],
      index_dirty_transactions: [],
      recovery_required: false,
      locked: false,
      started_at: '2026-09-14T00:00:00Z',
      uptime_seconds: 1,
    }
    const stub = mockFetch(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const status = await api.status()
    expect(callOf(stub)[0]).toBe('/api/v1/status')
    // snake_case 原样保留：不做 camelCase 转换，拼错字段名会在 tsc 阶段暴露。
    expect(status.vault_ready).toBe(false)
    expect(status.index.semantic_ready).toBe(false)
    expect(status.uptime_seconds).toBe(1)
  })

  it('health 打到 /api/v1/health', async () => {
    const stub = mockFetch(new Response(JSON.stringify({ status: 'ok' }), { status: 200 }))
    await expect(api.health()).resolves.toEqual({ status: 'ok' })
    expect(callOf(stub)[0]).toBe('/api/v1/health')
  })

  it('把 AbortSignal 传给 status', async () => {
    const stub = mockFetch(new Response('{}', { status: 200 }))
    const controller = new AbortController()
    await api.status(controller.signal)
    expect(callOf(stub)[1].signal).toBe(controller.signal)
  })

  it('startAgentRun 打到 POST /api/v1/agent/runs 并传参', async () => {
    const stub = mockFetch(
      new Response(
        JSON.stringify({
          run_id: 'r1',
          status: 'completed',
          query: 'search redis',
          step_count: 1,
          retrieval_step_count: 1,
          selected_note_ids: [],
          retrieved_chunk_ids: [],
          timeline: [],
          final_answer: 'ans',
          stop_reason: null,
          pending_approval: null,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    const result = await api.startAgentRun({ query: 'search redis' })
    expect(result.run_id).toBe('r1')
    expect(callOf(stub)[0]).toBe('/api/v1/agent/runs')
    expect(callOf(stub)[1].method).toBe('POST')
    expect(callOf(stub)[1].body).toBe(JSON.stringify({ query: 'search redis' }))
  })

  it('getAgentRun 打到 GET /api/v1/agent/runs/{id}', async () => {
    const stub = mockFetch(
      new Response(
        JSON.stringify({
          run_id: 'r-123',
          status: 'completed',
          query: 'q',
          step_count: 0,
          retrieval_step_count: 0,
          selected_note_ids: [],
          retrieved_chunk_ids: [],
          timeline: [],
          final_answer: null,
          stop_reason: null,
          pending_approval: null,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    const result = await api.getAgentRun('r-123')
    expect(result.run_id).toBe('r-123')
    expect(callOf(stub)[0]).toBe('/api/v1/agent/runs/r-123')
    expect(callOf(stub)[1].method).toBe('GET')
  })

  it('resumeAgentRun 打到 POST /api/v1/agent/runs/{id}/resume', async () => {
    const stub = mockFetch(
      new Response(
        JSON.stringify({
          run_id: 'r-123',
          status: 'completed',
          query: 'q',
          step_count: 2,
          retrieval_step_count: 0,
          selected_note_ids: [],
          retrieved_chunk_ids: [],
          timeline: [],
          final_answer: 'applied',
          stop_reason: null,
          pending_approval: null,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    const result = await api.resumeAgentRun('r-123', { approved: true })
    expect(result.final_answer).toBe('applied')
    expect(callOf(stub)[0]).toBe('/api/v1/agent/runs/r-123/resume')
    expect(callOf(stub)[1].method).toBe('POST')
    expect(callOf(stub)[1].body).toBe(JSON.stringify({ approved: true }))
  })
})

