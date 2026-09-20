"use client"

import { Filter, Search, X } from 'lucide-react'
import { useDeferredValue, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { ErrorPanel } from '@/components/error-panel'
import { RemoteConsentDialog } from '@/components/remote-consent-dialog'
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import { Input } from '@/components/ui/input'
import {
  ToggleGroup,
  ToggleGroupItem,
} from '@/components/ui/toggle-group'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { useSearch } from '@/hooks/use-search'
import type {
  ConsentApproval,
  SearchFilters,
  SearchMode,
  SearchRequest,
  SearchResult,
} from '@/lib/api-types'
import { consentFromError, consentOutcome, failureNotice, isApprovalNotice } from '@/lib/consent'
import { ApiError, present } from '@/lib/errors'
import {
  DEFAULT_SEARCH_MODE,
  SEARCH_MODES,
  describeFilters,
  formatScore,
  hasFilters,
  isDegraded,
  parseMode,
  presentWarnings,
} from '@/lib/search'

// --------------------------------------------------------------------------- //
// Consent
// --------------------------------------------------------------------------- //

/**
 * 用户对某一次查询做过的决定。
 *
 * 与**那次请求对象**绑在一起存，而不是单独存一个 `approval`：`request` 是 `useMemo`
 * 的产物，查询、模式或筛选器一变它就是新对象，旧决定于是自动失效。用 effect 手动清空
 * 会在"变了的这一帧"里留下一个已经不适用的批准，而它恰好会被当成有效批准发出去——
 * 服务端会拒（`approval_covers` 比对 `query_hash`），用户看到的是一次莫名其妙的失败。
 */
type Decision =
  | { kind: 'approved'; request: SearchRequest; approval: ConsentApproval }
  | { kind: 'declined'; request: SearchRequest }

/**
 * 同意提示条的文案。
 *
 * `declined`（用户自己拒绝的）与 `stale`（批准过但已经不适用）必须分开写：一个说
 * "按你的选择降级了"，另一个说"你批准的那次失效了"。合并成一句"需要批准"会让前者
 * 看起来像系统又在催一遍，后者看起来像自己没批过。
 */
function consentPrompt(
  declined: boolean,
  stale: boolean,
): { title: string; body: string; action: string } {
  if (declined) {
    return {
      title: '已按你的选择降级为关键词结果',
      body: '这次查询没有发送到任何远程服务。语义检索会更准一些，随时可以改主意。',
      action: '重新考虑',
    }
  }
  if (stale) {
    return {
      title: '之前那次批准已经不适用了',
      body: '查询或索引变过，或者批准已经超过有效期，所以这次只用了关键词检索。',
      action: '重新批准',
    }
  }
  return {
    title: '可以启用语义检索',
    body: '这次查询只用了关键词检索。语义检索更准，但需要把查询文本发送到远程嵌入服务。',
    action: '查看并批准',
  }
}

// --------------------------------------------------------------------------- //
// Filter bar
// --------------------------------------------------------------------------- //

function FilterChip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <Badge variant="secondary" className="gap-1 pr-1">
      {label}
      <button
        type="button"
        onClick={onRemove}
        className="ring-offset-background focus:outline-hidden focus:ring-2 focus:ring-ring focus:ring-offset-2 rounded-full p-0.5 hover:bg-secondary-foreground/10"
        aria-label={`移除筛选器 ${label}`}
      >
        <X className="size-3" />
      </button>
    </Badge>
  )
}

