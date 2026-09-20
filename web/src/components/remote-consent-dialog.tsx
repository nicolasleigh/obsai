"use client"

import { Send } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { api } from '@/lib/api'
import type { ConsentApproval, RemoteConsent } from '@/lib/api-types'
import { describeConsent, minutesUntil } from '@/lib/consent'
import { present, type ErrorPresentation } from '@/lib/errors'

/**
 * 远程同意对话框。
 *
 * 这是全项目唯一一处"点一下就让数据离开本机"的地方，所以它的形状围绕一件事设计：
 * **让用户看清将要发出去的是什么**。为此它显示查询原文、token 数与成本，而不是
 * 只显示一个"是否允许远程请求"的抽象问题——批准一个自己看不见的字符串，不叫同意。
 *
 * 三条刻意的取舍：
 *
 * 1. **不自动弹。** 打开与关闭由调用方决定（`open` / `onOpenChange`）。搜索页用
 *    `useDeferredValue`，每次打字停顿都会重新查询，自动弹会把页面变成弹窗地狱。
 * 2. **"取消"不发任何请求。** 页面上的结果已经是降级后的关键词结果，再问一次服务端
 *    只会换回同一批结果、多一个往返，还多一次把"拒绝"变成"批准"的机会。
 * 3. **批准失败留在对话框里显示**，不当作同意关掉。失败几乎只有一种成因——挑战过期
 *    （服务端 `ConsentExpiredError` → 410）——那说明用户手上的挑战已经作废，重发一次
 *    同样的批准还是过期，需要的是重新搜索。
 */
type RemoteConsentDialogProps = {
  /** 要决定的挑战。`null` 时内容区为空，但组件仍需挂载以保住退出动画。 */
  consent: RemoteConsent | null
  /** 挑战对应的查询原文，逐字显示。 */
  query: string
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 批准成功后回调，参数是服务端签发的批准。调用方据此重发搜索。 */
  onApproved: (approval: ConsentApproval) => void
  /** 用户拒绝。调用方据此把"已降级"说清楚。**不会**发任何请求。 */
  onRejected: () => void
}

function RemoteConsentDialog({
  consent,
  query,
  open,
  onOpenChange,
  onApproved,
  onRejected,
}: RemoteConsentDialogProps) {
  const [pending, setPending] = useState(false)
  const [failure, setFailure] = useState<ErrorPresentation | null>(null)

  const remaining = consent === null ? null : minutesUntil(consent.expires_at, new Date())

  const decide = async (approved: boolean) => {
    if (consent === null) return
    if (!approved) {
      onRejected()
      onOpenChange(false)
      return
    }
    setPending(true)
    setFailure(null)
    try {
      const approval = await api.consent(consent, true)
      onApproved(approval)
      onOpenChange(false)
    } catch (error) {
      setFailure(present(error))
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* `data-consent-dialog` 是给无头冒烟脚本用的钩子。刻意不改 `data-slot`
          ——那是 registry 生成物的属性，覆盖它等于和 shadcn 的样式约定作对。

          `showCloseButton={false}`：footer 里的「取消」才是这个对话框的出口，它带
          文字、说得清后果（不批准，不是"关掉"）；shadcn 默认那个 X 的标签是一句英文
          `sr-only` 的 "Close"，出现在一个全中文的界面里。Esc 与点击遮罩仍然可以关闭，
          而"关掉"在这个对话框里等于"不批准"——fail closed，不会误发查询。 */}
      <DialogContent data-consent-dialog="" showCloseButton={false}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Send className="size-4" />
            允许这次语义检索？
          </DialogTitle>
          <DialogDescription>
            语义检索需要把下面这段查询发送到远程嵌入服务。查询文本会离开本机。
          </DialogDescription>
        </DialogHeader>

        {consent !== null && (
          <dl className="flex flex-col gap-2 text-sm">
            {describeConsent(consent, query).map((line) => (
              <div key={line.label} className="flex gap-3">
                <dt className="text-muted-foreground w-28 shrink-0">{line.label}</dt>
                <dd className="font-mono break-all">{line.value}</dd>
              </div>
            ))}
          </dl>
        )}

        {remaining !== null && (
          <p className="text-muted-foreground text-xs">
            {remaining > 0
              ? `这次批准请求还有约 ${remaining} 分钟有效。`
              : '这次批准请求已经过期，请重新搜索。'}
          </p>
        )}

        {failure !== null && (
          <p className="text-destructive text-sm">
            {failure.title}
            {failure.hint !== undefined && ` ${failure.hint}`}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => void decide(false)} disabled={pending}>
            取消
          </Button>
          <Button onClick={() => void decide(true)} disabled={pending || consent === null}>
            {pending ? '正在批准…' : '批准并重新搜索'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export { RemoteConsentDialog }
