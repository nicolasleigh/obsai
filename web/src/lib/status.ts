import type { IndexStatusView, StatusResponse, StatusView } from '@/lib/api-types'
import { present } from '@/lib/errors'

/**
 * `/status` → 可以直接显示的中文。
 *
 * 后端把「无 Vault」「Vault 已消失」「索引未建」「索引损坏」「事务待恢复」「持锁」
 * 六种降级状态全部当作**数据**返回（见计划 §17.2 决策 1）。这个模块负责把那些
 * 数据翻译成"现在是什么情况、下一步做什么"，而不是把六个布尔量摊在页面上让用户
 * 自己拼。概览页、侧栏状态灯、将来的索引页都从这里取同一份文案。
 *
 * 全部是纯函数，不碰 React，因此可以在 node 环境下逐个状态验证。
 */

/** 严重度。`danger` 表示需要用户先处理，`warning` 表示功能会降级但仍可用。 */
export type StatusTone = 'ok' | 'warning' | 'danger' | 'unknown'

export type StatusSummary = {
  tone: StatusTone
  /** 一句话说明当前情况。 */
  label: string
  /** 下一步该做什么；不需要动作时是空串，而不是"一切正常"这种废话。 */
  hint: string
}

const TONE_RANK: Record<StatusTone, number> = { unknown: 0, ok: 1, warning: 2, danger: 3 }

/** 取最严重的一个。用于把若干条摘要合成一条总览。 */
export function worstTone(tones: readonly StatusTone[]): StatusTone {
  return tones.reduce<StatusTone>(
    (worst, tone) => (TONE_RANK[tone] > TONE_RANK[worst] ? tone : worst),
    'ok',
  )
}

/** 取第一条 `danger`，否则第一条 `warning`；都没有则说明全部正常。 */
function firstBlocker(parts: readonly StatusSummary[]): StatusSummary | undefined {
  return parts.find((part) => part.tone === 'danger') ?? parts.find((part) => part.tone === 'warning')
}

/**
 * Vault 是否可读。
 *
 * 这个判断单独存在，是因为它决定了**另一些字段有没有意义**。事务日志只存在于
 * Vault 里：`read_status()` 在 `vault_ready` 为假时根本不调 `list_journals()`，
 * 于是 `unfinished_transactions` 是空元组——空元组读起来像"没有未完成的事务"，
 * 但服务端那句话其实是"我没能去看"。把这两种情况分开是本模块的主要职责之一。
 */
export function vaultIsReadable(status: StatusView): boolean {
  return status.vault_path !== null && status.vault_ready
}

export function summarizeVault(status: StatusView): StatusSummary {
  if (status.vault_path === null) {
    return {
      tone: 'warning',
      label: '未配置 Vault',
      hint: '在 ~/.config/obsai/config.toml 里设置 vault.path，指向你的 Obsidian 库。',
    }
  }
  if (!status.vault_ready) {
    return {
      tone: 'danger',
      label: 'Vault 目录不存在',
      hint: `${status.vault_path} 打不开。检查 vault.path 是否写对，或该目录是否被移动、改名。`,
    }
  }
  return { tone: 'ok', label: 'Vault 就绪', hint: '' }
}

export function summarizeIndex(index: IndexStatusView): StatusSummary {
  if (!index.exists) {
    return {
      tone: 'warning',
      label: '索引尚未构建',
      hint: '运行 obsai index update 建立索引。索引是可重建的派生物，不会影响笔记。',
    }
  }
  if (!index.usable) {
    return {
      tone: 'danger',
      label: '索引无法打开',
      hint: index.error ?? '原因未知。运行 obsai index rebuild 重建。',
    }
  }
  if (index.note_count === 0) {
    return {
      tone: 'warning',
      label: '索引为空',
      hint: '运行 obsai index update 收录笔记。',
    }
  }
  if (!index.semantic_ready) {
    return {
      tone: 'warning',
      label: `已索引 ${index.note_count} 篇笔记`,
      hint: '语义检索不可用，检索会降级为关键词模式。运行 obsai index embeddings 补齐向量。',
    }
  }
  return { tone: 'ok', label: `已索引 ${index.note_count} 篇笔记`, hint: '' }
}

