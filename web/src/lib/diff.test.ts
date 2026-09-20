import { describe, expect, it } from 'vitest'

import type { DiffLine, DiffSummaryItem } from './api-types'
import {
  computeDiffTotals,
  describeOperation,
  groupDiffByFile,
  truncateDiffLines,
} from './diff'

describe('describeOperation', () => {
  it('maps known operations to proper metadata', () => {
    expect(describeOperation('create')).toEqual({
      label: '新建',
      variant: 'default',
      tone: 'emerald',
      description: '在 Vault 中创建新笔记',
    })

    expect(describeOperation('replace')).toEqual({
      label: '修改',
      variant: 'secondary',
      tone: 'blue',
      description: '更新笔记正文内容',
    })

    expect(describeOperation('move')).toEqual({
      label: '移动',
      variant: 'outline',
      tone: 'blue',
      description: '重命名或移动笔记路径',
    })

    expect(describeOperation('trash')).toEqual({
      label: '移至废纸篓',
      variant: 'destructive',
      tone: 'rose',
      description: '安全移至废纸篓目录，支持事后回滚',
    })

    expect(describeOperation('rewrite_backlinks')).toEqual({
      label: '重写双链',
      variant: 'outline',
      tone: 'amber',
      description: '随移动操作同步更新引用了该笔记的反向链接',
    })
  })

  it('gracefully handles unknown operations', () => {
    const custom = describeOperation('custom_action')
    expect(custom.label).toBe('custom_action')
    expect(custom.variant).toBe('secondary')
    expect(custom.tone).toBe('slate')
  })
})

describe('computeDiffTotals', () => {
  it('returns zeroes for empty summary list', () => {
    expect(computeDiffTotals([])).toEqual({
      fileCount: 0,
      addedLines: 0,
      removedLines: 0,
    })
  })

  it('aggregates multiple items accurately', () => {
    const items: DiffSummaryItem[] = [
      {
        path: 'Note1.md',
        operation: 'create',
        destination: null,
        added_lines: 15,
        removed_lines: 0,
      },
      {
        path: 'Note2.md',
        operation: 'replace',
        destination: null,
        added_lines: 4,
        removed_lines: 7,
      },
      {
        path: 'Note3.md',
        operation: 'trash',
        destination: '.obsai-trash/uuid/Note3.md',
        added_lines: 0,
        removed_lines: 20,
      },
    ]

    expect(computeDiffTotals(items)).toEqual({
      fileCount: 3,
      addedLines: 19,
      removedLines: 27,
    })
  })
})

describe('groupDiffByFile', () => {
  it('returns empty array when diff is empty', () => {
    expect(groupDiffByFile([])).toEqual([])
  })

  it('returns single chunk directly when affectedPaths has length 1', () => {
    const lines: DiffLine[] = [
      { text: '@@ -1,3 +1,3 @@', style: 'hunk', highlight: false },
      { text: '-old line', style: 'removed', highlight: false },
      { text: '+new line', style: 'added', highlight: false },
    ]
    const grouped = groupDiffByFile(lines, ['Solo.md'])
    expect(grouped).toHaveLength(1)
    expect(grouped[0]?.path).toBe('Solo.md')
    expect(grouped[0]?.lines).toEqual(lines)
  })

  it('splits multi-file diff by header markers', () => {
    const lines: DiffLine[] = [
      { text: '--- a/File1.md', style: null, highlight: false },
      { text: '+++ b/File1.md', style: null, highlight: false },
      { text: '+added in file 1', style: 'added', highlight: false },
      { text: '--- a/File2.md', style: null, highlight: false },
      { text: '+++ b/File2.md', style: null, highlight: false },
      { text: '-removed in file 2', style: 'removed', highlight: false },
    ]

    const grouped = groupDiffByFile(lines, ['File1.md', 'File2.md'])
    expect(grouped).toHaveLength(2)
    expect(grouped[0]?.path).toBe('File1.md')
    expect(grouped[0]?.lines).toHaveLength(3)
    expect(grouped[1]?.path).toBe('File2.md')
    expect(grouped[1]?.lines).toHaveLength(3)
  })
})

describe('truncateDiffLines', () => {
  it('does not truncate if lines are within threshold', () => {
    const lines: DiffLine[] = [
      { text: 'line 1', style: null, highlight: false },
      { text: 'line 2', style: null, highlight: false },
    ]

    const result = truncateDiffLines(lines, 10)
    expect(result.isTruncated).toBe(false)
    expect(result.totalCount).toBe(2)
    expect(result.remainingCount).toBe(0)
    expect(result.displayedLines).toHaveLength(2)
  })

  it('truncates lines and calculates hidden count when exceeding limit', () => {
    const lines: DiffLine[] = Array.from({ length: 1500 }, (_, i) => ({
      text: `line ${i}`,
      style: null,
      highlight: false,
    }))

    const result = truncateDiffLines(lines, 1000)
    expect(result.isTruncated).toBe(true)
    expect(result.totalCount).toBe(1500)
    expect(result.remainingCount).toBe(500)
    expect(result.displayedLines).toHaveLength(1000)
    expect(result.displayedLines[0]?.text).toBe('line 0')
    expect(result.displayedLines[999]?.text).toBe('line 999')
  })
})
