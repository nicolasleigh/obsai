/**
 * 搜索页的纯逻辑：模式定义、分数格式化、筛选器判断。
 *
 * 这里不碰 React，也不碰 fetch——那些属于页面和 `api.ts`。这个模块只回答"分数该
 * 怎么显示"、"有没有活跃筛选器"这类**与渲染无关**的问题，因此测试可以在 vitest
 * 里跑，不需要 jsdom。
 */

import type { SearchFilters, SearchMode, SearchResponse } from './api-types'

export const SEARCH_MODES: readonly { value: SearchMode; label: string }[] = [
  { value: 'keyword', label: '关键词' },
  { value: 'hybrid', label: '混合' },
  { value: 'semantic', label: '语义' },
  { value: 'graph', label: '图谱' },
] as const

export const DEFAULT_SEARCH_MODE: SearchMode = 'hybrid'

/**
 * URL 里的 `mode` 参数 → 合法的模式。
 *
 * 地址栏是用户可以随手改的，所以这里的输入不可信：任何不认识的值都回落到默认模式，
 * 而不是让一个非法值顺着 `as SearchMode` 一路走到请求体里——那样后端会返回 422，
 * 用户看到的却是一个自己没打过的请求失败。
 */
export function parseMode(value: string | null | undefined): SearchMode {
  const found = SEARCH_MODES.find((mode) => mode.value === value)
  return found === undefined ? DEFAULT_SEARCH_MODE : found.value
}

/**
 * 把分数格式化为可读字符串。
 *
 * **不渲染成百分比。**  keyword 返回的是原始 BM25（无上界，可能极小），hybrid 返回
 * 的是 RRF 融合权重（约 ``1/(60+rank)``，~0.016），semantic 返回的是余弦相似度
 * （[-1, 1]）。三种量纲完全不同，``(score * 100).toFixed(0) + '%'`` 会在 keyword
 * 模式下给每条结果打出 ``0%``。
 *
 * ``toPrecision(3)`` 保持诚实：它暴露量纲差异，而不是掩盖它。
 */
export function formatScore(score: number): string {
  return score.toPrecision(3)
}

/** 筛选器里有没有非默认值。 */
export function hasFilters(filters?: SearchFilters): boolean {
  if (!filters) return false
  return (
    (filters.tags?.length ?? 0) > 0 ||
    Boolean(filters.folder?.trim()) ||
    Boolean(filters.modified_after) ||
    Boolean(filters.modified_before) ||
    Object.keys(filters.frontmatter ?? {}).length > 0 ||
    Object.keys(filters.dataview ?? {}).length > 0
  )
}

/** 把活跃筛选器翻译成芯片文字。 */
export function describeFilters(filters: SearchFilters): string[] {
  const chips: string[] = []
  if (filters.tags?.length) chips.push(`标签: ${filters.tags.join(', ')}`)
  if (filters.folder?.trim()) chips.push(`目录: ${filters.folder}`)
  if (filters.modified_after) chips.push(`修改于 ${filters.modified_after} 之后`)
  if (filters.modified_before) chips.push(`修改于 ${filters.modified_before} 之前`)
  if (Object.keys(filters.frontmatter ?? {}).length) chips.push('frontmatter 筛选')
  if (Object.keys(filters.dataview ?? {}).length) chips.push('dataview 筛选')
  return chips
}

/** 结果是否经历了降级（语义腿没跑成）。 */
export function isDegraded(response: SearchResponse): boolean {
  return response.warnings.length > 0
}

/** 页面上要展示的降级提示列表。
 *
 * 目前是 ``warnings`` 的原样透传；未来如果需要合并 ``semantic.reason`` 或去重，
 * 改动只发生在这里。
 */
export function presentWarnings(response: SearchResponse): readonly string[] {
  return response.warnings
}
