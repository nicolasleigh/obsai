/**
 * 问答页的纯逻辑：弃答分类、回答正文分段、降级提示筛选。
 *
 * 和 `search.ts` 一样，这里不碰 React 也不碰 fetch，因此测试可以在 vitest 里跑。
 *
 * 这个模块存在的**唯一理由**是：`AskOutcome` 里三种弃答的形状完全一样。
 * 空问题、无证据、引用校验失败都返回 `abstained: true`、`citations: []`，
 * 都是 200，没有各自的错误码，HTTP 状态也没有差别。服务端没有把它们分开，
 * 所以"分开"这件事只能在读侧做——而读侧必须有一处集中、且被测试钉住的判断。
 */

import type { AskOutcome, CitationView } from './api-types'

/** 服务端在引用校验失败时追加的诊断，见 `answering/service.py`。
 *
 * 它是一句英文诊断，不是给用户看的说明；用户看到的那句中文在 `text` 里。
 */
const CITATION_WARNING = 'Model answer had missing or invalid citations'

/** 服务端对空问题的固定回复，见 `answering/service.py`。
 *
 * 只在结构信号分不开的时候兜底用——见 `classifyAskOutcome` 的第三条。
 */
const BLANK_QUESTION_TEXT = '请提供问题。'

/** 一次问答的结果分类：真的答了，或三种弃答之一。 */
export type AskKind = 'answered' | 'empty_question' | 'no_evidence' | 'citation_failed'

/**
 * 判断这次问答属于哪一种。
 *
 * 判据优先用**结构**，只在结构真的分不开时才读文案：
 *
 * 1. 没有弃答 → `answered`。
 * 2. `warnings` 里有引用诊断 → `citation_failed`。这条最可靠：该标记只在
 *    `validate_citations` 返回 `None` 的那一条分支上追加。
 * 3. `warnings` 为空 → `empty_question`。空问题在检索器之前就返回了，所以
 *    **不可能**带上任何降级提示；这是它唯一的结构特征。
 * 4. 其余 → `no_evidence`。
 *
 * 第 3 条有一个已知盲区，写在这里而不是留给下一个人去踩：如果语义腿完好
 * （向量已建且已批准）、而查询确实一条都没命中，那么同样是"弃答 + warnings 为空"，
 * 与空问题无法用结构区分。所以这一步退化为比较文案，字面量收在上面的常量里。
 * 该盲区要等语义检索真正跑起来（B-8 之后）才可能出现，现在不可达；等它可达时，
 * 正确的修法是让服务端给出成因，而不是继续在这里堆字符串。
 *
 * 另外：页面本身不会送出空问题（输入框为空时不发请求），所以 `empty_question`
 * 在界面上不可达。保留它是因为这个函数描述的是**端点**的行为，不是页面的行为。
 */
export function classifyAskOutcome(outcome: AskOutcome): AskKind {
  if (!outcome.abstained) return 'answered'
  if (outcome.warnings.includes(CITATION_WARNING)) return 'citation_failed'
  if (outcome.warnings.length === 0) {
    return outcome.text.trim() === BLANK_QUESTION_TEXT ? 'empty_question' : 'no_evidence'
  }
  return 'no_evidence'
}

/** 回答正文里一段的两种形态。 */
export type AnswerSegment =
  | { kind: 'text'; text: string }
  | { kind: 'citation'; citationId: string }

/** 与 `answering/citation.py` 的 `_CITATION` 一致。 */
const CITATION_PATTERN = /\[S\d+\]/g

/**
 * 把回答正文切成"普通文字"与"引用角标"。
 *
 * 只有出现在 `citations` 里的编号才会变成角标；认不出的 `[S9]` 留在文字里。
 * 服务端的 `validate_citations` 已经保证不会有认不出的编号——遇到未知编号它会
 * 整条弃答——所以"留在文字里"这条分支正常情况下不可达。留着它是因为这里是从
 * 模型输出走向 DOM 的唯一一处：万一那条保证失效，退化成一个纯文本远好过一个
 * 指向 `undefined` 的角标。
 */
export function splitAnswerSegments(
  text: string,
  citations: readonly CitationView[],
): AnswerSegment[] {
  const known = new Set(citations.map((citation) => citation.citation_id))
  const segments: AnswerSegment[] = []
  let cursor = 0

  for (const match of text.matchAll(CITATION_PATTERN)) {
    const label = match[0]
    const id = label.slice(1, -1)
    if (!known.has(id)) continue
    const start = match.index ?? 0
    if (start > cursor) segments.push({ kind: 'text', text: text.slice(cursor, start) })
    segments.push({ kind: 'citation', citationId: id })
    cursor = start + label.length
  }

  if (cursor < text.length) segments.push({ kind: 'text', text: text.slice(cursor) })
  return segments
}

/**
 * "检索已降级"要展示的提示。
 *
 * 滤掉引用诊断：失败原因已经由弃答文案用中文说清楚了，而这条英文诊断列在
 * "检索已降级"下面会指向错误的部件——检索没问题，是模型没按格式给引用。
 */
export function degradationNotices(outcome: AskOutcome): readonly string[] {
  return outcome.warnings.filter((warning) => warning !== CITATION_WARNING)
}
