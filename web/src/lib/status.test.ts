import { describe, expect, it } from 'vitest'

import type { IndexStatusView, StatusResponse, StatusView } from './api-types'
import { ApiError } from './errors'
import {
  indexIsSearchable,
  summarizeBackend,
  summarizeDirtyNotes,
  summarizeIndex,
  summarizeLock,
  summarizeRecovery,
  summarizeVault,
  vaultIsReadable,
  worstTone,
} from './status'

/**
 * 六种降级状态在服务端都是**数据**而不是异常（计划 §17.2 决策 1）。这一组测试逐个
 * 把它们翻成中文，确认界面上出现的每一句话都说清了"现在怎样、下一步做什么"。
 */

function indexView(overrides: Partial<IndexStatusView> = {}): IndexStatusView {
  return {
    path: '/tmp/obsai/index.db',
    exists: true,
    usable: true,
    error: null,
    note_count: 9,
    chunk_count: 80,
    vector_count: 80,
    generation: 'gen-1',
    semantic_ready: true,
    dirty_notes: [],
    ...overrides,
  }
}

function statusView(overrides: Partial<StatusView> = {}): StatusView {
  return {
    version: '0.1.0',
    vault_path: '/tmp/vault',
    vault_ready: true,
    index: indexView(),
    unfinished_transactions: [],
    index_dirty_transactions: [],
    recovery_required: false,
    locked: false,
    ...overrides,
  }
}

function response(overrides: Partial<StatusResponse> = {}): StatusResponse {
  return { ...statusView(overrides), started_at: '2026-09-14T00:00:00Z', uptime_seconds: 12.5 }
}

describe('worstTone', () => {
  it('取最严重的一个', () => {
    expect(worstTone(['ok', 'warning', 'ok'])).toBe('warning')
    expect(worstTone(['warning', 'danger'])).toBe('danger')
  })

  it('unknown 比 ok 轻——它只是"还没读到"', () => {
    expect(worstTone(['unknown', 'ok'])).toBe('ok')
    expect(worstTone([])).toBe('ok')
  })
})

describe('Vault', () => {
  it('未配置时引导去写 config.toml', () => {
    const summary = summarizeVault(statusView({ vault_path: null, vault_ready: false }))
    expect(summary.tone).toBe('warning')
    expect(summary.label).toBe('未配置 Vault')
    expect(summary.hint).toContain('config.toml')
  })

  it('配置了但目录不存在时报出那个路径', () => {
    const summary = summarizeVault(statusView({ vault_ready: false }))
    expect(summary.tone).toBe('danger')
    expect(summary.label).toBe('Vault 目录不存在')
    expect(summary.hint).toContain('/tmp/vault')
  })

  it('正常时不写提示', () => {
    expect(summarizeVault(statusView())).toEqual({
      tone: 'ok',
      label: 'Vault 就绪',
      hint: '',
    })
  })
})

describe('索引', () => {
  it('未构建时指向 index update', () => {
    const summary = summarizeIndex(indexView({ exists: false, usable: false, note_count: 0 }))
    expect(summary.tone).toBe('warning')
    expect(summary.hint).toContain('obsai index update')
  })

  it('存在但打不开时把 sqlite 的原因原样带出来', () => {
    const summary = summarizeIndex(
      indexView({ usable: false, error: 'file is not a database', note_count: 0 }),
    )
    expect(summary.tone).toBe('danger')
    expect(summary.hint).toBe('file is not a database')
  })

  it('打不开且没有原因时有兜底文案', () => {
    expect(summarizeIndex(indexView({ usable: false, note_count: 0 })).hint).not.toBe('')
  })

  it('空索引提示去收录笔记', () => {
    const summary = summarizeIndex(indexView({ note_count: 0, chunk_count: 0, vector_count: 0 }))
    expect(summary.tone).toBe('warning')
    expect(summary.label).toBe('索引为空')
  })

  it('有笔记但没有向量时说明会降级为关键词检索', () => {
    const summary = summarizeIndex(indexView({ semantic_ready: false, vector_count: 0 }))
    expect(summary.tone).toBe('warning')
    expect(summary.label).toBe('已索引 9 篇笔记')
    expect(summary.hint).toContain('关键词')
    expect(summary.hint).toContain('obsai index embeddings')
  })

  it('全好时不写提示', () => {
    expect(summarizeIndex(indexView())).toEqual({
      tone: 'ok',
      label: '已索引 9 篇笔记',
      hint: '',
    })
  })
})

describe('事务与恢复', () => {
  it('有待恢复事务时明确说写入已被冻结', () => {
    const summary = summarizeRecovery(
      statusView({
        recovery_required: true,
        unfinished_transactions: [
          { transaction_id: 'tx-1', status: 'applying', originals: [] },
          { transaction_id: 'tx-2', status: 'prepared', originals: [] },
        ],
      }),
    )
    expect(summary.tone).toBe('danger')
    expect(summary.label).toBe('有 2 个未完成的写入事务')
    expect(summary.hint).toContain('obsai transaction recover')
  })

  it('recovery_required 为真但没有日志时不当成灾难', () => {
    expect(summarizeRecovery(statusView({ recovery_required: true })).tone).toBe('ok')
  })

  it('只差索引的事务是警告而非灾难', () => {
    const summary = summarizeRecovery(
      statusView({
        index_dirty_transactions: [{ transaction_id: 'tx-3', status: 'index_dirty', originals: [] }],
      }),
    )
    expect(summary.tone).toBe('warning')
    expect(summary.label).toBe('有 1 个事务待补索引')
  })

  it('干净时说无待恢复事务，且不写提示', () => {
    expect(summarizeRecovery(statusView())).toEqual({
      tone: 'ok',
      label: '无待恢复事务',
      hint: '',
    })
  })

  it('未配置 Vault 时说"无法检查"，不说"没有事务"', () => {
    // read_status() 在 vault_ready 为假时根本不读 journal，空列表并不等于"干净"。
    const summary = summarizeRecovery(statusView({ vault_path: null, vault_ready: false }))
    expect(summary.tone).toBe('unknown')
    expect(summary.label).toBe('无法检查事务状态')
    expect(summary.hint).toContain('未配置 Vault')
  })

  it('Vault 目录打不开时也区分开，并指向 Vault', () => {
    const summary = summarizeRecovery(statusView({ vault_ready: false }))
    expect(summary.tone).toBe('unknown')
    expect(summary.hint).toContain('Vault')
  })
})

