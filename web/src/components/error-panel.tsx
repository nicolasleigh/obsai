import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import type { ErrorPresentation } from '@/lib/errors'

/**
 * 错误面板：中文标题 + 下一步动作 + 服务端原文 + request id。
 *
 * 抽出来是因为它有两个消费者——概览页的查询失败、以及路由级错误页——而它们必须
 * 长得一样。用户看到的第一条错误信息不该取决于它是在哪一层被接住的。
 *
 * `detail`（服务端原文）刻意保留而不折叠：本地工具的使用者就是它的作者，
 * 那句英文比任何中文转述都更有诊断价值。
 */
function ErrorPanel({
  failure,
  onRetry,
  retryLabel = '重试',
}: {
  failure: ErrorPresentation
  onRetry?: () => void
  retryLabel?: string
}) {
  return (
    <Card className="border-destructive/40">
      <CardHeader>
        <CardTitle className="text-destructive text-sm">{failure.title}</CardTitle>
        {failure.hint !== undefined && <CardDescription>{failure.hint}</CardDescription>}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {failure.detail !== undefined && (
          <pre className="bg-muted overflow-x-auto rounded-md p-3 text-xs">
            {failure.detail}
            {failure.requestId !== undefined && `\nrequest id: ${failure.requestId}`}
          </pre>
        )}
        {onRetry !== undefined && (
          <div>
            <Button size="sm" variant="outline" onClick={onRetry}>
              {retryLabel}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export { ErrorPanel }
