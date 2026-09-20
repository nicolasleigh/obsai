import { RefreshCw } from 'lucide-react'
import { toast } from 'sonner'

import { StatusDot } from '@/components/status-dot'
import { SidebarMenu, SidebarMenuButton, SidebarMenuItem } from '@/components/ui/sidebar'
import { useStatus } from '@/hooks/use-status'
import { present } from '@/lib/errors'
import { summarizeBackend } from '@/lib/status'
import { cn } from '@/lib/utils'

/**
 * 侧栏底部的后端状态灯。
 *
 * 它解决一个具体问题：只读 UI 的失败大多**不在当前页面上**——索引没建、Vault 路径
 * 写错、另一个进程占着写锁。用户在搜索页看到空结果时，需要一条通往原因的路径，
 * 而不是去猜。这里把它压缩成一行常驻文案，点一下即可重取。
 *
 * 文案全部来自 `lib/status.ts`，与概览页共用同一份判断，避免两处说法不一致。
 */
function ConnectionStatus() {
  const status = useStatus()
  const summary = summarizeBackend({ data: status.data, error: status.error })
  const secondary = summary.hint === '' ? '点击重新读取' : summary.hint

  const refresh = async () => {
    const result = await status.refetch()
    if (result.isSuccess) {
      toast.success('已重新读取后端状态')
      return
    }
    const failure = present(result.error)
    toast.error(failure.title, { description: failure.hint })
  }

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <SidebarMenuButton
          size="lg"
          tooltip={`${summary.label}${summary.hint === '' ? '' : ` —— ${summary.hint}`}`}
          onClick={() => void refresh()}
        >
          <StatusDot tone={summary.tone} />
          <div className="grid flex-1 text-left text-sm leading-tight">
            <span className="truncate font-medium">{summary.label}</span>
            <span className="text-muted-foreground truncate text-xs">{secondary}</span>
          </div>
          <RefreshCw className={cn('size-4 shrink-0', status.isFetching && 'animate-spin')} />
        </SidebarMenuButton>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}

export { ConnectionStatus }
