"use client"

import { FileText, Unlink } from 'lucide-react'
import { useEffect, useMemo, useRef } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'

import { ErrorPanel } from '@/components/error-panel'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { useNote } from '@/hooks/use-note'
import type { NoteBlockView, NoteSegment } from '@/lib/api-types'
import { present } from '@/lib/errors'
import {
  blockIndexFor,
  blockText,
  frontmatterView,
  groupBlocks,
  headingTag,
  linkHref,
  unresolvedNotice,
} from '@/lib/note'
import { cn } from '@/lib/utils'

/**
 * 笔记查看页。
 *
 * 这一页最重要的事实是它**不做的事**：没有 Markdown 渲染器，没有
 * `dangerouslySetInnerHTML`，也没有清洗器。笔记在服务端就已经被拆成文本段与已解析
 * 的链接（见 `src/obsai/application/notes.py`），所以这里能渲染的东西只有两种——文本
 * 节点和路由链接。含 `<script>`、`<iframe>` 的笔记"不执行任何内容"因此不是一条需要
 * 被测试守住的行为，而是这一页唯一能表达的形状。
 *
 * 也因此 `segments` 里的链接**不可点**这一情形很重要：服务端用
 * `target_note_id: null` 说明目标不在 Vault 内，页面把它渲染成带虚线的普通文本。
 * 不猜测、不退回按路径拼链接——B-7 的验收要求可跳转的目标必须是服务端校验过的。
 *
 * 一个已知的取舍：**所有列表都渲染成无序列表。** 解析器只记了列表嵌套深度，没有记
 * 标记类型（`-` 还是 `1.`），所以 `1. 2. 3.` 的步骤会显示成圆点。修它要给 `Block`
 * 加一个字段并让 `parsed_json` 换代，那是索引的改动，不属于 B-7；见计划文档 §23.5。
 */

// --------------------------------------------------------------------------- //
// 块内容
// --------------------------------------------------------------------------- //

/**
 * 一个块的段。
 *
 * 三种走法，全部落在文本节点或路由链接上：
 *
 * - 文本段：`<span>`。
 * - 可跳转的链接：`<Link>`；嵌入（`![[Note]]`）额外标一个"嵌入"，因为这一页不展开
 *   被嵌入的内容，不标出来会让人以为正文就这些。
 * - 不可跳转的链接：带虚线的 `<span>`，`title` 说明原因。**不是 `<a>`**——一个指向
 *   不存在笔记的链接，点了只会得到一个 404，不如一开始就说清楚。
 */
function Segments({ segments }: { segments: readonly NoteSegment[] }) {
  return (
    <>
      {segments.map((segment, index) => {
        if (segment.kind === 'text') return <span key={index}>{segment.text}</span>

        const href = linkHref(segment)
        if (href === null) {
          return (
            <span
              key={index}
              title={`目标不在 Vault 内：${segment.target_path ?? segment.text}`}
              className="decoration-muted-foreground/60 underline decoration-dashed underline-offset-4"
            >
              {segment.text}
            </span>
          )
        }
        return (
          <span key={index}>
            <Link
              to={href}
              className="text-primary decoration-primary/40 hover:decoration-primary underline underline-offset-4"
            >
              {segment.text}
            </Link>
            {segment.is_embed && (
              <span className="text-muted-foreground ml-0.5 align-super text-[0.6rem]">嵌入</span>
            )}
          </span>
        )
      })}
    </>
  )
}

const HEADING_CLASS = 'text-lg font-semibold tracking-tight'

