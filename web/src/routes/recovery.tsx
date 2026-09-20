import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  FileText,
  History,
  LifeBuoy,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
} from 'lucide-react'
import { useSearchParams } from 'react-router'

import { DiffSummary } from '@/components/diff-summary'
import { DiffViewer } from '@/components/diff-viewer'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import {
  TRANSACTIONS_QUERY_KEY,
  TRANSACTION_RECOVERY_QUERY_KEY,
  useTransactionRecovery,
  useTransactions,
} from '@/hooks/use-transactions'
import { api } from '@/lib/api'
import type { ChangeOutcome, DiffSummaryItem, JournalView } from '@/lib/api-types'
import { groupDiffByFile } from '@/lib/diff'
import { present, type ErrorPresentation } from '@/lib/errors'

const UNFINISHED_STATUSES = new Set(['applying', 'rolling_back', 'recovery_required'])

function getStatusBadge(status: string) {
  if (status === 'recovery_required' || status === 'rolling_back') {
    return <Badge variant="destructive">需恢复 ({status})</Badge>
  }
  if (status === 'applying') {
    return <Badge className="bg-amber-500 text-white hover:bg-amber-600">中断进行中 ({status})</Badge>
  }
  if (status === 'index_dirty') {
    return <Badge className="bg-blue-500 text-white hover:bg-blue-600">索引滞后 ({status})</Badge>
  }
  if (status === 'committed') {
    return <Badge variant="outline" className="border-emerald-500 text-emerald-600 dark:text-emerald-400">已提交</Badge>
  }
  return <Badge variant="outline">{status}</Badge>
}

