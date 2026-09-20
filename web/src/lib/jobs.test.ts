import { describe, expect, it } from 'vitest'

import type { JobStatus, JobView } from './api-types'
import { codeFromType, wordingFor } from './errors'
import {
  EMBEDDINGS_STALE_HINT,
  embeddingsAreStale,
  jobCanBeCancelled,
  jobCounts,
  jobFailure,
  jobHasUnknownStep,
  jobIsTerminal,
  jobKindLabel,
  jobProgress,
  jobStatusLabel,
  jobStepLabel,
  mergeJobs,
  sortJobs,
  summarizeJob,
} from './jobs'

/**
 * 任务记录里没有一句给人看的话：`kind` 是标识符、`message` 是步骤名、`detail` 是标量。
 * 这一组测试逐个把它们翻成中文，并钉住"未知的报 null 而不是报空白"这条规矩。
 */

function jobView(overrides: Partial<JobView> = {}): JobView {
  return {
    job_id: 'j1',
    kind: 'index-update',
    status: 'running',
    created_at: '2026-09-15T12:00:00Z',
    started_at: '2026-09-15T12:00:01Z',
    finished_at: null,
    message: 'scanning',
    detail: {},
    error: null,
    error_code: null,
    ...overrides,
  }
}

const ALL_STATUSES: readonly JobStatus[] = [
  'queued',
  'running',
  'awaiting_approval',
  'succeeded',
  'failed',
  'cancelled',
  'interrupted',
]

describe('词表', () => {
  it('认识索引任务与向量任务，未知的 kind 报 null', () => {
    expect(jobKindLabel(jobView({ kind: 'index-update' }))).toBe('索引更新')
    expect(jobKindLabel(jobView({ kind: 'index-rebuild' }))).toBe('索引重建')
    expect(jobKindLabel(jobView({ kind: 'embedding' }))).toBe('向量生成')
    // null 而不是原文：调用方必须自己决定怎么显示，不能悄悄吞掉。
    expect(jobKindLabel(jobView({ kind: 'unknown_kind' }))).toBeNull()
  })

  it('七个状态都有中文，且互不相同', () => {
    const labels = ALL_STATUSES.map((status) => jobStatusLabel(jobView({ status })))
    expect(labels.every((label) => label.length > 0)).toBe(true)
    expect(new Set(labels).size).toBe(ALL_STATUSES.length)
  })

  it('四个步骤都有中文，未知的步骤报 null', () => {
    expect(jobStepLabel(jobView({ message: 'scanning' }))).toBe('正在扫描 Vault')
    expect(jobStepLabel(jobView({ message: 'synchronizing' }))).toBe('正在同步笔记')
    expect(jobStepLabel(jobView({ message: 'building' }))).toBe('正在重建索引')
    expect(jobStepLabel(jobView({ message: 'done' }))).toBe('已完成')
    expect(jobStepLabel(jobView({ message: 'whatever' }))).toBeNull()
    // 没有步骤（排队中）与步骤不认识是两件事，前者不该被报成"未翻译"。
    expect(jobStepLabel(jobView({ message: null }))).toBeNull()
    expect(jobHasUnknownStep(jobView({ message: null }))).toBe(false)
    expect(jobHasUnknownStep(jobView({ message: 'whatever' }))).toBe(true)
  })
})

describe('终态与取消', () => {
  it('终态就是 JobView.terminal 的那四个', () => {
    expect(ALL_STATUSES.filter((status) => jobIsTerminal(jobView({ status })))).toEqual([
      'succeeded',
      'failed',
      'cancelled',
      'interrupted',
    ])
  })

  it('能取消的就是没结束的', () => {
    expect(jobCanBeCancelled(jobView({ status: 'running' }))).toBe(true)
    expect(jobCanBeCancelled(jobView({ status: 'queued' }))).toBe(true)
    expect(jobCanBeCancelled(jobView({ status: 'awaiting_approval' }))).toBe(true)
    expect(jobCanBeCancelled(jobView({ status: 'succeeded' }))).toBe(false)
    expect(jobCanBeCancelled(jobView({ status: 'interrupted' }))).toBe(false)
  })
})

