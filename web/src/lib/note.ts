/**
 * 笔记页的纯逻辑。
 *
 * 这里的每个函数都是"服务端给了数据、但怎么显示还需要一条规则"的地方。规则放在
 * 这里而不是组件里，是因为它们能脱离 DOM 被验证——而笔记页最需要被验证的两件事
 * （哪些链接可点、标题渲染成几级）恰好都是规则。
 *
 * 有一条贯穿全篇的约束：**这里不解析 Markdown，也不碰笔记原文。** 服务端已经把
 * 笔记拆成文本段与已解析的链接，前端只负责把段渲染成文本节点。任何"再读一遍原文"
 * 的做法都会把 B-7 的安全边界从"结构上不可能"降级成"取决于正则写对没有"。
 */

import type { NoteBlockView, NoteSegment } from './api-types'

/**
 * 指向一篇笔记（可带块锚点）的路由地址。
 *
 * 单独抽出来是因为它有两个来源：笔记正文里的 WikiLink（`linkHref`），以及问答页
 * 的引用卡片——引用的 `block_id` 来自证据所在的块，把用户直接送到那一段是 B-6 那句
 * "点击跳转笔记定位"真正落地的地方。两处各拼一次的话，锚点参数名迟早会分叉。
 *
 * `null` 锚点表示"只打开笔记"：页面据此停在顶部，而不是滚到一个猜出来的位置。
 */
export function noteHref(noteId: string, blockId: string | null): string {
  const href = `/notes/${noteId}`
  if (blockId === null) return href
  return `${href}?block=${encodeURIComponent(blockId)}`
}

/**
 * 链接的跳转目标；`null` 表示这个链接**不可点**。
 *
 * 判据只有一条：`target_note_id` 是否为 `null`。那是服务端的结论——索引在写入时
 * 已经拿 `notes` 表查过目标是否存在，所以这里不做任何猜测、也不拿 `target_path`
 * 去试。B-7 的验收「WikiLink 只跳转服务端校验过的 Vault 内目标」就是这一行。
 *
 * 只有带 `^锚点` 的块链接会带上 `?block=`：`[[Note#标题]]` 没有锚点，只能打开笔记
 * 本身，不假装能定位到某个标题。
 */
export function linkHref(segment: NoteSegment): string | null {
  if (segment.kind !== 'link' || segment.target_note_id === null) return null
  return noteHref(segment.target_note_id, segment.target_block_id)
}

/**
 * 标题块渲染成几级标签。
 *
 * 页面本身已经有一个 `<h1>`（笔记标题），所以笔记里的 `#` 是文档大纲的第二级。
 * 直接把 `level` 当标签名会让一页出现两个 `<h1>`，屏幕阅读器会认为文档有两个标题。
 * 三级以上统一压到 `h4`：再深的层级在侧栏宽度里已经没有视觉差别，而无限展开只会
 * 让大纲越来越难读。
 */
export function headingTag(block: NoteBlockView): 'h2' | 'h3' | 'h4' {
  const level = block.level ?? 1
  if (level <= 1) return 'h2'
  return level === 2 ? 'h3' : 'h4'
}

/** 块的纯文本。渲染兜底与测试用——页面本身逐段渲染，不拼整串。 */
export function blockText(block: NoteBlockView): string {
  return block.segments.map((segment) => segment.text).join('')
}

export type BlockGroup =
  | { kind: 'list'; blocks: readonly NoteBlockView[] }
  | { kind: 'block'; block: NoteBlockView }

/**
 * 把相邻的 `list_item` 合成一个列表。
 *
 * 解析器给每个列表项一个独立的块（`Block` 列表是扁平的，没有嵌套结构），所以一个
 * 三项列表就是三个连续的 `list_item`。逐个渲染会得到三个没有 `<ul>` 的 `<li>`——
 * 浏览器会把它们拆开并丢掉序号，而且嵌套列表与相邻的两个列表会无法区分。这是唯一
 * 需要跨块看一步的地方，所以它是一条规则，不是组件里的一个 if。
 */
export function groupBlocks(blocks: readonly NoteBlockView[]): readonly BlockGroup[] {
  const groups: BlockGroup[] = []
  for (const block of blocks) {
    const last = groups[groups.length - 1]
    if (block.kind === 'list_item' && last !== undefined && last.kind === 'list') {
      groups[groups.length - 1] = { kind: 'list', blocks: [...last.blocks, block] }
      continue
    }
    groups.push(
      block.kind === 'list_item' ? { kind: 'list', blocks: [block] } : { kind: 'block', block },
    )
  }
  return groups
}

export type FrontmatterEntry = { key: string; text: string }

export type FrontmatterView = {
  entries: readonly FrontmatterEntry[]
  /** 值不是标量、没法直接展示的字段数。 */
  hidden: number
}

/**
 * frontmatter 里可以展示的部分。
 *
 * 只收标量，以及标量数组（`tags: [a, b]` 是最常见的形状）。嵌套对象不展开：YAML 里
 * 能塞进多少层，页面就得跟着长多少层，而这一栏的用途是"这篇笔记标了什么"，不是把
 * frontmatter 编辑器做出来。数一下有几个比默默吞掉好——用户至少知道那里有东西。
 */
export function frontmatterView(frontmatter: Record<string, unknown>): FrontmatterView {
  const entries: FrontmatterEntry[] = []
  let hidden = 0
  for (const [key, value] of Object.entries(frontmatter)) {
    const text = scalarText(value)
    if (text === null) hidden += 1
    else entries.push({ key, text })
  }
  return { entries, hidden }
}

function scalarText(value: unknown): string | null {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) {
    const parts = value.map(scalarText)
    return parts.length > 0 && parts.every((part) => part !== null) ? parts.join('、') : null
  }
  return null
}

/** 提示里最多列几个断链。 */
const UNRESOLVED_SAMPLE = 3

/**
 * 断链提示；一条都没有时返回 `null`。
 *
 * 上限三条：这一栏是解释"为什么有些链接点不动"，不是把笔记的断链全列一遍。但数量
 * 给的是准确的，因为那才是用户真正想知道的那个数。
 */
export function unresolvedNotice(links: readonly string[]): string | null {
  if (links.length === 0) return null
  const shown = links.slice(0, UNRESOLVED_SAMPLE).join('、')
  const rest = links.length > UNRESOLVED_SAMPLE ? ` 等 ${links.length} 个` : ''
  return `有 ${links.length} 个链接指向不存在的笔记：${shown}${rest}`
}

/**
 * `?block=` 指向的块下标；找不到返回 `-1`。
 *
 * 只认 `block_id`，也就是笔记自己的 `^锚点`。没有锚点的引用（比如 `[[Note]]`）只是
 * 打开笔记，不假装能定位到某一段——页面滚到一个猜出来的位置比停在顶部更糟。
 */
export function blockIndexFor(blocks: readonly NoteBlockView[], blockId: string | null): number {
  if (blockId === null) return -1
  return blocks.findIndex((block) => block.block_id === blockId)
}
