import { useState } from 'react'
import { Link } from 'react-router'
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Coins,
  Cpu,
  Database,
  Hash,
  Layers,
  Loader2,
  RefreshCw,
  Send,
  Sparkles,
} from 'lucide-react'

import { ErrorPanel } from '@/components/error-panel'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'
import { useApproveEmbeddingPlan, useEmbeddingPlan } from '@/hooks/use-embedding'
import { useCancelJob, useJobs } from '@/hooks/use-jobs'
import { ApiError, present } from '@/lib/errors'
import { formatCost } from '@/lib/consent'
import {
  jobCanBeCancelled,
  jobIsTerminal,
  jobProgress,
  jobStatusLabel,
  jobStepLabel,
} from '@/lib/jobs'
import type { EmbeddingPlanView, JobView } from '@/lib/api-types'

function MetricCard({
  title,
  value,
  description,
  icon: Icon,
}: {
  title: string
  value: string | number
  description?: string
  icon: React.ComponentType<{ className?: string }>
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
        <Icon className="text-muted-foreground size-4" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold tracking-tight">{value}</div>
        {description && <p className="text-muted-foreground mt-1 text-xs">{description}</p>}
      </CardContent>
    </Card>
  )
}

export function EmbeddingPage() {
  const { data: plan, isLoading, error, refetch, isFetching } = useEmbeddingPlan()
  const approveMutation = useApproveEmbeddingPlan()
  const feed = useJobs()
  const cancelJob = useCancelJob()
  const [driftNotice, setDriftNotice] = useState<string | null>(null)
  const [submittedJob, setSubmittedJob] = useState<JobView | null>(null)

  const liveJob = submittedJob
    ? feed.jobs.find((j) => j.job_id === submittedJob.job_id) ?? submittedJob
    : null

  const handleApprove = (targetPlan: EmbeddingPlanView) => {
    setDriftNotice(null)
    approveMutation.mutate(
      { planId: targetPlan.plan_id, nonce: targetPlan.nonce },
      {
        onSuccess: (job) => {
          setSubmittedJob(job)
        },
        onError: (err) => {
          if (err instanceof ApiError && err.code === 'plan_drift') {
            setDriftNotice('检测到索引或分块数据在审批期间已发生变更，原预估已失效。系统已重新计算最新预估，请核对后再次批准。')
            refetch()
          }
        },
      },
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="icon" asChild className="size-8">
              <Link to="/index" title="返回索引页">
                <ArrowLeft className="size-4" />
              </Link>
            </Button>
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <Sparkles className="text-primary size-5" />
              向量生成
            </h1>
          </div>
          <p className="text-muted-foreground text-sm">
            预估分块、Token 与调用成本，核对预算后批准发起向量嵌入任务。
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setDriftNotice(null)
              refetch()
            }}
            disabled={isFetching || approveMutation.isPending}
          >
            <RefreshCw className={`size-3.5 ${isFetching ? 'animate-spin' : ''}`} />
            刷新预估
          </Button>
        </div>
      </header>

      {driftNotice && (
        <Alert className="border-amber-500/60 bg-amber-500/10 text-amber-900 dark:text-amber-200">
          <AlertTriangle className="size-4 text-amber-600 dark:text-amber-400" />
          <AlertTitle>预估计划已漂移并刷新</AlertTitle>
          <AlertDescription>{driftNotice}</AlertDescription>
        </Alert>
      )}

      {liveJob && (
        <Card className={liveJob.status === 'cancelled' ? 'border-amber-500/50' : liveJob.status === 'failed' ? 'border-destructive/50' : liveJob.status === 'succeeded' ? 'border-green-500/50' : 'border-primary/50'}>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="flex items-center gap-2 text-base font-medium">
                {liveJob.status === 'cancelled' ? (
                  <AlertTriangle className="size-5 text-amber-500" />
                ) : liveJob.status === 'failed' ? (
                  <AlertTriangle className="size-5 text-destructive" />
                ) : liveJob.status === 'succeeded' ? (
                  <CheckCircle2 className="size-5 text-green-500" />
                ) : (
                  <Loader2 className="size-5 animate-spin text-primary" />
                )}
                {jobStatusLabel(liveJob)}：向量生成任务
              </CardTitle>
              <span className="font-mono text-xs text-muted-foreground">{liveJob.job_id}</span>
            </div>
            <CardDescription>
              {jobStepLabel(liveJob) ?? liveJob.message ?? '正在处理中...'}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {!jobIsTerminal(liveJob) && (() => {
              const progress = jobProgress(liveJob)
              const pct =
                progress.kind === 'fraction' && progress.max > 0
                  ? Math.round((progress.value / progress.max) * 100)
                  : null
              return (
                <div className="flex flex-col gap-1.5">
                  <div className="flex justify-between text-xs text-muted-foreground">
                    <span>
                      {progress.kind === 'fraction'
                        ? `正在处理第 ${progress.value} / ${progress.max} 批次`
                        : '正在执行...'}
                    </span>
                    {pct !== null && <span>{pct}%</span>}
                  </div>
                  <Progress value={pct ?? 0} />
                </div>
              )
            })()}

            {liveJob.status === 'cancelled' && (
              <p className="text-sm text-muted-foreground">
                任务已协作式取消。已自动拦截后续批次，未向索引库写入任何半途向量。
              </p>
            )}

            {liveJob.status === 'failed' && (
              <p className="text-sm text-destructive">
                {liveJob.error || '任务执行失败，请检查日志或网络连接。'}
              </p>
            )}

            {liveJob.status === 'succeeded' && (
              <p className="text-sm text-green-600 dark:text-green-400">
                向量生成已成功完成，分块向量已持久化至本地索引库。
              </p>
            )}
          </CardContent>
          <CardFooter className="flex flex-wrap items-center justify-between gap-3 border-t pt-3">
            <div className="flex items-center gap-2">
              <Button size="sm" variant="outline" asChild>
                <Link to="/index">前往索引页</Link>
              </Button>
              {jobIsTerminal(liveJob) && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setSubmittedJob(null)
                    refetch()
                  }}
                >
                  重新核验计划
                </Button>
              )}
            </div>
            {!jobIsTerminal(liveJob) && (
              <Button
                size="sm"
                variant="destructive"
                onClick={() => cancelJob.mutate(liveJob.job_id)}
                disabled={cancelJob.isPending || !jobCanBeCancelled(liveJob)}
              >
                {cancelJob.isPending ? '正在取消...' : '取消任务'}
              </Button>
            )}
          </CardFooter>
        </Card>
      )}

      {isLoading && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2, 3, 4, 5].map((key) => (
            <Skeleton key={key} className="h-28 w-full rounded-xl" />
          ))}
        </div>
      )}

      {error && (
        <ErrorPanel
          failure={present(error)}
          onRetry={() => {
            setDriftNotice(null)
            refetch()
          }}
        />
      )}

      {approveMutation.error && !(approveMutation.error instanceof ApiError && approveMutation.error.status === 409) && (
        <ErrorPanel
          failure={present(approveMutation.error)}
          onRetry={() => {
            setDriftNotice(null)
            refetch()
          }}
          retryLabel="重新获取计划"
        />
      )}

      {plan && !submittedJob && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <MetricCard
              title="待生成向量分块"
              value={plan.chunks_requiring_embeddings.toLocaleString()}
              description={`已排除 ${plan.cache_hits} 个缓存命中分块`}
              icon={Layers}
            />
            <MetricCard
              title="预估消耗 Tokens"
              value={plan.estimated_tokens.toLocaleString()}
              description="按字符/字节保守上限估算"
              icon={Hash}
            />
            <MetricCard
              title="预估调用成本"
              value={formatCost(plan.estimated_cost_usd)}
              description="根据提供商定价规则核算"
              icon={Coins}
            />
            <MetricCard
              title="预计网络请求批次"
              value={plan.request_count}
              description="根据配置的批量大小分批请求"
              icon={Send}
            />
            <MetricCard
              title="缓存复用分块"
              value={plan.cache_hits.toLocaleString()}
              description="无需重复发送远程请求"
              icon={Database}
            />
            <MetricCard
              title="模型代标识"
              value={plan.generation_id.slice(0, 12)}
              description="生成隔离哈希前缀"
              icon={Cpu}
            />
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">审批与执行确认</CardTitle>
              <CardDescription>
                批准操作将使用上述预估参数向远程嵌入提供商发起批量请求，并将生成的向量持久化至本地索引库。
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-3 text-sm">
              <div className="flex flex-col gap-1 rounded-lg border p-3">
                <span className="text-muted-foreground text-xs">计划有效期至</span>
                <span className="font-mono text-xs">{plan.expires_at}</span>
              </div>
              {plan.chunks_requiring_embeddings === 0 ? (
                <p className="text-muted-foreground">
                  当前索引库所有分块已具有有效向量，无需执行额外的向量生成。
                </p>
              ) : (
                <p className="text-muted-foreground">
                  请确认上述 Tokens 消耗与预估费用在您的预算范围内。批准后若笔记数据发生更改，系统会自动拦截并要求重新确认。
                </p>
              )}
            </CardContent>
            <CardFooter className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
              <Button variant="outline" asChild>
                <Link to="/index">返回索引页</Link>
              </Button>
              <Button
                onClick={() => handleApprove(plan)}
                disabled={approveMutation.isPending || isFetching || plan.chunks_requiring_embeddings === 0}
              >
                {approveMutation.isPending ? (
                  <>
                    <Loader2 className="mr-2 size-4 animate-spin" />
                    正在提交并校验...
                  </>
                ) : (
                  <>
                    <Sparkles className="mr-2 size-4" />
                    批准并启动向量生成
                  </>
                )}
              </Button>
            </CardFooter>
          </Card>
        </>
      )}
    </div>
  )
}

export default EmbeddingPage
