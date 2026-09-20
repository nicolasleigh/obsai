"use client"

import { CornerDownLeft, Loader2, Quote, SearchX } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { ErrorPanel } from '@/components/error-panel'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { useAsk } from '@/hooks/use-ask'
import type { AskOutcome, CitationView } from '@/lib/api-types'
import { classifyAskOutcome, degradationNotices, splitAnswerSegments, type AskKind } from '@/lib/ask'
import { present } from '@/lib/errors'
import { noteHref } from '@/lib/note'
import { cn } from '@/lib/utils'

/** 引用卡片的 DOM id。角标点击后要滚到这里。 */
function citationAnchor(citationId: string): string {
  return `citation-${citationId}`
}

/** 高亮持续时长，毫秒。够眼睛跟过去，又不至于一直闪。 */
const HIGHLIGHT_MS = 1_600

/**
 * 弃答时补一句"下一步做什么"。
 *
 * 服务端的 `text` 说的是**发生了什么**（"未找到可用于回答的笔记证据。"），
 * 它没法知道用户接下来该做什么——那是界面的事。分开写而不是把两句拼在一起，
 * 是因为服务端那句在 CLI 里也要原样打印。
 */
const ABSTENTION_HINTS: Record<Exclude<AskKind, 'answered'>, string> = {
  empty_question: '在输入框里写下一个问题再提交。',
  no_evidence:
    '换一种说法，或先运行 obsai index update 把索引补齐；语义检索还需要 obsai index embeddings。',
  citation_failed:
    '模型没有给出可核验的引用，这段回答已被丢弃。重新提问通常能拿到一版带有效引用的回答。',
}

// --------------------------------------------------------------------------- //
// 回答正文与引用
// --------------------------------------------------------------------------- //

/**
 * 正文里的一个引用角标。
 *
 * 内容渲染成 ``S1`` 而不是 ``[S1]``：方括号的视觉效果由角标本身提供，再写一遍
 * 会变成 ``[[S1]]``。
 */
function CitationBadge({
  citation,
  onActivate,
}: {
  citation: CitationView
  onActivate: () => void
}) {
  return (
    <button
      type="button"
      onClick={onActivate}
      title={citation.location}
      aria-label={`引用 ${citation.citation_id}：${citation.location}`}
      className="ring-offset-background focus:outline-hidden focus:ring-2 focus:ring-ring focus:ring-offset-2 mx-0.5 inline-flex translate-y-[-1px] rounded bg-primary/10 px-1 align-baseline font-mono text-[0.7rem] font-medium text-primary hover:bg-primary/20"
    >
      {citation.citation_id}
    </button>
  )
}

/**
 * 回答正文，引用编号渲染成可点角标。
 *
 * `whitespace-pre-wrap` 保留模型输出里的换行：回答常常是分点的，把它压成一行
 * 会让"第一点第二点"连成一段。
 */
function AnswerBody({
  outcome,
  onActivate,
}: {
  outcome: AskOutcome
  onActivate: (citationId: string) => void
}) {
  const byId = useMemo(
    () => new Map(outcome.citations.map((citation) => [citation.citation_id, citation])),
    [outcome.citations],
  )
  const segments = useMemo(
    () => splitAnswerSegments(outcome.text, outcome.citations),
    [outcome.text, outcome.citations],
  )

  return (
    <p className="text-sm leading-7 whitespace-pre-wrap">
      {segments.map((segment, index) => {
        if (segment.kind === 'text') return <span key={index}>{segment.text}</span>
        const citation = byId.get(segment.citationId)
        // `splitAnswerSegments` 只会为已知编号产出角标，所以这里不可达。留一条
        // 退化路径是为了万一它变了，页面渲染出的是文本而不是崩溃。
        if (citation === undefined) return <span key={index}>[{segment.citationId}]</span>
        return (
          <CitationBadge
            key={index}
            citation={citation}
            onActivate={() => onActivate(segment.citationId)}
          />
        )
      })}
    </p>
  )
}

/**
 * 一张引用卡片。
 *
 * 标题是指向笔记页的链接，并且带上 `?block=`：`citation.block_id` 来自证据所在的块，
 * 笔记页会滚到那一段并高亮。这是"点击跳转笔记定位"落地的地方——角标只是把视线引到
 * 这张卡片上，真正回到原文要再点一次标题。
 *
 * `block_id` 为 `null` 时只打开笔记本身。它只在证据所在块带 `^锚点` 时才有值，所以
 * 大多数引用会停在笔记顶部，而不是假装能定位。
 */
function CitationCard({
  citation,
  highlighted,
}: {
  citation: CitationView
  highlighted: boolean
}) {
  return (
    <Card
      id={citationAnchor(citation.citation_id)}
      className={cn('gap-2 scroll-mt-6 transition-shadow', highlighted && 'ring-2 ring-primary')}
    >
      <CardHeader className="gap-1">
        <div className="flex items-center gap-2">
          <Badge variant="secondary" className="font-mono">
            {citation.citation_id}
          </Badge>
          <CardTitle className="text-sm">
            <Link
              to={noteHref(citation.note_id, citation.block_id)}
              className="decoration-primary/40 hover:decoration-primary underline-offset-4 hover:underline"
            >
              {citation.title}
            </Link>
          </CardTitle>
          {citation.truncated && (
            <Badge variant="outline" className="text-xs">
              已截断
            </Badge>
          )}
        </div>
        {/* `location` 是服务端拼好的 `path > heading ^block`，直接展示，不再自己拼。 */}
        <CardDescription className="font-mono text-xs">{citation.location}</CardDescription>
      </CardHeader>
    </Card>
  )
}

