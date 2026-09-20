import { describe, expect, it } from 'vitest'

import type { SearchResponse } from './api-types'
import {
  DEFAULT_SEARCH_MODE,
  SEARCH_MODES,
  describeFilters,
  formatScore,
  hasFilters,
  isDegraded,
  parseMode,
  presentWarnings,
} from './search'

// --------------------------------------------------------------------------- //
// SEARCH_MODES
// --------------------------------------------------------------------------- //

describe('SEARCH_MODES', () => {
  it('contains all four modes in the order the UI presents them', () => {
    expect(SEARCH_MODES.map((m) => m.value)).toEqual([
      'keyword',
      'hybrid',
      'semantic',
      'graph',
    ])
  })

  it('has readable Chinese labels', () => {
    expect(SEARCH_MODES.map((m) => m.label)).toEqual([
      '关键词',
      '混合',
      '语义',
      '图谱',
    ])
  })
})

// --------------------------------------------------------------------------- //
// parseMode
// --------------------------------------------------------------------------- //

describe('parseMode', () => {
  it('accepts every declared mode', () => {
    for (const mode of SEARCH_MODES) {
      expect(parseMode(mode.value)).toBe(mode.value)
    }
  })

  it('falls back to the default for null / undefined / empty', () => {
    expect(parseMode(null)).toBe(DEFAULT_SEARCH_MODE)
    expect(parseMode(undefined)).toBe(DEFAULT_SEARCH_MODE)
    expect(parseMode('')).toBe(DEFAULT_SEARCH_MODE)
  })

  it('falls back for an unknown value instead of passing it through', () => {
    // 地址栏可以被手改。放行非法值会让后端返回 422，而用户看到的是一个
    // 自己没打过的请求失败。
    expect(parseMode('fuzzy')).toBe(DEFAULT_SEARCH_MODE)
    expect(parseMode('KEYWORD')).toBe(DEFAULT_SEARCH_MODE)
  })
})

// --------------------------------------------------------------------------- //
// formatScore
// --------------------------------------------------------------------------- //

describe('formatScore', () => {
  it('shows keyword BM25 as a tiny fixed-point number', () => {
    // 实测 keyword 模式下 Redis 笔记的 BM25 约为 2.02e-6。
    // JS toPrecision(3) 对这个量级输出固定小数位而非科学计数法。
    expect(formatScore(2.017850851438638e-6)).toBe('0.00000202')
  })

  it('shows hybrid RRF in fixed-point because it is around 0.016', () => {
    expect(formatScore(0.016129032258064516)).toBe('0.0161')
  })

  it('shows semantic cosine similarity in familiar fixed-point', () => {
    expect(formatScore(0.95)).toBe('0.950')
  })

  it('never produces a percentage', () => {
    const formatted = formatScore(0.95)
    expect(formatted).not.toContain('%')
  })
})

// --------------------------------------------------------------------------- //
// hasFilters
// --------------------------------------------------------------------------- //

describe('hasFilters', () => {
  it('is false for undefined', () => {
    expect(hasFilters(undefined)).toBe(false)
  })

  it('is false for an empty object', () => {
    expect(hasFilters({})).toBe(false)
  })

  it('is true when tags are present', () => {
    expect(hasFilters({ tags: ['redis'] })).toBe(true)
  })

  it('is true when a folder is set', () => {
    expect(hasFilters({ folder: 'Guides' })).toBe(true)
  })

  it('is false when folder is only whitespace', () => {
    expect(hasFilters({ folder: '   ' })).toBe(false)
  })

  it('is true when frontmatter is set', () => {
    expect(hasFilters({ frontmatter: { status: 'draft' } })).toBe(true)
  })
})

// --------------------------------------------------------------------------- //
// describeFilters
// --------------------------------------------------------------------------- //

describe('describeFilters', () => {
  it('returns empty array for empty filters', () => {
    expect(describeFilters({})).toEqual([])
  })

  it('describes a tag filter', () => {
    expect(describeFilters({ tags: ['redis', 'cache'] })).toContain('标签: redis, cache')
  })

  it('describes a folder filter', () => {
    expect(describeFilters({ folder: 'Guides' })).toContain('目录: Guides')
  })

  it('ignores whitespace-only folder', () => {
    expect(describeFilters({ folder: '   ' })).toEqual([])
  })
})

// --------------------------------------------------------------------------- //
// isDegraded / presentWarnings
// --------------------------------------------------------------------------- //

function responseWith(warnings: string[]): SearchResponse {
  return {
    results: [],
    warnings,
    semantic: null,
  }
}

describe('isDegraded', () => {
  it('is false when there are no warnings', () => {
    expect(isDegraded(responseWith([]))).toBe(false)
  })

  it('is true when warnings exist', () => {
    expect(isDegraded(responseWith(['Semantic backend unavailable']))).toBe(true)
  })
})

describe('presentWarnings', () => {
  it('returns the warnings as-is', () => {
    const warnings = ['Index missing']
    expect(presentWarnings(responseWith(warnings))).toEqual(warnings)
  })
})
