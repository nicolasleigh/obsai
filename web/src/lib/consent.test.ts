import { describe, expect, it } from 'vitest'

import type { ConsentApproval, RemoteConsent, SearchResponse } from './api-types'
import {
  consentFromError,
  consentOutcome,
  describeConsent,
  failureNotice,
  formatCost,
  formatTokens,
  isApprovalNotice,
  minutesUntil,
} from './consent'
import { ApiError } from './errors'

const CONSENT: RemoteConsent = {
  consent_id: 'c1',
  query_hash: 'q1',
  generation_id: 'openai:text-embedding-3-small:1536',
  expires_at: '2026-09-15T12:15:00+00:00',
  query_tokens: 4,
  estimated_cost_usd: '1E-7',
  request_count: 1,
}

const APPROVAL: ConsentApproval = {
  consent_id: 'c1',
  query_hash: 'q1',
  generation_id: 'openai:text-embedding-3-small:1536',
  nonce: 'n1',
  approved_at: '2026-09-15T12:00:00+00:00',
  approved: true,
}

/** 服务端在"没有有效批准"时的原话，与 `application/search.py` 的常量一致。 */
const NOT_APPROVED = 'Semantic query was not approved'

function response(overrides: Partial<SearchResponse> = {}): SearchResponse {
  return {
    results: [],
    warnings: [],
    semantic: { consent: CONSENT, reason: '', failure: null },
    ...overrides,
  }
}

function unavailable(failure: 'index_missing' | 'backend_unavailable'): SearchResponse {
  return response({
    warnings: ['Semantic index missing; run \'obsai index embeddings\''],
    semantic: { consent: null, reason: 'whatever', failure },
  })
}

// --------------------------------------------------------------------------- //
// consentOutcome
// --------------------------------------------------------------------------- //

describe('consentOutcome', () => {
  it('在还没发请求时说不需要同意', () => {
    expect(consentOutcome(undefined, null)).toEqual({ kind: 'not_needed' })
  })

  it('keyword 模式没有探针，所以也不需要同意', () => {
    expect(consentOutcome(response({ semantic: null }), null)).toEqual({ kind: 'not_needed' })
  })

  it('有挑战且没批准过，就是该弹对话框的状态', () => {
    expect(consentOutcome(response(), null)).toEqual({ kind: 'pending', consent: CONSENT })
  })

  it('批准过且服务端接受了，就不再打扰用户', () => {
    expect(consentOutcome(response(), APPROVAL).kind).toBe('approved')
  })

  it('批准过但服务端仍说未批准，说明这次批准已经不适用', () => {
    const stale = response({ warnings: [NOT_APPROVED] })

    expect(consentOutcome(stale, APPROVAL).kind).toBe('rejected')
  })

  it('批准生效但语义腿失败时不算被拒绝', () => {
    // 与上一条的区别只在那句话：后端故障是"批准生效了，是服务出问题"，
    // 报成"批准失效"会让用户去重新批准一个本来有效的决定。
    const failed = response({ warnings: ['Semantic backend failed (ConfigError: no key)'] })

    expect(consentOutcome(failed, APPROVAL).kind).toBe('approved')
  })

  it('没有挑战时按结构化成因说明为什么不能批准', () => {
    expect(consentOutcome(unavailable('index_missing'), null)).toEqual({
      kind: 'unavailable',
      failure: 'index_missing',
    })
    expect(consentOutcome(unavailable('backend_unavailable'), null)).toEqual({
      kind: 'unavailable',
      failure: 'backend_unavailable',
    })
  })

  it('本地 provider 的空同意字段不显示批准按钮', () => {
    // Ollama 不会把查询发出本机；与远程 provider 不同，它有意返回两个空字段。
    const local = response({ semantic: { consent: null, reason: '', failure: null } })

    expect(consentOutcome(local, null)).toEqual({ kind: 'not_needed' })
  })
})

// --------------------------------------------------------------------------- //
// consentFromError
// --------------------------------------------------------------------------- //

