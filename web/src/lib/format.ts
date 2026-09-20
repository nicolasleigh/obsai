/**
 * 时间戳与时长 → 界面上要显示的那几个字。
 *
 * 单独一个模块，是因为它有两个以上的消费者：概览页的脏笔记时间、索引页的任务时间，
 * 以及 D 阶段的事务与写入页。两处各写一份 `formatTimestamp` 的后果是"同一个时刻在
 * 两个页面上差一分钟"这种没人会主动去查的不一致。
 *
 * 不引 `date-fns` / `dayjs`：只需要一个函数，而本地工具的用户就是它的作者，
 * 他们要看的是**本地时间**而不是相对时间（"3 分钟前"在一个刚跑完重建的页面上，
 * 比一个确切时刻更难判断）。
 */

/**
 * ISO 时间戳 → 本地时间，精确到分钟。
 *
 * 解析失败时原样返回：服务端给的是它自己的时间字符串，看不懂也好过显示
 * `Invalid Date`——后者会让用户以为页面坏了，而前者至少还能看出原始值。
 */
export function formatTimestamp(value: string): string {
  const at = new Date(value)
  if (Number.isNaN(at.getTime())) return value
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`
}