describe('进度', () => {
  it('两个索引任务都是"不确定"的那一种', () => {
    // 服务端只报步骤不报分数，所以这两个 kind 永远走这一支。
    expect(jobProgress(jobView({ kind: 'index-update', message: 'synchronizing' }))).toEqual({
      kind: 'indeterminate',
    })
    expect(jobProgress(jobView({ kind: 'index-rebuild', message: 'building' }))).toEqual({
      kind: 'indeterminate',
    })
  })

  it('有分子分母时才算得出来，且不会超过分母', () => {
    expect(jobProgress(jobView({ detail: { completed: 3, total: 12 } }))).toEqual({
      kind: 'fraction',
      value: 3,
      max: 12,
    })
    expect(jobProgress(jobView({ detail: { completed: 99, total: 12 } }))).toEqual({
      kind: 'fraction',
      value: 12,
      max: 12,
    })
  })

  it('分母为 0、缺一个、或值不是数字时都退回"不确定"', () => {
    expect(jobProgress(jobView({ detail: { completed: 3, total: 0 } }))).toEqual({ kind: 'indeterminate' })
    expect(jobProgress(jobView({ detail: { completed: 3 } }))).toEqual({ kind: 'indeterminate' })
    // 字符串不 Number()：服务端发的是 JSON 数字，一个字符串说明契约漂了，强行转换
    // 只会把漂移藏起来。
    expect(jobProgress(jobView({ detail: { completed: '3', total: '12' } }))).toEqual({
      kind: 'indeterminate',
    })
  })
})

describe('计数', () => {
  it('按固定顺序取出认识的那几个键', () => {
    const counts = jobCounts(
      jobView({ detail: { created: 2, notes: 9, affected_links: 4, unchanged: 7 } }),
    )
    expect(counts.map((count) => [count.label, count.value])).toEqual([
      ['笔记', 9],
      ['新增', 2],
      ['未变', 7],
      ['受影响的链接', 4],
    ])
  })

  it('不认识的键一律不显示——包括失败时的 traceback', () => {
    const counts = jobCounts(jobView({ detail: { traceback: 'Traceback...', notes: 1 } }))
    expect(counts.map((count) => count.key)).toEqual(['notes'])
  })

  it('空 detail 得到空列表，而不是一条全是 0 的假账', () => {
    expect(jobCounts(jobView())).toEqual([])
  })
})

describe('重建后的向量提示', () => {
  it('只认 true，缺键或别的值都不算', () => {
    expect(embeddingsAreStale(jobView({ detail: { embeddings_stale: true } }))).toBe(true)
    expect(embeddingsAreStale(jobView({ detail: {} }))).toBe(false)
    expect(embeddingsAreStale(jobView({ detail: { embeddings_stale: 'true' } }))).toBe(false)
  })

  it('重建成功的那一行带着"重新生成向量"的下一步', () => {
    const summary = summarizeJob(
      jobView({ kind: 'index-rebuild', status: 'succeeded', detail: { embeddings_stale: true } }),
    )
    expect(summary.tone).toBe('ok')
    expect(summary.label).toBe('索引重建已完成')
    expect(summary.hint).toBe(EMBEDDINGS_STALE_HINT)
  })
})

describe('失败', () => {
  it('只有 failed 才有失败文案', () => {
    expect(jobFailure(jobView({ status: 'running' }))).toBeNull()
    expect(jobFailure(jobView({ status: 'cancelled' }))).toBeNull()
    expect(jobFailure(jobView({ status: 'interrupted' }))).toBeNull()
  })

  it('从异常类名反推目录里的键，并把服务端原文留在 detail', () => {
    const failure = jobFailure(
      jobView({
        status: 'failed',
        error_code: 'ConfigError',
        error: 'ConfigError: Vault directory does not exist: /tmp/nope',
      }),
    )
    expect(failure?.title).toBe('配置不可用')
    expect(failure?.detail).toBe('ConfigError: Vault directory does not exist: /tmp/nope')
  })

  it('认不出的类名退到兜底，但原文仍然留着', () => {
    const failure = jobFailure(
      jobView({ status: 'failed', error_code: 'WeirdError', error: 'WeirdError: boom' }),
    )
    expect(failure?.title).toBe('操作失败')
    expect(failure?.detail).toBe('WeirdError: boom')
  })

  it('没有 error_code 时也不编一个', () => {
    const failure = jobFailure(jobView({ status: 'failed', error: 'boom' }))
    expect(failure?.title).toBe('任务失败')
  })
})