/** 块内容本体，按 `kind` 决定外壳。层级一律来自服务端，不在这里数 `#`。 */
function BlockBody({ block }: { block: NoteBlockView }) {
  const body = <Segments segments={block.segments} />

  switch (block.kind) {
    case 'heading': {
      // 标签名逐个写出来，而不是 `const Tag = headingTag(block)` 再渲染：后者会让
      // oxlint 的 react(static-components) 报警——它无法区分"渲染期创建的组件"和
      // "内置标签名"，而三个显式分支比一条 lint 豁免便宜。
      const tag = headingTag(block)
      if (tag === 'h3') return <h3 className={HEADING_CLASS}>{body}</h3>
      if (tag === 'h4') return <h4 className={HEADING_CLASS}>{body}</h4>
      return <h2 className={HEADING_CLASS}>{body}</h2>
    }
    case 'code':
      return (
        <pre className="bg-muted overflow-x-auto rounded-md p-3 text-xs">
          <code data-language={block.language ?? undefined}>{body}</code>
        </pre>
      )
    case 'list_item':
      return <li className="ml-4 list-disc">{body}</li>
    case 'blockquote':
      return <blockquote className="border-muted-foreground/30 border-l-2 pl-3">{body}</blockquote>
    case 'callout':
      return (
        <div className="bg-muted/50 border-muted-foreground/30 rounded-md border-l-2 px-3 py-2">
          {body}
        </div>
      )
    default:
      return <p>{body}</p>
  }
}

/**
 * 一个块，外加定位用的外壳。
 *
 * 外壳存在有三个理由：`?block=` 的高亮需要一块背景，滚动需要一个能 `ref` 到的元素，
 * 而 `data-slot` / `data-highlighted` 给 `scripts/web-smoke.py` 一个稳定的钩子——它用
 * `--dump-dom` 取 DOM，靠结构层级或 CSS 类去数块都不稳。两个属性都跟着 shadcn 组件
 * 已有的 `data-slot` 约定走。
 *
 * 每块都加一层 `<div>` 而不是只在被高亮时加，是为了避免高亮出现/消失时改变 DOM 结构
 * ——那会让 React 重挂整棵子树，滚动位置也跟着跳。
 */
function BlockRow({ block, highlighted }: { block: NoteBlockView; highlighted: boolean }) {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (highlighted) ref.current?.scrollIntoView({ block: 'center' })
  }, [highlighted])

  return (
    <div
      ref={ref}
      data-slot="note-block"
      data-highlighted={highlighted ? 'true' : undefined}
      className={cn(
        '-mx-2 scroll-mt-6 rounded-md px-2 transition-colors',
        block.kind === 'heading' && 'mt-3',
        highlighted && 'bg-primary/5 ring-primary/30 ring-1',
      )}
    >
      <BlockBody block={block} />
    </div>
  )
}

/**
 * 块的渲染序列。
 *
 * `groupBlocks` 负责把相邻的列表项收进一个列表元素——`<li>` 脱离 `<ul>` 是无效 HTML，
 * 浏览器会把它拆开。分组规则在 `lib/note.ts` 里，因为它能脱离 DOM 被验证。
 *
 * 下标用一张 `Map` 算一次，而不是每块 `indexOf`：既避免 O(n²)，也让"这是第几块"只有
 * 一个来源——高亮与 React 的 key 都从它取。
 */
