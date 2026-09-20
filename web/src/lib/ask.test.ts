import { describe, expect, it } from 'vitest'

import type { AskOutcome, CitationView } from './api-types'
import { classifyAskOutcome, degradationNotices, splitAnswerSegments } from './ask'

// 服务端原样发出的两条字符串，见 `answering/service.py` 与 `application/search.py`。
const SEMANTIC_INDEX_MISSING = "Semantic index missing; run 'obsai index embeddings'"
const CITATION_WARNING = 'Model answer had missing or invalid citations'

// 三条弃答文案，与 `tests/integration/test_api_ask.py` 钉的是同一批字面量。
const NO_QUESTION = '请提供问题。'
const NO_EVIDENCE = '未找到可用于回答的笔记证据。'
const BAD_CITATIONS = '无法从检索到的证据生成带有效引用的回答。'

function citation(id: string, path = 'Backend/Redis.md'): CitationView {
  return {
    citation_id: id,
    note_id: 'note-1',
    chunk_id: `chunk-${id}`,
    path,
    title: 'Redis',
    heading_path: ['Redis'],
    block_id: null,
    location: `${path} > Redis`,
    truncated: false,
  }
}

function outcome(overrides: Partial<AskOutcome> = {}): AskOutcome {
  return { text: '', citations: [], warnings: [], abstained: false, ...overrides }
}

/** 把分段还原成原文，用来验证切分没有丢字也没有多字。 */
function rejoin(text: string, citations: readonly CitationView[]): string {
  return splitAnswerSegments(text, citations)
    .map((segment) => (segment.kind === 'text' ? segment.text : `[${segment.citationId}]`))
    .join('')
}

// --------------------------------------------------------------------------- //
// classifyAskOutcome
// --------------------------------------------------------------------------- //

describe('classifyAskOutcome', () => {
  it('reports an answered response as answered', () => {
    const payload = outcome({ text: 'Redis 是内存缓存。[S1]', citations: [citation('S1')] })
    expect(classifyAskOutcome(payload)).toBe('answered')
  })

  it('does not treat a degraded but answered response as an abstention', () => {
    // 降级与弃答是两件事：语义腿没跑成，但关键词证据足够回答。把 warnings 非空
    // 当成弃答，会让每一次降级检索都显示成"无法回答"。
    const payload = outcome({
      text: 'Redis 是内存缓存。[S1]',
      citations: [citation('S1')],
      warnings: [SEMANTIC_INDEX_MISSING],
    })
    expect(classifyAskOutcome(payload)).toBe('answered')
  })

  it('recognises a failed citation check', () => {
    const payload = outcome({
      text: BAD_CITATIONS,
      warnings: [SEMANTIC_INDEX_MISSING, CITATION_WARNING],
      abstained: true,
    })
    expect(classifyAskOutcome(payload)).toBe('citation_failed')
  })

  it('recognises a failed citation check even without a degradation notice', () => {
    // 语义腿完好时 warnings 里只有引用诊断，这条判据不能依赖"还有别的 warning"。
    const payload = outcome({
      text: BAD_CITATIONS,
      warnings: [CITATION_WARNING],
      abstained: true,
    })
    expect(classifyAskOutcome(payload)).toBe('citation_failed')
  })

  it('does not mistake the citation diagnostic for an empty question', () => {
    // 判据顺序：引用诊断先于"warnings 为空"。两条分支在只有引用诊断时会同时成立，
    // 顺序错了就会把"模型乱引用"报成"你没提问"。
    const payload = outcome({ text: BAD_CITATIONS, warnings: [CITATION_WARNING], abstained: true })
    expect(classifyAskOutcome(payload)).not.toBe('empty_question')
  })

  it('recognises an abstention with no evidence', () => {
    const payload = outcome({
      text: NO_EVIDENCE,
      warnings: [SEMANTIC_INDEX_MISSING],
      abstained: true,
    })
    expect(classifyAskOutcome(payload)).toBe('no_evidence')
  })

  it('recognises an empty question by its missing warnings', () => {
    // 空问题在检索器之前就返回了，所以不可能带上降级提示——这是它唯一的结构特征。
    const payload = outcome({ text: NO_QUESTION, warnings: [], abstained: true })
    expect(classifyAskOutcome(payload)).toBe('empty_question')
  })

  it('falls back to no_evidence when warnings are empty and the text is not the blank-question one', () => {
    // 已知盲区：语义腿完好且一条都没命中时，形状与空问题完全相同。这里把兜底方向
    // 钉住——宁可说"没找到证据"（下一步可以换个问法），也不要说"请提供问题"
    // （用户明明提了问题）。
    const payload = outcome({ text: NO_EVIDENCE, warnings: [], abstained: true })
    expect(classifyAskOutcome(payload)).toBe('no_evidence')
  })
})

// --------------------------------------------------------------------------- //
// splitAnswerSegments
// --------------------------------------------------------------------------- //