export function RecoveryPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const urlTxId = searchParams.get('tx_id')

  const [selectedTxId, setSelectedTxId] = useState<string | null>(urlTxId)
  const [focusedPath, setFocusedPath] = useState<string | null>(null)
  const [isConfirmOpen, setIsConfirmOpen] = useState(false)
  const [recovering, setRecovering] = useState(false)
  const [outcome, setOutcome] = useState<ChangeOutcome | null>(null)
  const [recoveryError, setRecoveryError] = useState<ErrorPresentation | null>(null)
  const [filter, setFilter] = useState<'all' | 'unfinished' | 'dirty' | 'committed'>('all')

  // 同步 URL 参数变更
  if (urlTxId && urlTxId !== selectedTxId) {
    setSelectedTxId(urlTxId)
  }

  const queryClient = useQueryClient()
  const {
    data: transactions = [],
    isLoading: loadingList,
    isRefetching: refetchingList,
    refetch: refetchList,
    error: listError,
  } = useTransactions()

  const {
    data: recoveryView = null,
    isLoading: loadingRecovery,
    error: recoveryViewError,
  } = useTransactionRecovery(selectedTxId)

  const handleSelectTx = (id: string) => {
    setSelectedTxId(id)
    setFocusedPath(null)
    setSearchParams({ tx_id: id })
    setOutcome(null)
    setRecoveryError(null)
  }

  const handleClearSelection = () => {
    setSelectedTxId(null)
    setFocusedPath(null)
    setSearchParams({})
    setOutcome(null)
    setRecoveryError(null)
  }

  const handleExecuteRecovery = async () => {
    if (!selectedTxId) return
    setIsConfirmOpen(false)
    setRecovering(true)
    setRecoveryError(null)
    setOutcome(null)

    try {
      const result = await api.recoverTransaction(selectedTxId, { approved: true })
      setOutcome(result)
      // 恢复成功后刷新事务列表和当前恢复视图
      void queryClient.invalidateQueries({ queryKey: [TRANSACTIONS_QUERY_KEY] })
      void queryClient.invalidateQueries({ queryKey: [TRANSACTION_RECOVERY_QUERY_KEY, selectedTxId] })
    } catch (err) {
      setRecoveryError(present(err))
    } finally {
      setRecovering(false)
    }
  }

  const unfinishedCount = transactions.filter((t) => UNFINISHED_STATUSES.has(t.status)).length
  const dirtyCount = transactions.filter((t) => t.status === 'index_dirty').length
  const committedCount = transactions.filter((t) => t.status === 'committed').length

  const filteredTransactions = transactions.filter((t) => {
    if (filter === 'unfinished') return UNFINISHED_STATUSES.has(t.status)
    if (filter === 'dirty') return t.status === 'index_dirty'
    if (filter === 'committed') return t.status === 'committed'
    return true
  })

  // 根据恢复视图中的 Diff 组装文件块与统计指标
  const diffChunks =
    recoveryView && recoveryView.diff.length > 0
      ? groupDiffByFile(
          recoveryView.diff,
          recoveryView.originals.map((o) => o.path),
        )
      : []

  const summaryItems: DiffSummaryItem[] = diffChunks.map((chunk) => ({
    path: chunk.path,
    operation: 'replace',
    destination: null,
    added_lines: chunk.lines.filter((l) => l.text.startsWith('+') && !l.text.startsWith('+++')).length,
    removed_lines: chunk.lines.filter((l) => l.text.startsWith('-') && !l.text.startsWith('---')).length,
  }))

  const activeChunk = diffChunks.find((c) => c.path === focusedPath) ?? diffChunks[0]
  const activeDiffLines = activeChunk ? activeChunk.lines : recoveryView?.diff ?? []

  return (
    <div className="space-y-6">
      {/* 顶部操作与标题 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">事务恢复与日志</h1>
          <p className="text-sm text-muted-foreground">
            检视未完成事务、审阅快照差异并安全回滚恢复 Vault 文件。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void refetchList()}
            disabled={loadingList || refetchingList}
            className="gap-1.5"
            id="recovery-refresh-btn"
          >
            <RefreshCw className={`h-4 w-4 ${refetchingList ? 'animate-spin' : ''}`} />
            刷新日志
          </Button>
        </div>
      </div>

      {/* 统计与警示看板 */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card className={unfinishedCount > 0 ? 'border-amber-400 bg-amber-50/50 dark:bg-amber-950/20' : ''}>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">待恢复事务</CardTitle>
            <ShieldAlert className={`h-4 w-4 ${unfinishedCount > 0 ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'}`} />
          </CardHeader>
          <CardContent>
            <div className={`text-2xl font-bold ${unfinishedCount > 0 ? 'text-amber-600 dark:text-amber-400' : ''}`}>
              {unfinishedCount}
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {unfinishedCount > 0 ? '存在写入中断事务，需人工审阅并恢复' : '当前 Vault 状态一致，无待恢复事务'}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">索引滞后事务</CardTitle>
            <Database className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{dirtyCount}</div>
            <p className="text-xs text-muted-foreground mt-1">
              文件已写入但索引未同步，可前往索引页增量更新
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">历史记录总数</CardTitle>
            <History className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{transactions.length}</div>
            <p className="text-xs text-muted-foreground mt-1">
              Vault 中保留的事务日记与快照总计
            </p>
          </CardContent>
        </Card>
      </div>

      {/* 错误提示 */}
      {listError && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>加载事务列表失败</AlertTitle>
          <AlertDescription>{present(listError).detail || present(listError).title}</AlertDescription>
        </Alert>
      )}

      {/* 两列布局：左侧事务列表，右侧选定事务恢复详情 */}
      <div className="grid gap-6 lg:grid-cols-12">
        {/* 左侧事务列表卡片 */}
        <div className="lg:col-span-5 space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex gap-1.5 p-1 bg-muted rounded-lg text-xs">
              <button
                type="button"
                onClick={() => setFilter('all')}
                className={`px-2.5 py-1 rounded-md transition-colors ${filter === 'all' ? 'bg-background shadow-xs font-semibold' : 'text-muted-foreground hover:text-foreground'}`}
              >
                全部 ({transactions.length})
              </button>
              <button
                type="button"
                onClick={() => setFilter('unfinished')}
                className={`px-2.5 py-1 rounded-md transition-colors ${filter === 'unfinished' ? 'bg-background shadow-xs font-semibold' : 'text-muted-foreground hover:text-foreground'}`}
              >
                待恢复 ({unfinishedCount})
              </button>
              <button
                type="button"
                onClick={() => setFilter('dirty')}
                className={`px-2.5 py-1 rounded-md transition-colors ${filter === 'dirty' ? 'bg-background shadow-xs font-semibold' : 'text-muted-foreground hover:text-foreground'}`}
              >
                索引滞后 ({dirtyCount})
              </button>
              <button
                type="button"
                onClick={() => setFilter('committed')}
                className={`px-2.5 py-1 rounded-md transition-colors ${filter === 'committed' ? 'bg-background shadow-xs font-semibold' : 'text-muted-foreground hover:text-foreground'}`}
              >
                已完成 ({committedCount})
              </button>
            </div>
          </div>

          {loadingList ? (
            <div className="flex items-center justify-center p-12 text-sm text-muted-foreground">
              <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
              正在读取事务日志...
            </div>
          ) : filteredTransactions.length === 0 ? (
            <Card className="p-8 text-center">
              <ShieldCheck className="mx-auto h-8 w-8 text-muted-foreground mb-2" />
              <p className="text-sm font-medium text-muted-foreground">没有符合条件的事务日志</p>
            </Card>
          ) : (
            <div className="space-y-3">
              {filteredTransactions.map((tx: JournalView) => {
                const isSelected = tx.transaction_id === selectedTxId
                const isUnfinished = UNFINISHED_STATUSES.has(tx.status)

                return (
                  <Card
                    key={tx.transaction_id}
                    onClick={() => handleSelectTx(tx.transaction_id)}
                    className={`cursor-pointer transition-all hover:border-primary/50 ${
                      isSelected
                        ? 'border-primary shadow-xs ring-1 ring-primary'
                        : isUnfinished
                        ? 'border-amber-300 dark:border-amber-800'
                        : ''
                    }`}
                  >
                    <CardHeader className="p-4 pb-2">
                      <div className="flex items-start justify-between gap-2">
                        <div className="font-mono text-xs font-bold text-foreground truncate">
                          {tx.transaction_id}
                        </div>
                        {getStatusBadge(tx.status)}
                      </div>
                    </CardHeader>
                    <CardContent className="p-4 pt-0 text-xs text-muted-foreground space-y-1">
                      <div className="flex items-center gap-1.5">
                        <FileText className="h-3.5 w-3.5" />
                        <span>涉及原始文件：{tx.originals.length} 个</span>
                      </div>
                      {tx.originals.slice(0, 2).map((orig) => (
                        <div key={orig.path} className="font-mono text-[11px] text-muted-foreground/80 pl-5 truncate">
                          • {orig.path}
                        </div>
                      ))}
                      {tx.originals.length > 2 && (
                        <div className="text-[11px] text-muted-foreground pl-5">
                          ...等共 {tx.originals.length} 个文件
                        </div>
                      )}
                    </CardContent>
                  </Card>
                )
              })}
            </div>
          )}
        </div>

        {/* 右侧选定事务详情与恢复工作区 */}
        <div className="lg:col-span-7 space-y-4">
          {!selectedTxId ? (
            <Card className="flex flex-col items-center justify-center p-12 text-center text-muted-foreground h-full min-h-[300px]">
              <LifeBuoy className="h-10 w-10 text-muted-foreground/60 mb-3" />
              <h3 className="text-base font-semibold text-foreground">请在左侧选择一个事务</h3>
              <p className="text-xs text-muted-foreground max-w-sm mt-1">
                选中事务后，系统将加载其恢复快照、回滚差异与日志详情，并允许在独立二次确认下安全恢复。
              </p>
            </Card>
          ) : loadingRecovery ? (
            <Card className="flex items-center justify-center p-12 text-sm text-muted-foreground">
              <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
              正在加载恢复快照与 Diff 差异...
            </Card>
          ) : recoveryViewError ? (
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>加载恢复详情失败</AlertTitle>
              <AlertDescription>{present(recoveryViewError).detail || present(recoveryViewError).title}</AlertDescription>
            </Alert>
          ) : !recoveryView ? (
            <Card className="p-6 text-center text-sm text-muted-foreground">
              未找到该事务的恢复数据
            </Card>
          ) : (
            <div className="space-y-4">
              {/* 恢复执行结果展示 */}
              {outcome && (
                <Alert className="border-emerald-500/50 bg-emerald-500/10 text-emerald-900 dark:text-emerald-200">
                  <CheckCircle2 className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                  <AlertTitle>事务恢复执行完毕</AlertTitle>
                  <AlertDescription className="text-xs space-y-1">
                    <p>事务 {outcome.transaction_id} 已成功回滚，Vault 原始文件已恢复至快照状态。</p>
                    {outcome.index_dirty && (
                      <p className="text-amber-600 dark:text-amber-400">
                        提示：索引状态已标记为待同步，请至索引页进行重建或增量更新。
                      </p>
                    )}
                  </AlertDescription>
                </Alert>
              )}

              {/* 恢复错误展示（例如 423 状态不匹配） */}
              {recoveryError && (
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" />
                  <AlertTitle>{recoveryError.title || '恢复操作失败'}</AlertTitle>
                  <AlertDescription className="text-xs space-y-2 mt-1">
                    <p>{recoveryError.detail || recoveryError.title}</p>
                    {recoveryError.hint && <p className="font-semibold">{recoveryError.hint}</p>}
                    <div className="p-2 bg-destructive/10 rounded font-mono text-[11px] break-all">
                      备份快照目录：{recoveryView.journal_directory}
                    </div>
                  </AlertDescription>
                </Alert>
              )}

              <Card>
                <CardHeader>
                  <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                    <div>
                      <CardTitle className="text-base font-bold flex items-center gap-2">
                        <span className="font-mono text-sm">{recoveryView.transaction_id}</span>
                        {getStatusBadge(recoveryView.status)}
                      </CardTitle>
                      <CardDescription className="text-xs mt-1 font-mono break-all">
                        日志目录：{recoveryView.journal_directory}
                      </CardDescription>
                    </div>
                    <Button variant="ghost" size="sm" onClick={handleClearSelection} className="self-start text-xs">
                      关闭检视
                    </Button>
                  </div>
                </CardHeader>

                <CardContent className="space-y-4">
                  {/* 快照原文件列表 */}
                  <div>
                    <h4 className="text-xs font-semibold text-foreground uppercase tracking-wider mb-2">
                      快照备份文件 ({recoveryView.originals.length})
                    </h4>
                    <div className="border rounded-md divide-y text-xs">
                      {recoveryView.originals.map((orig) => (
                        <div key={orig.path} className="p-2.5 flex items-center justify-between font-mono">
                          <span className="truncate">{orig.path}</span>
                          <span className="text-[11px] text-muted-foreground ml-2 shrink-0">
                            {orig.snapshot ? `快照: ${orig.snapshot}` : '初始为空文件'}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* 核心安全红线：恢复前展示快照差异 */}
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <h4 className="text-xs font-semibold text-foreground uppercase tracking-wider">
                        回滚快照差异 (Unified Diff)
                      </h4>
                      <span className="text-[11px] text-muted-foreground">
                        {recoveryView.recoverable ? '当前 Vault 内容 → 快照还原状态' : '不可回滚 / 无差异'}
                      </span>
                    </div>

                    {recoveryView.diff.length === 0 ? (
                      <div className="p-6 border border-dashed rounded-md text-center text-xs text-muted-foreground">
                        {recoveryView.recoverable
                          ? '当前文件与快照内容一致，暂无需要回滚的字节差异。'
                          : '该事务已完成提交或已清理，不提供回滚差异。'}
                      </div>
                    ) : (
                      <div className="space-y-3">
                        <DiffSummary
                          summary={summaryItems}
                          selectedPath={activeChunk?.path}
                          onSelectPath={setFocusedPath}
                        />
                        <div className="border rounded-md overflow-hidden max-h-[400px] overflow-y-auto">
                          <DiffViewer
                            diff={activeDiffLines}
                            path={activeChunk?.path}
                            operation="replace"
                          />
                        </div>
                      </div>
                    )}
                  </div>
                </CardContent>

                <CardFooter className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 pt-4 border-t bg-muted/20">
                  <div className="text-xs text-muted-foreground">
                    {recoveryView.recoverable ? (
                      <span className="text-amber-600 dark:text-amber-400 flex items-center gap-1">
                        <AlertTriangle className="h-3.5 w-3.5 inline" />
                        恢复将使用初始快照覆盖当前 Vault 对应文件
                      </span>
                    ) : (
                      <span>该事务状态不可恢复 Vault 磁盘文件</span>
                    )}
                  </div>

                  {recoveryView.recoverable && (
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => setIsConfirmOpen(true)}
                      disabled={recovering}
                      id="recovery-execute-btn"
                      className="gap-1.5"
                    >
                      <RotateCcw className={`h-4 w-4 ${recovering ? 'animate-spin' : ''}`} />
                      {recovering ? '正在恢复...' : '恢复文件至此快照'}
                    </Button>
                  )}
                </CardFooter>
              </Card>
            </div>
          )}
        </div>
      </div>

      {/* 防御性二次确认弹窗：默认焦点严格锁定在取消按钮 */}
      <AlertDialog open={isConfirmOpen} onOpenChange={setIsConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2 text-destructive">
              <AlertTriangle className="h-5 w-5" />
              确认回滚事务并恢复快照？
            </AlertDialogTitle>
            <AlertDialogDescription className="space-y-2 pt-2 text-sm">
              <p>
                您即将对事务 <code className="font-mono font-bold text-foreground">{selectedTxId}</code> 执行物理恢复。
              </p>
              <p>
                此操作将读取备份快照，直接覆盖还原 Vault 中对应的文件。如果文件在事务发生后被外部编辑器修改，系统将拒绝覆盖并保护现场。
              </p>
              <p className="font-medium text-foreground">
                请确认您已审阅上方显示的 Unified Diff 差异。
              </p>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            {/* 核心验收红线：默认焦点在取消按钮上 */}
            <AlertDialogCancel autoFocus id="recovery-cancel-btn">
              取消
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleExecuteRecovery}
              id="recovery-confirm-btn"
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              确认恢复
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
export default RecoveryPage