function CitationList({
  outcome,
  highlighted,
}: {
  outcome: AskOutcome
  highlighted: string | null
}) {
  if (outcome.citations.length === 0) return null
  return (
    <div className="flex flex-col gap-2">
      <h2 className="text-muted-foreground text-xs font-medium tracking-wide uppercase">引用来源</h2>
      {outcome.citations.map((citation) => (
        <CitationCard
          key={citation.citation_id}
          citation={citation}
          highlighted={highlighted === citation.citation_id}
        />
      ))}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// 弃答
// --------------------------------------------------------------------------- //

function AbstentionNotice({ kind, text }: { kind: Exclude<AskKind, 'answered'>; text: string }) {
  return (
    <Alert variant={kind === 'citation_failed' ? 'destructive' : 'default'}>
      <SearchX />
      <AlertTitle>{text}</AlertTitle>
      <AlertDescription>{ABSTENTION_HINTS[kind]}</AlertDescription>
    </Alert>
  )
}

function AnswerSkeleton() {
  return (
    <Card className="gap-3">
      <CardHeader className="gap-2">
        <Skeleton className="h-4 w-24" />
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-11/12" />
        <Skeleton className="h-3 w-2/3" />
      </CardContent>
    </Card>
  )
}

// --------------------------------------------------------------------------- //
// 问答页
// --------------------------------------------------------------------------- //

export default function AskPage() {
  // **已提交的问题**放在 URL 里，输入框的草稿只活在组件内。理由有两条：
  //
  // 1. 一次问答要花钱和时间，不能每敲一个字符就发一次请求。URL 记录的是"问过的
  //    问题"，于是刷新、回链、前进后退都能复现那一次问答，而打字不会。
  // 2. `scripts/web-smoke.py` 用 `--dump-dom` 在真实浏览器里跑，而它**不会打字**。
  //    把问题放进地址栏（`#/ask?q=redis`）是唯一能让冒烟脚本走到回答那条路径的办法。
  const [searchParams, setSearchParams] = useSearchParams()
  const asked = (searchParams.get('q') ?? '').trim()
  const [draft, setDraft] = useState(asked)
  const [syncedFrom, setSyncedFrom] = useState(asked)
  const [highlighted, setHighlighted] = useState<string | null>(null)

  // 地址栏里的问题换了（后退/前进、手改 URL）就把输入框同步回来。
  //
  // 用"渲染期根据 props 调整 state"而不是 useEffect：effect 会在提交之后再渲染
  // 一次，输入框会闪一下，而且 react-hooks 的 lint 会直接拦下来。这个写法是
  // React 文档里给这种情况的官方答案，且是幂等的——提交时 `asked` 正好等于
  // `draft.trim()`，`setDraft` 会因为值没变而直接跳过。打字时 `asked` 不变，
  // 整段不会执行。
  if (syncedFrom !== asked) {
    setSyncedFrom(asked)
    setDraft(asked)
  }

  useEffect(() => {
    if (highlighted === null) return
    const timer = window.setTimeout(() => setHighlighted(null), HIGHLIGHT_MS)
    return () => window.clearTimeout(timer)
  }, [highlighted])

  const request = useMemo(() => (asked === '' ? null : { query: asked }), [asked])
  const { data, error, isFetching, refetch } = useAsk(request)

  const submit = () => {
    const next = draft.trim()
    if (next === '') return
    if (next === asked) {
      // 同一个问题再问一次：这是"重新生成"，不是新的一次提问。显式 refetch 才
      // 能穿过 useAsk 的 staleTime。
      void refetch()
      return
    }
    setSearchParams({ q: next })
  }

  const kind = data ? classifyAskOutcome(data) : null
  const notices = data ? degradationNotices(data) : []
  // 只在重试有可能成功时才给按钮。缺 API Key 这类错误重试一百次还是同一句，
  // 摆一个"重试"按钮等于让用户白点一次。
  const failure = error ? present(error) : null

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 p-6">
      <div className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">问答</h1>

        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            submit()
          }}
        >
          <Input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="就你的笔记提问…"
            aria-label="问题"
            className="flex-1"
          />
          <Button type="submit" disabled={draft.trim() === ''}>
            {isFetching ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <CornerDownLeft className="size-4" />
            )}
            提问
          </Button>
        </form>

        {/* 与搜索页同一句话术：降级是数据，不是错误。 */}
        {notices.length > 0 && (
          <Alert variant="destructive">
            <AlertTitle>检索已降级</AlertTitle>
            <AlertDescription className="flex flex-col gap-1">
              {notices.map((notice, index) => (
                <span key={index}>{notice}</span>
              ))}
            </AlertDescription>
          </Alert>
        )}
      </div>

      {failure && (
        <ErrorPanel
          failure={failure}
          onRetry={failure.retryable ? () => refetch() : undefined}
        />
      )}

      {isFetching && data === undefined && <AnswerSkeleton />}

      {data && kind === 'answered' && (
        <div className="flex flex-col gap-6">
          <Card className="gap-3">
            <CardHeader className="gap-1">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Quote className="size-4" />
                回答
              </CardTitle>
            </CardHeader>
            <CardContent>
              <AnswerBody outcome={data} onActivate={setHighlighted} />
            </CardContent>
          </Card>

          <CitationList outcome={data} highlighted={highlighted} />
        </div>
      )}

      {data && kind !== null && kind !== 'answered' && (
        <AbstentionNotice kind={kind} text={data.text} />
      )}

      {asked === '' && (
        <div className="text-muted-foreground py-12 text-center text-sm">
          输入问题开始问答。回答只依据索引里的笔记证据，并给出可核验的引用。
        </div>
      )}
    </div>
  )
}