describe('splitAnswerSegments', () => {
  it('returns nothing for an empty answer', () => {
    expect(splitAnswerSegments('', [])).toEqual([])
  })

  it('returns a single text segment when there is no citation', () => {
    expect(splitAnswerSegments('Redis 是内存缓存。', [])).toEqual([
      { kind: 'text', text: 'Redis 是内存缓存。' },
    ])
  })

  it('splits a citation in the middle', () => {
    const citations = [citation('S1'), citation('S2')]
    expect(splitAnswerSegments('先 [S1] 后 [S2] 结束', citations)).toEqual([
      { kind: 'text', text: '先 ' },
      { kind: 'citation', citationId: 'S1' },
      { kind: 'text', text: ' 后 ' },
      { kind: 'citation', citationId: 'S2' },
      { kind: 'text', text: ' 结束' },
    ])
  })

  it('handles a citation at the very start', () => {
    expect(splitAnswerSegments('[S1] 是缓存。', [citation('S1')])).toEqual([
      { kind: 'citation', citationId: 'S1' },
      { kind: 'text', text: ' 是缓存。' },
    ])
  })

  it('handles a citation at the very end without emitting an empty tail', () => {
    // 尾段为空时必须不产出，否则页面会多渲染一个空 <span>。
    expect(splitAnswerSegments('见 [S1]', [citation('S1')])).toEqual([
      { kind: 'text', text: '见 ' },
      { kind: 'citation', citationId: 'S1' },
    ])
  })

  it('handles an answer that is only a citation', () => {
    expect(splitAnswerSegments('[S1]', [citation('S1')])).toEqual([
      { kind: 'citation', citationId: 'S1' },
    ])
  })

  it('leaves an unknown citation id inside the surrounding text', () => {
    // 服务端的 validate_citations 会因此整条弃答，所以这条分支正常不可达；
    // 万一失效，退化成纯文本好过渲染一个指向 undefined 的角标。
    expect(splitAnswerSegments('见 [S9]', [citation('S1')])).toEqual([
      { kind: 'text', text: '见 [S9]' },
    ])
  })

  it('leaves every citation in the text when the citations list is empty', () => {
    expect(splitAnswerSegments('见 [S1] 和 [S2]', [])).toEqual([
      { kind: 'text', text: '见 [S1] 和 [S2]' },
    ])
  })

  it('keeps an unknown id in the run that follows a known one', () => {
    expect(splitAnswerSegments('A [S1] B [S9] C', [citation('S1')])).toEqual([
      { kind: 'text', text: 'A ' },
      { kind: 'citation', citationId: 'S1' },
      { kind: 'text', text: ' B [S9] C' },
    ])
  })

  it('matches multi-digit ids', () => {
    expect(splitAnswerSegments('见 [S10]', [citation('S10')])).toEqual([
      { kind: 'text', text: '见 ' },
      { kind: 'citation', citationId: 'S10' },
    ])
  })

  it('renders a repeated citation as a badge every time it appears', () => {
    // 去重是服务端 citations 列表的事（同一个 id 只出现一次），正文里出现几次
    // 就要有几个角标——否则第二次引用会变成裸文本 [S1]。
    expect(splitAnswerSegments('[S1] 和 [S1]', [citation('S1')])).toEqual([
      { kind: 'citation', citationId: 'S1' },
      { kind: 'text', text: ' 和 ' },
      { kind: 'citation', citationId: 'S1' },
    ])
  })

  it('loses nothing when rejoined', () => {
    const citations = [citation('S1'), citation('S2')]
    for (const text of [
      '',
      '没有引用',
      '[S1]',
      '前 [S1] 中 [S2] 后',
      '[S9] 未知',
      'A [S1] B [S9] C [S2]',
    ]) {
      expect(rejoin(text, citations)).toBe(text)
    }
  })
})

// --------------------------------------------------------------------------- //
// degradationNotices
// --------------------------------------------------------------------------- //

describe('degradationNotices', () => {
  it('is empty when there are no warnings', () => {
    expect(degradationNotices(outcome())).toEqual([])
  })

  it('keeps the retrieval degradation notices', () => {
    expect(degradationNotices(outcome({ warnings: [SEMANTIC_INDEX_MISSING] }))).toEqual([
      SEMANTIC_INDEX_MISSING,
    ])
  })

  it('drops the citation diagnostic, which is not a retrieval problem', () => {
    // 把它列在"检索已降级"下面会指向错误的部件：检索没问题，是模型没按格式给引用。
    // 失败原因已经由中文弃答文案说清楚了。
    const payload = outcome({ warnings: [SEMANTIC_INDEX_MISSING, CITATION_WARNING] })
    expect(degradationNotices(payload)).toEqual([SEMANTIC_INDEX_MISSING])
  })

  it('is empty when the citation diagnostic is the only warning', () => {
    expect(degradationNotices(outcome({ warnings: [CITATION_WARNING] }))).toEqual([])
  })
})
