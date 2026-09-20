/**
 * Agent 运行与工具调用状态适配层。
 *
 * 封装工具名称中文映射、状态分类、有界性上限常量与判断辅助函数。
 * 纯函数设计，便于在 Node/Vitest 环境下进行单元测试。
 */

import type { AgentRunView, AgentTimelineItem } from '@/lib/api-types'

export const AGENT_MAX_STEPS = 15
export const AGENT_MAX_RETRIEVAL_STEPS = 5

export const AGENT_STATUS_LABELS: Record<string, string> = {
  completed: '已完成',
  interrupted: '等待审批',
  failed: '执行中断',
  running: '执行中',
}

export const AGENT_TOOL_LABELS: Record<string, string> = {
  search_notes: '检索笔记',
  read_note: '读取笔记',
  get_backlinks: '反向链接',
  get_outgoing_links: '引用出链',
  create_note: '创建笔记',
  update_note: '更新笔记',
  move_note: '移动笔记',
  trash_note: '删除笔记',
  apply_write: '应用写入',
  rag_answer: '生成回答',
}

export const WRITE_TOOLS = new Set([
  'create_note',
  'update_note',
  'move_note',
  'trash_note',
  'update_frontmatter',
])

export const READ_TOOLS = new Set([
  'search_notes',
  'read_note',
  'get_backlinks',
  'get_outgoing_links',
])

/**
 * 将工具英文标识转为中文展示标签。
 */
export function formatToolName(tool: string): string {
  return AGENT_TOOL_LABELS[tool] || tool || '未知工具'
}

/**
 * 判断工具是否为 Vault 写入类工具。
 */
export function isWriteTool(tool: string): boolean {
  return WRITE_TOOLS.has(tool)
}

/**
 * 获取友好的工作流状态说明。
 */
export function formatAgentStatus(status: string): string {
  return AGENT_STATUS_LABELS[status] || status || '未知状态'
}

/**
 * 判断工作流是否已处于终止态（完成或失败，无需等待审批或后续执行）。
 */
export function isTerminalStatus(status: string): boolean {
  return status === 'completed' || status === 'failed'
}

/**
 * 判断当前工作流是否正处于等待人机审批中断状态。
 */
export function hasPendingApproval(run: AgentRunView | null | undefined): boolean {
  return Boolean(run && run.status === 'interrupted' && run.pending_approval)
}

/**
 * 判断是否达到步数上限。
 */
export function isStepLimitReached(stepCount: number): boolean {
  return stepCount >= AGENT_MAX_STEPS
}

/**
 * 判断是否达到检索步数上限。
 */
export function isRetrievalLimitReached(retrievalStepCount: number): boolean {
  return retrievalStepCount >= AGENT_MAX_RETRIEVAL_STEPS
}

export type StopReasonCategory = 'security' | 'limit' | 'loop' | 'system'

export type StopReasonMeta = {
  raw: string
  title: string
  description: string
  tone: 'rose' | 'amber' | 'slate'
  category: StopReasonCategory
}

/**
 * 结构化解析停止原因元数据。
 */