function Blocks({ blocks, highlight }: { blocks: readonly NoteBlockView[]; highlight: number }) {
  const indexOf = useMemo(
    () => new Map(blocks.map((block, index) => [block, index])),
    [blocks],
  )

  return (
    <div className="flex flex-col gap-3 text-sm leading-7">
      {groupBlocks(blocks).map((group, groupIndex) =>
        group.kind === 'list' ? (
          <ul key={groupIndex} className="flex flex-col gap-1">
            {group.blocks.map((block) => {
              const index = indexOf.get(block) ?? -1
              return <BlockRow key={index} block={block} highlighted={index === highlight} />
            })}
          </ul>
        ) : (
          <BlockRow
            key={indexOf.get(group.block) ?? groupIndex}
            block={group.block}
            highlighted={(indexOf.get(group.block) ?? -1) === highlight}
          />
        ),
      )}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// 头部与提示
// --------------------------------------------------------------------------- //

function NoteHeader({ title, path, tags }: { title: string; path: string; tags: readonly string[] }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-start gap-2">
        <FileText className="text-muted-foreground mt-1 size-5 shrink-0" />
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          <div className="text-muted-foreground font-mono text-xs">{path}</div>
        </div>
      </div>
      {tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {tags.map((tag) => (
            <Badge key={tag} variant="secondary" className="text-xs">
              {tag}
            </Badge>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * frontmatter 一栏。
 *
 * 只展示标量（规则在 `frontmatterView`）。`hidden` 是"有、但没展开"的字段数——直接
 * 吞掉会让用户以为这篇笔记没有那些字段。
 */
function Frontmatter({ frontmatter }: { frontmatter: Record<string, unknown> }) {
  const view = useMemo(() => frontmatterView(frontmatter), [frontmatter])
  if (view.entries.length === 0 && view.hidden === 0) return null

  return (
    <div className="bg-muted/40 flex flex-col gap-1 rounded-md px-3 py-2">
      <dl className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {view.entries.map((entry) => (
          <div key={entry.key} className="flex items-baseline gap-1.5">
            <dt className="text-muted-foreground font-mono">{entry.key}</dt>
            <dd>{entry.text}</dd>
          </div>
        ))}
      </dl>
      {view.hidden > 0 && (
        <div className="text-muted-foreground text-xs">
          另有 {view.hidden} 个字段不是标量，未在此展示。
        </div>
      )}
    </div>
  )
}

/** 断链提示。文案由 `unresolvedNotice` 给，页面不自己拼。 */
function UnresolvedNotice({ links }: { links: readonly string[] }) {
  const notice = unresolvedNotice(links)
  if (notice === null) return null

  return (
    <Alert>
      <Unlink />
      <AlertTitle>部分 WikiLink 无法跳转</AlertTitle>
      <AlertDescription>{notice}</AlertDescription>
    </Alert>
  )
}

function NoteSkeleton() {
  return (
    <div className="flex flex-col gap-3">
      <Skeleton className="h-7 w-48" />
      <Skeleton className="h-3 w-32" />
      <div className="mt-4 flex flex-col gap-3">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-11/12" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------- //
// 笔记页
// --------------------------------------------------------------------------- //

export default function NotePage() {
  // `?block=` 是从引用卡片过来的定位参数：引用带了 `^锚点` 就一起传过来，页面滚到
  // 那一段并高亮。放在地址栏而不是组件 state 里，理由和搜索页一样——`web-smoke.py`
  // 用 `--dump-dom` 跑，它不会点击。
  const { noteId } = useParams()
  const [searchParams] = useSearchParams()
  const { data, error, isLoading, refetch } = useNote(noteId ?? null)

  const target = searchParams.get('block')
  const highlight = data ? blockIndexFor(data.blocks, target) : -1
  const failure = error ? present(error) : null
  // 有块不等于有正文：整段都是行内 HTML 的段落解析出来是空文本。这时给一句话，比
  // 留一片空白好——空白看起来像加载失败。
  const hasContent = data ? data.blocks.some((block) => blockText(block) !== '') : false

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 p-6">
      {failure && (
        <ErrorPanel failure={failure} onRetry={failure.retryable ? () => refetch() : undefined} />
      )}

      {isLoading && <NoteSkeleton />}

      {data !== undefined && (
        <>
          <NoteHeader title={data.title} path={data.path} tags={data.tags} />
          <Frontmatter frontmatter={data.frontmatter} />
          <UnresolvedNotice links={data.unresolved_links} />
          {hasContent ? (
            <Blocks blocks={data.blocks} highlight={highlight} />
          ) : (
            <div className="text-muted-foreground py-12 text-center text-sm">
              这篇笔记除了 frontmatter 之外没有可显示的正文。
            </div>
          )}
        </>
      )}

      {/* 加载失败与"没给 ID"两种情况下也要有 h1：这一页的一级标题平时是笔记标题，
          没有笔记时整页就没有一级标题了，屏幕阅读器与页头的"跳到主内容"都失去落点，
          `web-smoke.py` 的逐路由 h1 断言也会红。 */}
      {!isLoading && data === undefined && (
        <>
          <h1 className="text-2xl font-semibold tracking-tight">笔记</h1>
          {failure === null && (
            <div className="text-muted-foreground py-12 text-center text-sm">
              没有指定笔记。回到搜索页挑一条结果，或从问答页的引用进来。
            </div>
          )}
        </>
      )}
    </div>
  )
}
