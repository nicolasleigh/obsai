import { describe, expect, it } from 'vitest'

import {
  AGENT_MAX_RETRIEVAL_STEPS,
  AGENT_MAX_STEPS,
  AGENT_STATUS_LABELS,
  AGENT_TOOL_LABELS,
  describeStopReason,
  formatAgentStatus,
  formatApprovalKind,
  formatStopReason,
  formatToolName,
  getRetrievalHealthTone,
  getStepHealthTone,
  hasPendingApproval,
  isRetrievalLimitReached,
  isStepLimitReached,
  isTerminalStatus,
  isWriteTool,
  parseUnifiedDiff,
  summarizeTimelineTools,
  summarizeUnifiedDiff,
} from './agent'
import type { AgentRunView, AgentTimelineItem } from './api-types'

describe('Agent 状态与工具分类', () => {
  it('工具中文名称映射字典完整', () => {
    expect(AGENT_TOOL_LABELS['search_notes']).toBe('检索笔记')
    expect(AGENT_STATUS_LABELS['completed']).toBe('已完成')
    expect(formatToolName('search_notes')).toBe('检索笔记')
    expect(formatToolName('read_note')).toBe('读取笔记')
    expect(formatToolName('get_backlinks')).toBe('反向链接')
    expect(formatToolName('get_outgoing_links')).toBe('引用出链')
    expect(formatToolName('create_note')).toBe('创建笔记')
    expect(formatToolName('update_note')).toBe('更新笔记')
    expect(formatToolName('move_note')).toBe('移动笔记')
    expect(formatToolName('trash_note')).toBe('删除笔记')
    expect(formatToolName('apply_write')).toBe('应用写入')
    expect(formatToolName('rag_answer')).toBe('生成回答')
    expect(formatToolName('custom_tool')).toBe('custom_tool')
  })

  it('正确区分读写类工具', () => {
    expect(isWriteTool('create_note')).toBe(true)
    expect(isWriteTool('update_note')).toBe(true)
    expect(isWriteTool('move_note')).toBe(true)
    expect(isWriteTool('trash_note')).toBe(true)
    expect(isWriteTool('update_frontmatter')).toBe(true)

    expect(isWriteTool('search_notes')).toBe(false)
    expect(isWriteTool('read_note')).toBe(false)
    expect(isWriteTool('get_backlinks')).toBe(false)
  })

  it('工作流状态映射符合预期', () => {
    expect(formatAgentStatus('completed')).toBe('已完成')
    expect(formatAgentStatus('interrupted')).toBe('等待审批')
    expect(formatAgentStatus('failed')).toBe('执行中断')
    expect(formatAgentStatus('running')).toBe('执行中')
    expect(formatAgentStatus('unknown_status')).toBe('unknown_status')

    expect(isTerminalStatus('completed')).toBe(true)
    expect(isTerminalStatus('failed')).toBe(true)
    expect(isTerminalStatus('interrupted')).toBe(false)
    expect(isTerminalStatus('running')).toBe(false)
  })

  it('有界性步数上限检查', () => {
    expect(AGENT_MAX_STEPS).toBe(15)
    expect(AGENT_MAX_RETRIEVAL_STEPS).toBe(5)

    expect(isStepLimitReached(14)).toBe(false)
    expect(isStepLimitReached(15)).toBe(true)
    expect(isStepLimitReached(20)).toBe(true)

    expect(isRetrievalLimitReached(4)).toBe(false)
    expect(isRetrievalLimitReached(5)).toBe(true)
    expect(isRetrievalLimitReached(10)).toBe(true)
  })

  it('友好转换停止原因', () => {
    expect(formatStopReason(null)).toBeNull()
    expect(formatStopReason(undefined)).toBeNull()
    expect(formatStopReason('Maximum steps reached')).toContain('最大步数上限（15 步）')
    expect(formatStopReason('Maximum retrieval steps reached')).toContain('最大检索次数上限（5 次）')
    expect(formatStopReason('Repeated invalid tool calls')).toContain('连续无效或异常工具调用')
    expect(formatStopReason('Repeated identical tool call')).toContain('重复执行相同入参')
    expect(formatStopReason('No progress')).toContain('未取得有效进展')
    expect(formatStopReason('Write tool is not authorized by the user\'s request')).toContain('未授权调用写入工具')
    expect(formatStopReason('No evidence was read')).toContain('尚未检索到有效笔记证据')
    expect(formatStopReason('Other reason')).toBe('Other reason')
  })

  it('结构化解析停止原因元数据 (describeStopReason)', () => {
    expect(describeStopReason(null)).toBeNull()
    expect(describeStopReason(undefined)).toBeNull()

    const security = describeStopReason("Write tool is not authorized by the user's request")
    expect(security?.category).toBe('security')
    expect(security?.tone).toBe('rose')
    expect(security?.title).toContain('越权写入拦截')

    const stepLimit = describeStopReason('Maximum steps reached')
    expect(stepLimit?.category).toBe('limit')
    expect(stepLimit?.tone).toBe('amber')
    expect(stepLimit?.title).toContain('达到单次步数上限')

    const retrievalLimit = describeStopReason('Maximum retrieval steps reached')
    expect(retrievalLimit?.category).toBe('limit')
    expect(retrievalLimit?.tone).toBe('amber')

    const invalidCalls = describeStopReason('Repeated invalid tool calls')
    expect(invalidCalls?.category).toBe('loop')
    expect(invalidCalls?.tone).toBe('rose')

    const identicalCalls = describeStopReason('Repeated identical tool call')
    expect(identicalCalls?.category).toBe('loop')
    expect(identicalCalls?.tone).toBe('rose')

    const noProgress = describeStopReason('No progress')
    expect(noProgress?.category).toBe('loop')
    expect(noProgress?.tone).toBe('amber')

    const noEvidence = describeStopReason('No evidence was read')
    expect(noEvidence?.category).toBe('system')
    expect(noEvidence?.tone).toBe('amber')

    const plannerFailure = describeStopReason('Planner failed: OpenAI timeout')
    expect(plannerFailure?.category).toBe('system')
    expect(plannerFailure?.tone).toBe('rose')
    expect(plannerFailure?.description).toContain('OpenAI timeout')

    const unknown = describeStopReason('Custom unexpected stop')
    expect(unknown?.category).toBe('system')
    expect(unknown?.tone).toBe('slate')
    expect(unknown?.description).toBe('Custom unexpected stop')
  })

  it('步数与检索健康色调评定', () => {
    expect(getStepHealthTone(5, 15)).toBe('normal')
    expect(getStepHealthTone(11, 15)).toBe('normal')
    expect(getStepHealthTone(12, 15)).toBe('warning')
    expect(getStepHealthTone(14, 15)).toBe('warning')
    expect(getStepHealthTone(15, 15)).toBe('danger')
    expect(getStepHealthTone(20, 15)).toBe('danger')

    expect(getRetrievalHealthTone(2, 5)).toBe('normal')
    expect(getRetrievalHealthTone(3, 5)).toBe('normal')
    expect(getRetrievalHealthTone(4, 5)).toBe('warning')
    expect(getRetrievalHealthTone(5, 5)).toBe('danger')
    expect(getRetrievalHealthTone(6, 5)).toBe('danger')
  })

  it('检测是否存在待审批状态', () => {
    const normalRun: AgentRunView = {
      run_id: 'r1',
      status: 'completed',
      query: 'search test',
      step_count: 1,
      retrieval_step_count: 1,
      selected_note_ids: [],
      retrieved_chunk_ids: [],
      timeline: [],
      final_answer: 'ok',
      stop_reason: null,
      pending_approval: null,
    }
    expect(hasPendingApproval(normalRun)).toBe(false)
    expect(hasPendingApproval(null)).toBe(false)

    const interruptedRun: AgentRunView = {
      ...normalRun,
      status: 'interrupted',
      pending_approval: {
        kind: 'write_approval',
        preview: '--- diff',
        plan_ref: 'plan-1',
      },
    }
    expect(hasPendingApproval(interruptedRun)).toBe(true)
  })

  it('统计时间线工具频次', () => {
    const timeline: AgentTimelineItem[] = [
      { tool: 'search_notes', summary: 's1', args: {} },
      { tool: 'read_note', summary: 'r1', args: {} },
      { tool: 'search_notes', summary: 's2', args: {} },
    ]
    const counts = summarizeTimelineTools(timeline)
    expect(counts).toEqual({
      search_notes: 2,
      read_note: 1,
    })
  })

  it('解析统一 Diff 语法与类型', () => {
    const rawDiff = `--- a/Notes/Test.md
+++ b/Notes/Test.md
@@ -1,3 +1,4 @@
 # Header
-Old line
+New line 1
+New line 2
 Normal context`

    const parsed = parseUnifiedDiff(rawDiff)
    expect(parsed).toHaveLength(8)
    expect(parsed[0]).toEqual({ text: '--- a/Notes/Test.md', type: 'header', lineNum: 1 })
    expect(parsed[1]).toEqual({ text: '+++ b/Notes/Test.md', type: 'header', lineNum: 2 })
    expect(parsed[2]).toEqual({ text: '@@ -1,3 +1,4 @@', type: 'hunk', lineNum: 3 })
    expect(parsed[3]).toEqual({ text: ' # Header', type: 'context', lineNum: 4 })
    expect(parsed[4]).toEqual({ text: '-Old line', type: 'delete', lineNum: 5 })
    expect(parsed[5]).toEqual({ text: '+New line 1', type: 'add', lineNum: 6 })
    expect(parsed[6]).toEqual({ text: '+New line 2', type: 'add', lineNum: 7 })
    expect(parsed[7]).toEqual({ text: ' Normal context', type: 'context', lineNum: 8 })

    expect(parseUnifiedDiff('')).toEqual([])
  })

  it('统计 Diff 的增删行数与区块数', () => {
    const rawDiff = `--- a/Test.md
+++ b/Test.md
@@ -1 +1,2 @@
-Line 1
+Line 1 modified
+Line 2 added
@@ -10 +11 @@
-Old footer
+New footer`

    const summary = summarizeUnifiedDiff(rawDiff)
    expect(summary).toEqual({
      additions: 3,
      deletions: 2,
      hunks: 2,
    })

    expect(summarizeUnifiedDiff('')).toEqual({ additions: 0, deletions: 0, hunks: 0 })
  })

  it('友好转换审批类别', () => {
    expect(formatApprovalKind('write_approval')).toBe('写入变更审批')
    expect(formatApprovalKind('plan_write')).toBe('计划写入审批')
    expect(formatApprovalKind('delete_approval')).toBe('删除笔记审批')
    expect(formatApprovalKind('custom_kind')).toBe('custom_kind')
    expect(formatApprovalKind('')).toBe('待审批操作')
  })
})