describe('待重新索引的笔记', () => {
  it('没有脏笔记时安静', () => {
    expect(summarizeDirtyNotes(indexView())).toEqual({
      tone: 'ok',
      label: '没有待重新索引的笔记',
      hint: '',
    })
  })

  it('有脏笔记时给出数量与补索引的命令', () => {
    const summary = summarizeDirtyNotes(
      indexView({
        dirty_notes: [
          { path: 'a.md', reason: 'mtime', marked_at: '2026-09-14T00:00:00Z' },
          { path: 'b.md', reason: 'mtime', marked_at: '2026-09-14T00:00:00Z' },
        ],
      }),
    )
    expect(summary.tone).toBe('warning')
    expect(summary.label).toBe('有 2 篇笔记待重新索引')
    expect(summary.hint).toContain('obsai index update')
  })

  it('索引打不开时不假装清单是空的', () => {
    // dirty_notes 的默认值是空元组，与"确实没有脏笔记"无法区分。
    const summary = summarizeDirtyNotes(indexView({ usable: false, dirty_notes: [] }))
    expect(summary.tone).toBe('unknown')
    expect(summary.label).toBe('无法检查待重新索引的笔记')
  })
})

describe('vaultIsReadable', () => {
  it('未配置、目录不存在都算不可读', () => {
    expect(vaultIsReadable(statusView())).toBe(true)
    expect(vaultIsReadable(statusView({ vault_path: null, vault_ready: false }))).toBe(false)
    expect(vaultIsReadable(statusView({ vault_ready: false }))).toBe(false)
  })
})

describe('写入锁', () => {
  it('持锁时说明是另一个进程', () => {
    const summary = summarizeLock(statusView({ locked: true }))
    expect(summary.tone).toBe('warning')
    expect(summary.hint).toContain('obsai')
  })

  it('空闲时安静', () => {
    expect(summarizeLock(statusView()).label).toBe('写入锁空闲')
  })
})

describe('后端整体状况', () => {
  it('首次加载时报"正在连接"', () => {
    expect(summarizeBackend({ data: undefined, error: null })).toEqual({
      tone: 'unknown',
      label: '正在连接后端 ...',
      hint: '',
    })
  })

  it('后端没启动时给出启动方式', () => {
    const summary = summarizeBackend({ data: undefined, error: new TypeError('Failed to fetch') })
    expect(summary.tone).toBe('danger')
    expect(summary.label).toBe('无法连接到本地服务')
    expect(summary.hint).toContain('./scripts/dev.sh')
  })

  it('服务端错误走同一张中文目录', () => {
    const summary = summarizeBackend({
      data: undefined,
      error: new ApiError({
        code: 'config',
        type: 'ConfigError',
        message: 'Configured vault does not exist',
        status: 400,
      }),
    })
    expect(summary.label).toBe('配置不可用')
    expect(summary.hint).toContain('config.toml')
  })

  it('全部正常时只说已连接', () => {
    expect(summarizeBackend({ data: response(), error: null })).toEqual({
      tone: 'ok',
      label: '后端已连接',
      hint: '',
    })
  })

  it('未配置 Vault 时状态灯直接说这件事', () => {
    const summary = summarizeBackend({
      data: response({ vault_path: null, vault_ready: false }),
      error: null,
    })
    expect(summary.tone).toBe('warning')
    expect(summary.label).toBe('未配置 Vault')
  })

  it('笔记待重新索引时状态灯不再说"一切正常"', () => {
    // 索引可用、有向量、没有待恢复事务——只有脏笔记。少了 summarizeDirtyNotes，
    // 这一整组字段会合出"后端已连接"。
    const summary = summarizeBackend({
      data: response({
        index: indexView({
          dirty_notes: [{ path: 'a.md', reason: 'mtime', marked_at: '2026-09-14T00:00:00Z' }],
        }),
      }),
      error: null,
    })
    expect(summary.tone).toBe('warning')
    expect(summary.label).toBe('有 1 篇笔记待重新索引')
  })

  it('挑出最严重的那个问题，而不是列一堆', () => {
    const summary = summarizeBackend({
      data: response({ vault_ready: false, locked: true }),
      error: null,
    })
    expect(summary.tone).toBe('danger')
    expect(summary.label).toBe('Vault 目录不存在')
  })

  it('重取失败时以错误为准，不沿用上一次的成功数据', () => {
    const summary = summarizeBackend({ data: response(), error: new TypeError('Failed to fetch') })
    expect(summary.tone).toBe('danger')
    expect(summary.label).toBe('无法连接到本地服务')
  })
})

describe('indexIsSearchable', () => {
  it('可用且有笔记才算可检索', () => {
    expect(indexIsSearchable(indexView())).toBe(true)
    expect(indexIsSearchable(indexView({ note_count: 0 }))).toBe(false)
    expect(indexIsSearchable(indexView({ usable: false }))).toBe(false)
  })
})
