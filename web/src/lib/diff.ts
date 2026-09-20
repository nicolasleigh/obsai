/**
/**
 * Diff 解析、文件分组、增删统计与大文件截断保护纯逻辑。
 *
 * 与 React 渲染解耦，以便在纯 Node 环境下（无需 DOM / jsdom）运行高覆盖率的 Vitest 单元测试。
 */

import type { DiffLine, DiffSummaryItem } from './api-types'

export type OperationTone = 'emerald' | 'amber' | 'blue' | 'rose' | 'slate'

export type OperationMeta = {
  /** 中文标签 */
  label: string
  /** 语义色彩变体 */
  variant: 'default' | 'secondary' | 'destructive' | 'outline'
  /** 颜色基调 */
  tone: OperationTone
  /** 简述说明 */
  description: string
}

/**
 * 操作类型到中文及视觉呈现的映射。
 */
export function describeOperation(operation: string): OperationMeta {
  switch (operation) {
    case 'create':
      return {
        label: '新建',
        variant: 'default',
        tone: 'emerald',
        description: '在 Vault 中创建新笔记',
      }
    case 'update':
    case 'replace':
      return {
        label: '修改',
        variant: 'secondary',
        tone: 'blue',
        description: '更新笔记正文内容',
      }
    case 'append':
      return {
        label: '追加',
        variant: 'secondary',
        tone: 'blue',
        description: '向笔记末尾追加内容',
      }
    case 'frontmatter':
      return {
        label: '属性',
        variant: 'outline',
        tone: 'amber',
        description: '修改笔记 YAML 属性区',
      }
    case 'move':
      return {
        label: '移动',
        variant: 'outline',
        tone: 'blue',
        description: '重命名或移动笔记路径',
      }
    case 'trash':
      return {
        label: '移至废纸篓',
        variant: 'destructive',
        tone: 'rose',
        description: '安全移至废纸篓目录，支持事后回滚',
      }
    case 'rewrite_backlinks':
      return {
        label: '重写双链',
        variant: 'outline',
        tone: 'amber',
        description: '随移动操作同步更新引用了该笔记的反向链接',
      }
    default:
      return {
        label: operation,
        variant: 'secondary',
        tone: 'slate',
        description: `执行 ${operation} 操作`,
      }
  }
}

/**
 * 汇总多文件变更统计指标。
 */
export function computeDiffTotals(summary: readonly DiffSummaryItem[]): {
  fileCount: number
  addedLines: number
  removedLines: number
} {
  return summary.reduce(
    (acc, item) => ({
      fileCount: acc.fileCount + 1,
      addedLines: acc.addedLines + (item.added_lines ?? 0),
      removedLines: acc.removedLines + (item.removed_lines ?? 0),
    }),
    { fileCount: 0, addedLines: 0, removedLines: 0 },
  )
}

export type FileDiffChunk = {
  path: string
  lines: DiffLine[]
}

/**
 * 从单行统一 Diff 中提取关联的文件路径。
 * 格式通常为 `--- a/path`、`+++ b/path`。
 */
function extractPathFromHeader(text: string): string | null {
  if (text.startsWith('+++ b/') || text.startsWith('--- a/')) {
    const raw = text.slice(6).trim()
    return raw !== '/dev/null' ? raw : null
  }
  return null
}

/**
 * 将整组扁平的统一 Diff 行按照文件分段归组。
 *
 * @param diff 全部 diff 行
 * @param affectedPaths 计划中受影响的文件列表（保持顺序）
 */
export function groupDiffByFile(
  diff: readonly DiffLine[],
  affectedPaths: readonly string[] = [],
): FileDiffChunk[] {
  if (diff.length === 0) {
    return []
  }

  // 若只有一个受影响文件，直接归为该文件
  if (affectedPaths.length === 1) {
    return [{ path: affectedPaths[0] ?? 'unnamed', lines: [...diff] }]
  }

  const chunks: FileDiffChunk[] = []
  let currentPath: string | null = null
  let currentLines: DiffLine[] = []

  for (const line of diff) {
    const headerPath = extractPathFromHeader(line.text)
    if (headerPath !== null && headerPath !== currentPath) {
      if (currentPath !== null && currentLines.length > 0) {
        chunks.push({ path: currentPath, lines: currentLines })
      }
      currentPath = headerPath
      currentLines = [line]
    } else {
      currentLines.push(line)
    }
  }

  if (currentPath !== null && currentLines.length > 0) {
    chunks.push({ path: currentPath, lines: currentLines })
  }

  // 兜底：若未能通过 ---/+++ 识别出任何文件头，将整体作为一个 chunk 返回
  if (chunks.length === 0) {
    return [{ path: affectedPaths[0] ?? 'all', lines: [...diff] }]
  }

  return chunks
}

export type TruncatedDiff = {
  displayedLines: DiffLine[]
  totalCount: number
  isTruncated: boolean
  remainingCount: number
}

export const DEFAULT_MAX_LINES = 1000

/**
 * 对超大 Diff 行数实施安全截断保护，防止浏览器在单次渲染海量 DOM 时掉帧或无响应。
 *
 * @param lines 全部待展示行
 * @param maxLines 允许渲染的最大行数，默认为 1000
 */
export function truncateDiffLines(
  lines: readonly DiffLine[],
  maxLines: number = DEFAULT_MAX_LINES,
): TruncatedDiff {
  const totalCount = lines.length
  if (totalCount <= maxLines) {
    return {
      displayedLines: [...lines],
      totalCount,
      isTruncated: false,
      remainingCount: 0,
    }
  }

  return {
    displayedLines: lines.slice(0, maxLines),
    totalCount,
    isTruncated: true,
    remainingCount: totalCount - maxLines,
  }
}