export function describeStopReason(reason: string | null | undefined): StopReasonMeta | null {
  if (!reason) return null
  if (reason.startsWith("Write tool is not authorized")) {
    return {
      raw: reason,
      title: '越权写入拦截 (Security Guard)',
      description: '只读会话未授权调用写入工具，安全拒绝（检测到潜在提示词注入或只读越权）。',
      tone: 'rose',
      category: 'security',
    }
  }
  if (reason.startsWith('Maximum steps reached')) {
    return {
      raw: reason,
      title: '达到单次步数上限',
      description: `已达到单次会话最大步数上限（${AGENT_MAX_STEPS} 步），安全终止`,
      tone: 'amber',
      category: 'limit',
    }
  }
  if (reason.startsWith('Maximum retrieval steps reached')) {
    return {
      raw: reason,
      title: '达到最大检索上限',
      description: `已达到最大检索次数上限（${AGENT_MAX_RETRIEVAL_STEPS} 次），安全终止`,
      tone: 'amber',
      category: 'limit',
    }
  }
  if (reason.startsWith('Repeated invalid tool calls')) {
    return {
      raw: reason,
      title: '连续异常调用熔断',
      description: '检测到连续无效或异常工具调用，触发熔断保护',
      tone: 'rose',
      category: 'loop',
    }
  }
  if (reason.startsWith('Repeated identical tool call')) {
    return {
      raw: reason,
      title: '重复入参调用熔断',
      description: '检测到重复执行相同入参的工具调用，触发熔断保护',
      tone: 'rose',
      category: 'loop',
    }
  }
  if (reason.startsWith('No progress')) {
    return {
      raw: reason,
      title: '无进展自动终止',
      description: '智能体连续未取得有效进展，自动终止执行',
      tone: 'amber',
      category: 'loop',
    }
  }
  if (reason.startsWith('No evidence was read')) {
    return {
      raw: reason,
      title: '未读取到有效证据',
      description: '尚未检索到有效笔记证据，无法形成回答',
      tone: 'amber',
      category: 'system',
    }
  }
  if (reason.startsWith('Planner failed')) {
    return {
      raw: reason,
      title: '规划器执行故障',
      description: `规划器发生故障：${reason.replace(/^Planner failed:?\s*/, '') || '决策异常'}`,
      tone: 'rose',
      category: 'system',
    }
  }
  return {
    raw: reason,
    title: '执行提前终止',
    description: reason,
    tone: 'slate',
    category: 'system',
  }
}

/**
 * 将英文停止原因转换为友好的中文说明。
 */
export function formatStopReason(reason: string | null | undefined): string | null {
  const meta = describeStopReason(reason)
  return meta ? meta.description : null
}

/**
 * 根据当前步数消耗计算健康色调。
 */
export function getStepHealthTone(
  count: number,
  max: number = AGENT_MAX_STEPS,
): 'normal' | 'warning' | 'danger' {
  if (count >= max) return 'danger'
  if (count >= max - 3) return 'warning'
  return 'normal'
}

/**
 * 根据当前检索次数计算健康色调。
 */
export function getRetrievalHealthTone(
  count: number,
  max: number = AGENT_MAX_RETRIEVAL_STEPS,
): 'normal' | 'warning' | 'danger' {
  if (count >= max) return 'danger'
  if (count >= max - 1) return 'warning'
  return 'normal'
}


/**
 * 汇总时间线中各工具的调用频次。
 */
export function summarizeTimelineTools(timeline: AgentTimelineItem[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const item of timeline) {
    counts[item.tool] = (counts[item.tool] || 0) + 1
  }
  return counts
}

export type AgentDiffLineType = 'add' | 'delete' | 'hunk' | 'header' | 'context'

export type AgentDiffLine = {
  text: string
  type: AgentDiffLineType
  lineNum: number
}

/**
 * 将统一 Diff 文本解析为具名类型的行数据，便于进行语法高亮染色渲染。
 */
export function parseUnifiedDiff(raw: string): AgentDiffLine[] {
  if (!raw) return []
  const lines = raw.split('\n')
  return lines.map((text, idx) => {
    let type: AgentDiffLineType = 'context'
    if (text.startsWith('+++') || text.startsWith('---')) {
      type = 'header'
    } else if (text.startsWith('+')) {
      type = 'add'
    } else if (text.startsWith('-')) {
      type = 'delete'
    } else if (text.startsWith('@@')) {
      type = 'hunk'
    }
    return {
      text,
      type,
      lineNum: idx + 1,
    }
  })
}

/**
 * 统计 Diff 文本中的增删行数与代码块（hunk）数。
 */
export function summarizeUnifiedDiff(raw: string): {
  additions: number
  deletions: number
  hunks: number
} {
  const lines = parseUnifiedDiff(raw)
  let additions = 0
  let deletions = 0
  let hunks = 0

  for (const line of lines) {
    if (line.type === 'add') additions++
    else if (line.type === 'delete') deletions++
    else if (line.type === 'hunk') hunks++
  }

  return { additions, deletions, hunks }
}

/**
 * 将审批类别标识映射为中文标签。
 */
export function formatApprovalKind(kind: string): string {
  switch (kind) {
    case 'write_approval':
      return '写入变更审批'
    case 'plan_write':
      return '计划写入审批'
    case 'delete_approval':
      return '删除笔记审批'
    default:
      return kind || '待审批操作'
  }
}