function FilterBar({
  filters,
  onChange,
}: {
  filters: SearchFilters
  onChange: (f: SearchFilters) => void
}) {
  const [open, setOpen] = useState(false)
  const chips = useMemo(() => describeFilters(filters), [filters])

  const addTag = () => {
    onChange({ ...filters, tags: [...(filters.tags ?? []), ''] })
    setOpen(false)
  }

  const addFolder = () => {
    onChange({ ...filters, folder: '' })
    setOpen(false)
  }

  const removeTag = (index: number) => {
    const next = [...(filters.tags ?? [])]
    next.splice(index, 1)
    onChange({ ...filters, tags: next.length ? next : undefined })
  }

  const updateTag = (index: number, value: string) => {
    const next = [...(filters.tags ?? [])]
    next[index] = value
    onChange({ ...filters, tags: next })
  }

  const removeFolder = () => {
    onChange({ ...filters, folder: undefined })
  }

  const clearAll = () => {
    onChange({})
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        {chips.map((chip, i) => (
          <FilterChip
            key={chip + i}
            label={chip}
            onRemove={() => {
              // Heuristic: tag chips start with "标签:"
              if (chip.startsWith('标签:')) {
                const idx = (filters.tags ?? []).findIndex((t) => chip.includes(t))
                if (idx >= 0) removeTag(idx)
              } else if (chip.startsWith('目录:')) {
                removeFolder()
              }
            }}
          />
        ))}
        <Button
          variant="outline"
          size="sm"
          className="h-7 gap-1 text-xs"
          onClick={() => setOpen(true)}
        >
          <Filter className="size-3" />
          筛选器
        </Button>
        {hasFilters(filters) && (
          <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={clearAll}>
            清除
          </Button>
        )}
      </div>

      {/* Inline tag inputs */}
      {(filters.tags?.length ?? 0) > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {(filters.tags ?? []).map((tag, i) => (
            <div key={i} className="flex items-center gap-1">
              <Input
                value={tag}
                onChange={(e) => updateTag(i, e.target.value)}
                placeholder="标签"
                className="h-7 w-32 text-xs"
              />
              <button
                type="button"
                onClick={() => removeTag(i)}
                className="text-muted-foreground hover:text-foreground"
                aria-label="移除标签"
              >
                <X className="size-3" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Inline folder input */}
      {filters.folder !== undefined && (
        <div className="flex items-center gap-1">
          <Input
            value={filters.folder ?? ''}
            onChange={(e) => onChange({ ...filters, folder: e.target.value })}
            placeholder="目录路径"
            className="h-7 w-48 text-xs"
          />
          <button
            type="button"
            onClick={removeFolder}
            className="text-muted-foreground hover:text-foreground"
            aria-label="移除目录筛选"
          >
            <X className="size-3" />
          </button>
        </div>
      )}

      <CommandDialog open={open} onOpenChange={setOpen} title="添加筛选器">
        <CommandInput placeholder="搜索筛选器类型…" />
        <CommandList>
          <CommandEmpty>没有匹配的筛选器</CommandEmpty>
          <CommandGroup heading="筛选器">
            <CommandItem onSelect={addTag}>按标签筛选</CommandItem>
            <CommandItem onSelect={addFolder}>按目录筛选</CommandItem>
          </CommandGroup>
        </CommandList>
      </CommandDialog>
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Result card
// --------------------------------------------------------------------------- //

/**
 * 一条结果。
 *
 * 标题是指向笔记页的链接。`note_id` 而不是 `path` 做路由参数：路径里可能有空格、
 * 中文与斜杠，而且笔记改名之后旧路径就失效了——ID 不会。
 *
 * 只有标题是链接，整张卡片不是：卡片里还有标签、路径、分数，做成整块可点会让"复制
 * 那段路径"变得别扭，而路径恰恰是本地工具里最常被复制的东西。
 */
function ResultCard({ result }: { result: SearchResult }) {
  return (
    <Card className="gap-3">
      <CardHeader className="gap-1">
        <CardTitle className="text-base">
          <Link
            to={`/notes/${result.note_id}`}
            className="decoration-primary/40 hover:decoration-primary underline-offset-4 hover:underline"
          >
            {result.title}
          </Link>
        </CardTitle>
        <CardDescription className="font-mono text-xs">{result.path}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {result.heading_path.length > 0 && (
          <div className="text-muted-foreground text-xs">
            {result.heading_path.join(' / ')}
          </div>
        )}
        <p className="text-sm">{result.snippet}</p>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Badge variant="outline">{result.source}</Badge>
          <span>{formatScore(result.score)}</span>
          {result.sources.length > 0 && (
            <span className="text-muted-foreground">
              ({result.sources.join(', ')})
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

function ResultSkeleton() {
  return (
    <Card className="gap-3">
      <CardHeader className="gap-1">
        <Skeleton className="h-5 w-32" />
        <Skeleton className="h-3 w-48" />
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-2/3" />
        <div className="flex items-center gap-2">
          <Skeleton className="h-5 w-16" />
          <Skeleton className="h-3 w-12" />
        </div>
      </CardContent>
    </Card>
  )
}

// --------------------------------------------------------------------------- //
// Search page
// --------------------------------------------------------------------------- //

export default function SearchPage() {
  // 查询串与模式以 URL 为准：地址栏里的 `#/search?q=redis&mode=keyword` 一打开就能
  // 复现这次搜索。除了可分享、可回链（B-7 的笔记页会想要一条"回到这次搜索"），它
  // 也是 `scripts/web-smoke.py` 能在真实浏览器里跑到搜索结果那条路径的唯一办法——
  // `--dump-dom` 不会打字。
  const [searchParams, setSearchParams] = useSearchParams()
  const [query, setQuery] = useState(() => searchParams.get('q') ?? '')
  const [mode, setMode] = useState<SearchMode>(() => parseMode(searchParams.get('mode')))
  const [filters, setFilters] = useState<SearchFilters>({})
  const [decision, setDecision] = useState<Decision | null>(null)
  const deferredQuery = useDeferredValue(query)

  // 对话框的开合也进 URL，理由与 `q` / `mode` 相同：`--dump-dom` 不会点击，而 B-8
  // 点名的产出就是这个对话框——不把它变成可寻址的状态，无头浏览器就只能验到"入口
  // 按钮渲染出来了"。它同时也是一个合理的用户动作（"打开就停在同意对话框上"的链接）。
  const dialogOpen = searchParams.get('consent') === '1'
  const setDialogOpen = (open: boolean) => {
    const next = new URLSearchParams(searchParams)
    if (open) next.set('consent', '1')
    else next.delete('consent')
    setSearchParams(next, { replace: true })
  }

  // 回写用 `replace`，否则每敲一个字符都会在历史里留一条记录，"后退"变成逐字回删。
  // 只在真的变了的时候写，避免 effect 自激。
  useEffect(() => {
    const next = new URLSearchParams()
    if (query !== '') next.set('q', query)
    if (mode !== DEFAULT_SEARCH_MODE) next.set('mode', mode)
    if (dialogOpen) next.set('consent', '1')
    if (next.toString() === searchParams.toString()) return
    setSearchParams(next, { replace: true })
  }, [query, mode, dialogOpen, searchParams, setSearchParams])

  const request = useMemo<SearchRequest | null>(() => {
    const trimmed = deferredQuery.trim()
    if (!trimmed) return null
    return {
      query: trimmed,
      mode,
      filters,
    }
  }, [deferredQuery, mode, filters])

  // 决定只在它所属的那次请求上有效（见 `Decision` 的说明）。
  const current = decision !== null && decision.request === request ? decision : null
  const approval = current?.kind === 'approved' ? current.approval : null
  const declined = current?.kind === 'declined'

  const { data, error, isLoading, refetch } = useSearch(request, approval)
  const degraded = data !== undefined && isDegraded(data)
  const warnings = data ? presentWarnings(data) : []
  // 只在重试有可能成功时才给按钮：`config` 这类错误重试一百次还是同一句。
  const failure = error ? present(error) : null

  const outcome = consentOutcome(data, approval)
  // `mode=semantic` 不能降级：没批准时服务端直接 409，挑战只能从错误信封里取。
  // 没有这个分支，选"语义"模式的用户会看到一个"需要批准"的提示却没有任何东西可批准。
  const errorConsent = error instanceof ApiError ? consentFromError(error) : null
  // 探针给了挑战、但批准还没到位（或已经不适用）——两种情况都由提示条接手。
  const offered =
    outcome.kind === 'pending' || outcome.kind === 'rejected' ? outcome : null
  const challenge = offered === null ? errorConsent : offered.consent
  const blocked = errorConsent !== null
  const unavailable = outcome.kind === 'unavailable' ? outcome.failure : null
  const prompt = consentPrompt(declined, outcome.kind === 'rejected')

  // 提示条已经说过"这次只用了关键词检索"，就不必再用红色告警说第二遍（见
  // `isApprovalNotice`）。`warnings` 本身不删——它还要原样进告警当排查证据。
  const notices = offered === null ? warnings : warnings.filter((w) => !isApprovalNotice(w))

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 p-6">
      <div className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">搜索</h1>

        {/* Search bar */}
        <div className="flex flex-col gap-3">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索笔记…"
              className="pl-9"
            />
          </div>

          <ToggleGroup
            type="single"
            value={mode}
            onValueChange={(v) => {
              if (v) setMode(v as SearchMode)
            }}
            variant="outline"
            size="sm"
          >
            {SEARCH_MODES.map((m) => (
              <ToggleGroupItem key={m.value} value={m.value}>
                {m.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>

          <FilterBar filters={filters} onChange={setFilters} />
        </div>
      </div>

      {/* Warnings */}
      {degraded && notices.length > 0 && (
        <Alert variant="destructive">
          <AlertTitle>检索已降级</AlertTitle>
          <AlertDescription className="flex flex-col gap-1">
            {/* 中文先、原文后，与错误面板同一套顺序：前者说"下一步做什么"，
                后者是排查时的证据。两者说同一件事不是冗余——`warnings` 是英文的
                领域原话，只给英文会让"索引里没有向量"和"后端连不上"读起来一样。 */}
            {unavailable !== null && <span>{failureNotice(unavailable)}</span>}
            {notices.map((w, i) => (
              <span key={i}>{w}</span>
            ))}
          </AlertDescription>
        </Alert>
      )}

      {/* Consent */}
      {blocked && (
        <Alert>
          <AlertTitle>语义模式需要先批准这次查询</AlertTitle>
          <AlertDescription className="flex flex-col gap-3">
            <span>
              {declined
                ? '你拒绝了这次查询，语义模式没有可显示的结果——它不能像混合模式那样降级为关键词检索。'
                : '语义检索会把查询文本发送到远程嵌入服务。这个模式不能降级为关键词检索，所以在你做出决定之前它没有结果。'}
            </span>
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                onClick={() => setDialogOpen(true)}
                disabled={challenge === null}
              >
                {declined ? '重新考虑' : '查看并批准'}
              </Button>
              <Button size="sm" variant="outline" onClick={() => setMode('hybrid')}>
                改用混合模式
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}

      {!blocked && challenge !== null && (
        <Alert>
          <AlertTitle>{prompt.title}</AlertTitle>
          <AlertDescription className="flex flex-col gap-3">
            <span>{prompt.body}</span>
            <div>
              <Button size="sm" variant="outline" onClick={() => setDialogOpen(true)}>
                {prompt.action}
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}

      {outcome.kind === 'approved' && (
        <p className="text-muted-foreground text-xs">
          本次结果包含语义检索——查询已发送至远程嵌入服务。
        </p>
      )}

      {/* Error */}
      {/* 同意被拦下时不重复报错：那两条提示说的是同一件事，而提示条里还带着
          两个可点的出路（批准 / 换模式），错误面板一个都没有。 */}
      {failure && !blocked && (
        <ErrorPanel
          failure={failure}
          onRetry={failure.retryable ? () => refetch() : undefined}
        />
      )}

      {/* Results */}
      {isLoading && (
        <div className="flex flex-col gap-3">
          <ResultSkeleton />
          <ResultSkeleton />
          <ResultSkeleton />
        </div>
      )}

      {!isLoading && data && (
        <div className="flex flex-col gap-3">
          {data.results.length === 0 ? (
            <div className="text-muted-foreground py-12 text-center">
              {query.trim() ? '没有找到匹配的结果' : '输入关键词开始搜索'}
            </div>
          ) : (
            data.results.map((result) => (
              <ResultCard key={result.chunk_id} result={result} />
            ))
          )}
        </div>
      )}

      <RemoteConsentDialog
        consent={challenge}
        query={request?.query ?? ''}
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onApproved={(approved) => {
          if (request === null) return
          setDecision({ kind: 'approved', request, approval: approved })
        }}
        onRejected={() => {
          if (request === null) return
          setDecision({ kind: 'declined', request })
        }}
      />
    </div>
  )
}
