import { describe, expect, it } from 'vitest'

import type { NoteBlockView, NoteSegment } from './api-types'
import {
  blockIndexFor,
  blockText,
  frontmatterView,
  groupBlocks,
  headingTag,
  linkHref,
  noteHref,
  unresolvedNotice,
} from './note'

/**
 * 这一组测试守的是 B-7 的验收：「含 `<script>`、`<iframe>` 的笔记不执行任何内容；
 * WikiLink 只跳转服务端校验过的 Vault 内目标」。
 *
 * 后半句在这里被钉住：`linkHref` 是页面唯一决定"可不可点"的地方，而它只看
 * `target_note_id`。前半句不在这里——它靠的是**响应里没有原文**，因此由 Python 侧
 * 的 `tests/integration/test_api_notes.py` 负责；前端这边能做的只是保证没有任何一条
 * 路径会把链接文本变成元素。
 */

function text(value: string): NoteSegment {
  return {
    kind: 'text',
    text: value,
    target_note_id: null,
    target_path: null,
    target_heading: null,
    target_block_id: null,
    is_embed: false,
  }
}

function link(overrides: Partial<NoteSegment> = {}): NoteSegment {
  return {
    kind: 'link',
    text: '目标',
    target_note_id: 'abc123',
    target_path: 'Backend/Redis',
    target_heading: null,
    target_block_id: null,
    is_embed: false,
    ...overrides,
  }
}

function block(overrides: Partial<NoteBlockView> = {}): NoteBlockView {
  return {
    kind: 'paragraph',
    level: null,
    segments: [text('正文')],
    block_id: null,
    language: null,
    line: 1,
    ...overrides,
  }
}

describe('noteHref', () => {
  it('没有锚点时只打开笔记', () => {
    expect(noteHref('abc123', null)).toBe('/notes/abc123')
  })

  it('带锚点时落到那一段', () => {
    expect(noteHref('abc123', 'maxmemory')).toBe('/notes/abc123?block=maxmemory')
  })

  it('锚点会被转义', () => {
    // 引用卡片与笔记正文共用这一个函数，所以转义只需要在一处写对。
    expect(noteHref('abc123', 'a b&c')).toBe('/notes/abc123?block=a%20b%26c')
  })
})

describe('linkHref', () => {
  it('文本段没有跳转目标', () => {
    expect(linkHref(text('普通文字'))).toBeNull()
  })

  it('已解析的链接指向笔记页', () => {
    expect(linkHref(link())).toBe('/notes/abc123')
  })

  it('目标不在 Vault 内时不可点', () => {
    // 这条断言就是 B-7 验收的后半句。服务端用 `target_note_id: null` 表达"目标不
    // 存在"，前端唯一的正确反应是渲染成普通文本——不猜测、不退回按路径拼链接。
    expect(linkHref(link({ target_note_id: null, target_path: 'Notes/Gone' }))).toBeNull()
  })

  it('块链接带上锚点，好让引用能落到那一段', () => {
    expect(linkHref(link({ target_block_id: 'maxmemory' }))).toBe(
      '/notes/abc123?block=maxmemory',
    )
  })

  it('锚点会被转义', () => {
    expect(linkHref(link({ target_block_id: 'a b&c' }))).toBe('/notes/abc123?block=a%20b%26c')
  })

  it('标题链接没有锚点，只打开笔记本身', () => {
    // `[[Note#标题]]` 只解析出 target_heading。不假装能滚到那个标题：服务端没有
    // 给出标题的行号或 id，猜一个位置比停在顶部更糟。
    expect(linkHref(link({ target_heading: '配置' }))).toBe('/notes/abc123')
  })
})

describe('headingTag', () => {
  it('笔记里的 h1 让位给页面标题', () => {
    expect(headingTag(block({ kind: 'heading', level: 1 }))).toBe('h2')
  })

  it('逐级下降', () => {
    expect(headingTag(block({ kind: 'heading', level: 2 }))).toBe('h3')
    expect(headingTag(block({ kind: 'heading', level: 3 }))).toBe('h4')
  })

  it('再深的层级压到 h4', () => {
    expect(headingTag(block({ kind: 'heading', level: 6 }))).toBe('h4')
  })

  it('没有层级时按最顶层处理', () => {
    // `level` 为 null 说明服务端没合上 `Heading`（不该发生）。降级成 h2 而不是抛错：
    // 标题渲染得浅一点是外观问题，页面崩掉不是。
    expect(headingTag(block({ kind: 'heading', level: null }))).toBe('h2')
  })
})