describe('consentFromError', () => {
  function error(code: string, details?: unknown): ApiError {
    return new ApiError({ code, type: 'T', message: 'm', status: 409, requestId: 'r', details })
  }

  it('从 409 里取出挑战', () => {
    expect(consentFromError(error('consent_required', { consent: CONSENT }))).toEqual(CONSENT)
  })

  it('别的错误码一律返回 null', () => {
    expect(consentFromError(error('config', { consent: CONSENT }))).toBeNull()
  })

  it('没有 details 时返回 null', () => {
    expect(consentFromError(error('consent_required'))).toBeNull()
  })

  it('details 形状不对时返回 null，而不是渲染出一串 undefined', () => {
    expect(consentFromError(error('consent_required', { consent: { consent_id: 'c1' } }))).toBeNull()
    expect(consentFromError(error('consent_required', { consent: 'nope' }))).toBeNull()
    expect(consentFromError(error('consent_required', 'nope'))).toBeNull()
  })
})

// --------------------------------------------------------------------------- //
// 数字的写法
// --------------------------------------------------------------------------- //

describe('formatTokens', () => {
  it('带上单位，因为单独一个数字没有量纲', () => {
    expect(formatTokens(4)).toBe('4 个 token')
    expect(formatTokens(0)).toBe('0 个 token')
  })
})

describe('formatCost', () => {
  it('把科学计数法还原成美元', () => {
    // 服务端的 Decimal 序列化出来就是 "1E-7"。
    expect(formatCost('1E-7')).not.toContain('E')
  })

  it('小于一微美元时说"不足"，而不是显示成 0', () => {
    // `Intl.NumberFormat` 会给出 "$0.00"，把"很便宜"变成"免费"——而这个对话框
    // 存在的意义就是让用户看见价格。
    expect(formatCost('1E-7')).toBe('不足 $0.000001')
    expect(formatCost('0')).toBe('$0')
  })

  it('正常量级保留六位小数', () => {
    expect(formatCost('0.000123')).toBe('$0.000123')
    expect(formatCost('0.012')).toBe('$0.012000')
  })

  it('读不出来的值原样返回，好过显示 NaN', () => {
    expect(formatCost('not-a-number')).toBe('not-a-number')
  })
})

describe('minutesUntil', () => {
  const now = new Date('2026-09-15T12:00:00Z')

  it('给出还剩几分钟', () => {
    expect(minutesUntil('2026-09-15T12:15:00Z', now)).toBe(15)
  })

  it('过期后是负数，调用方据此判断而不是自己算', () => {
    expect(minutesUntil('2026-09-15T11:00:00Z', now)).toBe(-60)
  })

  it('时间戳读不出来时返回 null', () => {
    expect(minutesUntil('whenever', now)).toBeNull()
  })
})

// --------------------------------------------------------------------------- //
// 对话框的内容
// --------------------------------------------------------------------------- //

describe('describeConsent', () => {
  it('第一行是查询原文', () => {
    // 对话框的职责是让用户看清什么东西将要离开本机，而那件东西就是查询文本。
    const lines = describeConsent(CONSENT, 'redis 缓存策略')

    expect(lines[0]).toEqual({ label: '将发送的查询', value: 'redis 缓存策略' })
  })

  it('tokens 与成本都在，且成本已经是美元写法', () => {
    const values = describeConsent(CONSENT, 'q').map((line) => line.value)

    expect(values).toContain('4 个 token')
    expect(values).toContain('不足 $0.000001')
  })

  it('请求数也列出来，因为成本是按它估的', () => {
    const lines = describeConsent({ ...CONSENT, request_count: 2 }, 'q')

    expect(lines.find((line) => line.label === '请求数')?.value).toBe('2')
  })
})

describe('failureNotice', () => {
  it('索引缺向量时给出那条命令', () => {
    expect(failureNotice('index_missing')).toContain('obsai index embeddings')
  })

  it('后端不可用时说的是后端，不是索引', () => {
    expect(failureNotice('backend_unavailable')).toContain('后端')
    expect(failureNotice('backend_unavailable')).not.toContain('index embeddings')
  })
})

describe('isApprovalNotice', () => {
  it('认出服务端"还没批准"那条原文', () => {
    expect(isApprovalNotice(NOT_APPROVED)).toBe(true)
  })

  // 判据必须是"是不是那条原文"，不能放宽成"提到语义"。放宽之后索引缺向量与后端故障
  // 都会被滤掉——那两条是用户批准不掉的，滤掉就等于把真故障从页面上删了。
  it('真故障的提示不算同意问题', () => {
    expect(isApprovalNotice('Semantic index missing; run \'obsai index embeddings\'')).toBe(
      false,
    )
    expect(isApprovalNotice('Semantic backend unavailable (ConnectError: refused)')).toBe(false)
  })
})