describe('类名 → 码', () => {
  it('与 api/errors.py::error_code() 的算法一致', () => {
    // 这一张表由服务端实测输出抄来；Python 侧的契约测试对每一个 ObsAIError 子类
    // 逐个比对，服务端改了规则这里会红。
    expect(codeFromType('ConfigError')).toBe('config')
    expect(codeFromType('RecoveryRequiredError')).toBe('recovery_required')
    expect(codeFromType('LockBusyError')).toBe('lock_busy')
    expect(codeFromType('LockUnsafeError')).toBe('lock_unsafe')
    expect(codeFromType('JobNotFoundError')).toBe('job_not_found')
    expect(codeFromType('SemanticIndexMissingError')).toBe('semantic_index_missing')
    expect(codeFromType('SemanticUnavailableError')).toBe('semantic_unavailable')
    expect(codeFromType('EmbeddingRateLimitError')).toBe('embedding_rate_limit')
    expect(codeFromType('InvalidEncodingError')).toBe('invalid_encoding')
    expect(codeFromType('MissingCredentialError')).toBe('missing_credential')
    // 连续大写要粘在一起：ObsAI → obs_ai，LLM → llm。
    expect(codeFromType('ObsAIError')).toBe('obs_ai')
    expect(codeFromType('LLMError')).toBe('llm')
  })

  it('按码取文案时，未知码退到兜底并保留原文', () => {
    const presentation = wordingFor('nope', 'server said so')
    expect(presentation.title).toBe('操作失败')
    expect(presentation.detail).toBe('server said so')
  })
})

describe('一行状态', () => {
  it('七个状态都能得到一句中文，且没有空 label', () => {
    for (const status of ALL_STATUSES) {
      const summary = summarizeJob(jobView({ status }))
      expect(summary.label.length).toBeGreaterThan(0)
    }
  })

  it('进行中会带上当前步骤，没有步骤时不硬凑', () => {
    expect(summarizeJob(jobView({ status: 'running', message: 'building' })).label).toBe(
      '索引更新：正在重建索引',
    )
    expect(summarizeJob(jobView({ status: 'running', message: null })).label).toBe('索引更新进行中')
  })

  it('未知 kind 退化成原始标识符，不编名字', () => {
    expect(summarizeJob(jobView({ kind: 'custom_job', status: 'succeeded' })).label).toBe(
      'custom_job已完成',
    )
  })

  it('被中断说的是"不知道做到哪一步"，不是失败', () => {
    const summary = summarizeJob(jobView({ status: 'interrupted' }))
    expect(summary.tone).toBe('warning')
    expect(summary.hint).toContain('无法得知')
  })

  it('取消说的是"索引仍然是上一个完整版本"', () => {
    const summary = summarizeJob(jobView({ status: 'cancelled' }))
    expect(summary.tone).toBe('warning')
    expect(summary.hint).toContain('回滚')
  })

  it('不需要动作时 hint 是空串，不是"请稍候"', () => {
    expect(summarizeJob(jobView({ status: 'running' })).hint).toBe('')
    expect(summarizeJob(jobView({ status: 'succeeded' })).hint).toBe('')
  })
})

describe('流的合并', () => {
  const older = jobView({ job_id: 'a', created_at: '2026-09-15T12:00:00Z' })
  const newer = jobView({ job_id: 'b', created_at: '2026-09-15T13:00:00Z' })

  it('最近的在最前面，且不改动入参', () => {
    const input = [older, newer]
    expect(sortJobs(input).map((job) => job.job_id)).toEqual(['b', 'a'])
    expect(input.map((job) => job.job_id)).toEqual(['a', 'b'])
  })

  it('同一时刻创建时按 job_id 稳定排序', () => {
    const same = '2026-09-15T12:00:00Z'
    expect(sortJobs([jobView({ job_id: 'a', created_at: same }), jobView({ job_id: 'b', created_at: same })])
      .map((job) => job.job_id)).toEqual(['b', 'a'])
  })

  it('变化事件是 upsert：只更新提到的那几个，不动其余', () => {
    const merged = mergeJobs([older, newer], [jobView({ job_id: 'a', status: 'succeeded' })])
    expect(merged.map((job) => job.job_id)).toEqual(['b', 'a'])
    expect(merged[1].status).toBe('succeeded')
    expect(merged[0]).toBe(newer)
  })

  it('变化事件里的新 id 会被追加，而不是被当成全量', () => {
    const merged = mergeJobs([older], [newer])
    expect(merged.map((job) => job.job_id)).toEqual(['b', 'a'])
  })

  it('空的 incoming 不会清空列表——清空是快照的职责', () => {
    expect(mergeJobs([older, newer], [])).toEqual([newer, older])
  })
})