export function summarizeRecovery(status: StatusView): StatusSummary {
  // 先排除"读不到"，否则下面的空列表会被说成"无待恢复事务"。
  if (!vaultIsReadable(status)) {
    return {
      tone: 'unknown',
      label: '无法检查事务状态',
      hint:
        status.vault_path === null
          ? '未配置 Vault，没有地方可以读事务日志。'
          : 'Vault 目录打不开，读不到事务日志。先修好 Vault，这里才有结论。',
    }
  }
  const pending = status.unfinished_transactions.length
  if (status.recovery_required && pending > 0) {
    return {
      tone: 'danger',
      label: `有 ${pending} 个未完成的写入事务`,
      hint: '写入已被冻结。运行 obsai transaction status 查看，再用 obsai transaction recover <ID> 恢复。',
    }
  }
  const stale = status.index_dirty_transactions.length
  if (stale > 0) {
    return {
      tone: 'warning',
      label: `有 ${stale} 个事务待补索引`,
      hint: '笔记已改完，只差索引。运行 obsai index update 补齐。',
    }
  }
  return { tone: 'ok', label: '无待恢复事务', hint: '' }
}

/**
 * 待重新索引的笔记。
 *
 * `dirty_notes` 在索引打不开时是**默认空元组**（见 `IndexStatusView` 的字段默认值），
 * 所以它同样区分不了"没有脏笔记"和"读不到脏笔记"。索引不可用时给出 `unknown`，
 * 而不是让用户以为索引是干净的。
 */
export function summarizeDirtyNotes(index: IndexStatusView): StatusSummary {
  if (!index.usable) {
    return {
      tone: 'unknown',
      label: '无法检查待重新索引的笔记',
      hint: '索引打不开，读不到这份清单。',
    }
  }
  const count = index.dirty_notes.length
  if (count === 0) {
    return { tone: 'ok', label: '没有待重新索引的笔记', hint: '' }
  }
  return {
    tone: 'warning',
    label: `有 ${count} 篇笔记待重新索引`,
    hint: '运行 obsai index update 补齐；不补的话检索结果会落后于笔记内容。',
  }
}

export function summarizeLock(status: StatusView): StatusSummary {
  if (status.locked) {
    return {
      tone: 'warning',
      label: '写入锁被另一个进程持有',
      hint: '另一个 obsai 进程正在写入此 Vault。等它结束；如果那是卡住的 index update，先让它返回。',
    }
  }
  return { tone: 'ok', label: '写入锁空闲', hint: '' }
}

/**
 * 概览页要逐项渲染的全部摘要。
 *
 * 顺序即"先看哪个"：Vault 决定另一些字段有没有意义，索引决定检索能不能用，
 * 事务与脏笔记是两种"笔记已改、索引没跟上"，锁是外部进程的状态。
 *
 * 脏笔记也在里面，虽然它没有单独的卡片摘要——侧栏状态灯要的是"有没有值得
 * 先说一句的事"。少了它，一篇刚改过的笔记会让状态灯继续显示"后端已连接"。
 */
export function summarizeAll(status: StatusView): readonly StatusSummary[] {
  return [
    summarizeVault(status),
    summarizeIndex(status.index),
    summarizeRecovery(status),
    summarizeDirtyNotes(status.index),
    summarizeLock(status),
  ]
}

export type BackendInput = {
  /** TanStack Query 的 `data`；首次加载完成前是 `undefined`。 */
  data: StatusResponse | undefined
  /** TanStack Query 的 `error`；无错误时是 `null` 或 `undefined`。 */
  error: unknown
}

/**
 * 后端整体状况，供侧栏状态灯使用。
 *
 * 顺序是刻意的：**先报错，再看数据**。TanStack Query 在重取失败时会保留上一次的
 * `data`，如果先看数据，界面会在后端已经断开的情况下继续显示"一切正常"。
 */
export function summarizeBackend(input: BackendInput): StatusSummary {
  if (input.error != null) {
    const failure = present(input.error)
    return { tone: 'danger', label: failure.title, hint: failure.hint ?? '' }
  }
  if (input.data === undefined) {
    return { tone: 'unknown', label: '正在连接后端 ...', hint: '' }
  }
  const blocker = firstBlocker(summarizeAll(input.data))
  if (blocker === undefined) {
    return { tone: 'ok', label: '后端已连接', hint: '' }
  }
  return { tone: blocker.tone, label: blocker.label, hint: blocker.hint }
}

/** 索引里有没有笔记，决定检索页该不该提示"先建索引"。 */
export function indexIsSearchable(index: IndexStatusView): boolean {
  return index.usable && index.note_count > 0
}
