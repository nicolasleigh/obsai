/**
 * B-2 验收：服务端错误码能变成可直接展示的中文。
 *
 * 这条验收看起来只是"查表"，但真正的风险不在查表本身，而在**表会不会悄悄过期**：
 * 后端新增一个领域异常、目录里没有对应的键时，用户看到的不是报错，而是一句
 * 「操作失败」加一段英文。所以这里除了行为测试，还断言服务端能发出的每个码都在目录里
 * （反向的"目录里有没有多余的键"由 `tests/unit/test_web_error_catalogue.py` 在
 * Python 侧一并检查，因为那边才知道服务端到底能发出哪些码）。
 */

import { describe, expect, it } from 'vitest'

import { ApiError, KNOWN_ERROR_CODES, isErrorEnvelope, present } from './errors'

function apiError(init: Partial<ConstructorParameters<typeof ApiError>[0]> = {}): ApiError {
  return new ApiError({
    code: 'config',
    type: 'ConfigError',
    message: 'No vault configured; set vault.path in config.toml',
    status: 400,
    requestId: 'abc123',
    ...init,
  })
}

describe('present', () => {
  it('把已知的码翻成中文标题与下一步动作', () => {
    const view = present(apiError())
    expect(view.title).toBe('配置不可用')
    expect(view.hint).toContain('config.toml')
    expect(view.retryable).toBe(false)
  })

  it('保留服务端原文与 request id', () => {
    const view = present(apiError({ code: 'recovery_required', status: 423 }))
    expect(view.title).toBe('有未完成的写入事务，写入已被冻结')
    expect(view.detail).toBe('No vault configured; set vault.path in config.toml')
    expect(view.requestId).toBe('abc123')
    expect(view.status).toBe(423)
  })

  it('未知的码退到兜底文案，但绝不吞掉服务端原文', () => {
    const view = present(
      apiError({ code: 'brand_new_failure', type: 'BrandNewError', message: 'something specific' }),
    )
    expect(view.title).toBe('操作失败')
    expect(view.hint).toBeUndefined()
    expect(view.detail).toBe('something specific')
    expect(view.retryable).toBe(false)
  })

  it('只有值得重试的码才标记为可重试', () => {
    expect(present(apiError({ code: 'internal', status: 500 })).retryable).toBe(true)
    expect(present(apiError({ code: 'lock_busy', status: 409 })).retryable).toBe(true)
    expect(present(apiError({ code: 'conflict', status: 409 })).retryable).toBe(false)
    expect(present(apiError({ code: 'recovery_required', status: 423 })).retryable).toBe(false)
  })

  it('把取消识别成取消，而不是失败', () => {
    const abort = new DOMException('The operation was aborted.', 'AbortError')
    const view = present(abort)
    expect(view.title).toBe('请求已取消')
    expect(view.retryable).toBe(false)
    expect(view.detail).toBeUndefined()
  })

  it('把 fetch 的网络层失败识别成"后端没启动"', () => {
    const view = present(new TypeError('Failed to fetch'))
    expect(view.title).toBe('无法连接到本地服务')
    expect(view.hint).toContain('dev.sh')
    expect(view.retryable).toBe(true)
    expect(view.detail).toBe('Failed to fetch')
  })

  it('对完全未知的抛出物也不抛异常', () => {
    for (const value of [undefined, null, 'boom', 42, { message: 'x' }]) {
      const view = present(value)
      expect(view.title).toBe('操作失败')
      expect(typeof view.detail).toBe('string')
    }
  })

  it('ApiError.presentation 与 present() 是同一份结果', () => {
    const error = apiError({ code: 'schema' })
    expect(error.presentation).toEqual(present(error))
  })

  it('每个已知码都有非空的中文标题', () => {
    for (const code of KNOWN_ERROR_CODES) {
      const view = present(apiError({ code }))
      expect(view.title, code).not.toBe('操作失败')
      // 标题必须是中文，否则说明目录里混进了英文原文。
      expect(view.title, code).toMatch(/[\u4e00-\u9fa5]/)
    }
  })
})

describe('isErrorEnvelope', () => {
  it('接受服务端真实返回的信封', () => {
    expect(
      isErrorEnvelope({
        error: { code: 'config', type: 'ConfigError', message: 'x' },
        request_id: 'r',
      }),
    ).toBe(true)
  })

  it('接受没有 details 的最小信封', () => {
    expect(isErrorEnvelope({ error: { code: 'c', type: 'T', message: 'm' } })).toBe(true)
  })

  it('拒绝 FastAPI 的默认形状与其它噪声', () => {
    // 默认 404 体是 {"detail": "Not Found"}；把它当信封会让 code 变成 undefined。
    expect(isErrorEnvelope({ detail: 'Not Found' })).toBe(false)
    expect(isErrorEnvelope({ error: { code: 1, type: 'T', message: 'm' } })).toBe(false)
    expect(isErrorEnvelope({ error: null })).toBe(false)
    expect(isErrorEnvelope({ error: 'x' })).toBe(false)
    expect(isErrorEnvelope([])).toBe(false)
    expect(isErrorEnvelope('boom')).toBe(false)
    expect(isErrorEnvelope(null)).toBe(false)
    expect(isErrorEnvelope(undefined)).toBe(false)
  })
})