describe('blockText', () => {
  it('按顺序拼接，不丢字也不加分隔', () => {
    expect(blockText(block({ segments: [text('见 '), link({ text: '缓存' }), text(' 一节。')] }))).toBe(
      '见 缓存 一节。',
    )
  })

  it('空块是空串', () => {
    expect(blockText(block({ segments: [] }))).toBe('')
  })
})

describe('groupBlocks', () => {
  it('相邻的列表项合成一个列表', () => {
    const groups = groupBlocks([
      block({ kind: 'list_item', segments: [text('一')] }),
      block({ kind: 'list_item', segments: [text('二')] }),
      block({ kind: 'list_item', segments: [text('三')] }),
    ])

    expect(groups).toHaveLength(1)
    expect(groups[0].kind).toBe('list')
    if (groups[0].kind === 'list') {
      expect(groups[0].blocks.map(blockText)).toEqual(['一', '二', '三'])
    }
  })

  it('被段落隔开的两个列表是两个列表', () => {
    const groups = groupBlocks([
      block({ kind: 'list_item' }),
      block({ kind: 'paragraph' }),
      block({ kind: 'list_item' }),
    ])

    expect(groups.map((group) => group.kind)).toEqual(['list', 'block', 'list'])
  })

  it('非列表块各自成组', () => {
    const groups = groupBlocks([block({ kind: 'heading', level: 1 }), block()])

    expect(groups.map((group) => group.kind)).toEqual(['block', 'block'])
  })

  it('空笔记得到空分组', () => {
    expect(groupBlocks([])).toEqual([])
  })
})

describe('frontmatterView', () => {
  it('标量原样展示', () => {
    expect(frontmatterView({ title: 'Redis', pinned: true, weight: 3 }).entries).toEqual([
      { key: 'title', text: 'Redis' },
      { key: 'pinned', text: 'true' },
      { key: 'weight', text: '3' },
    ])
  })

  it('标量数组拼成一行', () => {
    expect(frontmatterView({ tags: ['redis', 'cache'] }).entries).toEqual([
      { key: 'tags', text: 'redis、cache' },
    ])
  })

  it('嵌套结构只计数，不展开', () => {
    const view = frontmatterView({ title: 'A', meta: { owner: 'me' }, empty: null })

    expect(view.entries).toEqual([{ key: 'title', text: 'A' }])
    expect(view.hidden).toBe(2)
  })

  it('混了非标量的数组整体算隐藏', () => {
    // 半展示（把 `[1, {a: 1}]` 显示成 "1"）会让用户以为那就是全部内容。
    expect(frontmatterView({ mixed: [1, { a: 1 }] }).hidden).toBe(1)
  })

  it('没有 frontmatter 时是空的', () => {
    expect(frontmatterView({})).toEqual({ entries: [], hidden: 0 })
  })
})

describe('unresolvedNotice', () => {
  it('没有断链就没有提示', () => {
    expect(unresolvedNotice([])).toBeNull()
  })

  it('一个断链照实说', () => {
    expect(unresolvedNotice(['Notes/Gone'])).toBe('有 1 个链接指向不存在的笔记：Notes/Gone')
  })

  it('刚好三条时不加省略', () => {
    expect(unresolvedNotice(['a', 'b', 'c'])).toBe('有 3 个链接指向不存在的笔记：a、b、c')
  })

  it('超过三条时截断举例但给出准确总数', () => {
    expect(unresolvedNotice(['a', 'b', 'c', 'd', 'e'])).toBe(
      '有 5 个链接指向不存在的笔记：a、b、c 等 5 个',
    )
  })
})

describe('blockIndexFor', () => {
  const blocks = [block({ line: 1 }), block({ line: 5, block_id: 'maxmemory' })]

  it('没有锚点时不定位于任何块', () => {
    expect(blockIndexFor(blocks, null)).toBe(-1)
  })

  it('找到锚点所在的下标', () => {
    expect(blockIndexFor(blocks, 'maxmemory')).toBe(1)
  })

  it('锚点不存在时返回 -1', () => {
    // 笔记改过之后旧链接会走到这里。返回 -1 让页面停在顶部，而不是高亮一个随机的块。
    expect(blockIndexFor(blocks, 'gone')).toBe(-1)
  })

  it('空笔记也是 -1', () => {
    expect(blockIndexFor([], 'maxmemory')).toBe(-1)
  })
})
