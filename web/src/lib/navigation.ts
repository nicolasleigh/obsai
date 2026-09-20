import {
  Bot,
  Database,
  FilePenLine,
  FileText,
  Inbox,
  LayoutDashboard,
  LifeBuoy,
  MessageSquareText,
  Search,
  Sparkles,
  type LucideIcon,
} from 'lucide-react'

/**
 * 导航表的单一真相源。
 *
 * 侧栏、路由表、面包屑、`document.title` 全部从这一张表派生。分开写的话，
 * 「加了路由忘了加侧栏项」这类漂移只能靠人眼发现；而在这里，路由表是
 * `NAV_ITEMS.map(routeFor)` 生成的，缺一个条目在**结构上**就不可能。
 *
 * 表里同时记录每个页面由计划文档的哪一步实现、验收标准是什么。这不是装饰：
 * 未实现的页面需要一句诚实的说明，而这句话最好是原文抄来的，不是转述。
 */

/** 页面当前是否已经可用。`'planned'` 的条目渲染占位页。 */
export type NavStatus = 'ready' | 'planned'

export type NavItem = {
  /** 绝对路径，也是路由路径（`/` 表示索引路由）。同时用于前缀匹配。 */
  path: string
  /**
   * 路由匹配串，可含参数；缺省时由 `path` 去掉前导斜杠得到。
   * 只有带动态段的页面（如笔记详情）需要显式指定。
   */
  routePath?: string
  /** 侧栏与面包屑显示的中文名。 */
  label: string
  icon: LucideIcon
  /** 计划文档里负责**完成**该页面的步骤编号，如 `B-5`。 */
  step: string
  /**
   * 当前是否已可交付使用。
   *
   * 与 `step` 是两件事：概览页在 B-3 就有了可用版本（`ready`），但计划里
   * 由 B-4 把它做成最终的卡片网格，所以 `step` 仍是 `B-4`。
   */
  status: NavStatus
  /** 这个页面做什么，一句话。 */
  summary: string
  /** 计划文档里的验收标准原文。 */
  criteria: string
}

export type NavGroup = {
  label: string
  items: readonly NavItem[]
}

/** 计划文档 §5 B-3 点名的八个入口，按用途分六组。 */
export const NAV_GROUPS: readonly NavGroup[] = [
  {
    label: '总览',
    items: [
      {
        path: '/',
        label: '概览',
        icon: LayoutDashboard,
        step: 'B-4',
        status: 'ready',
        summary: 'Vault、索引、事务与写入锁的当前状态，一屏看完。',
        criteria: '无 Vault 时给出明确引导（对应 CLI 的 "No vault configured"）。',
      },
    ],
  },
  {
    label: '检索',
    items: [
      {
        path: '/search',
        label: '搜索',
        icon: Search,
        step: 'B-5',
        status: 'ready',
        summary: '关键词 / 语义 / 图谱 / 混合四种模式检索笔记。',
        criteria: '--mode keyword 无需 API key 可用；降级 warning 可见。',
      },
      {
        path: '/ask',
        label: '问答',
        icon: MessageSquareText,
        step: 'B-6',
        status: 'ready',
        summary: '带引用的问答，回答里的 [S1][S2] 可跳回原文。',
        criteria: '弃答、引用校验失败均有明确提示；未配置 Key 时给出引导而非报错堆栈。',
      },
    ],
  },
  {
    label: '维护',
    items: [
      {
        path: '/index',
        label: '索引',
        icon: Database,
        step: 'C-3',
        status: 'ready',
        summary: '查看索引与向量代状态，发起增量或全量重建。',
        criteria: '索引换代后旧 note ID 不得被悄悄沿用（见 §11 D4）。',
      },
      {
        path: '/organize',
        label: '整理',
        icon: Inbox,
        step: 'D-4',
        summary: 'Inbox 归类建议的预览与逐条批准。',
        status: 'ready',
        criteria: '拒绝时 Vault 字节不变。',
      },
    ],
  },
  {
    label: '写入',
    items: [
      {
        path: '/changes',
        label: '写入',
        icon: FilePenLine,
        step: 'D-3',
        status: 'ready',
        summary: '待审批的写入计划：diff 预览 + nonce 批准。',
        criteria: '所有写入都有服务端生成的预览和用户批准。',
      },
    ],
  },
  {
    label: '智能体',
    items: [
      {
        path: '/agent',
        label: 'Agent',
        icon: Bot,
        step: 'E-2',
        status: 'ready',
        summary: 'Agent 会话、工具调用时间线与人工审批闭环。',
        criteria: 'Agent 仍有步数与工具权限上限（见 §13）。',
      },
    ],
  },
  {
    label: '安全',
    items: [
      {
        path: '/recovery',
        label: '恢复',
        icon: LifeBuoy,
        step: 'D-5',
        status: 'ready',
        summary: '未完成事务的快照差异与恢复入口。',
        criteria: '恢复前必须展示快照差异。',
      },
    ],
  },
]

/**
 * 不出现在侧栏、但仍需要路由、面包屑与页面标题的页面。
 *
 * 典型是从搜索结果跳进来的笔记详情：它没有独立的侧栏入口（用户不会"去笔记页"，
 * 而是从某条结果进入），但 `document.title` 和面包屑必须认得它。
 */
export const EXTRA_ITEMS: readonly NavItem[] = [
  {
    path: '/notes',
    routePath: 'notes/:noteId',
    label: '笔记',
    icon: FileText,
    step: 'B-7',
    status: 'ready',
    summary: '渲染单篇笔记的解析结果，WikiLink 只跳转 Vault 内目标。',
    criteria:
      '含 <script>、<iframe> 的笔记不执行任何内容；WikiLink 只跳转服务端校验过的 Vault 内目标。',
  },
  {
    path: '/embedding',
    label: '向量生成',
    icon: Sparkles,
    step: 'C-4',
    status: 'ready',
    summary: '预估分块、Token 与调用成本，核验预算并批准执行向量生成。',
    criteria: '预算超限返回 429；计划漂移触发二次确认。',
  },
]

/** 侧栏分组。`EXTRA_ITEMS` 刻意不在其中。 */
export const SIDEBAR_GROUPS: readonly NavGroup[] = NAV_GROUPS

/** 全部页面（含无侧栏入口的），路由表与匹配逻辑都以此为准。 */
export const NAV_ITEMS: readonly NavItem[] = [
  ...NAV_GROUPS.flatMap((group) => group.items),
  ...EXTRA_ITEMS,
]

/** 索引路由的路径，也是"回首页"的目标。 */
export const DEFAULT_PATH = '/'

/** 应用名，用在面包屑首项与页面标题后缀上。 */
export const APP_NAME = 'ObsAgent'

/** 去掉尾部斜杠；空串归一为根路径。`/search/` 与 `/search` 是同一个页面。 */
function normalize(pathname: string): string {
  const trimmed = pathname.replace(/\/+$/, '')
  return trimmed === '' ? DEFAULT_PATH : trimmed
}

/** 该条目在 `pathname` 上是否命中（精确或前缀）。 */
function matches(item: NavItem, path: string): boolean {
  if (item.path === path) return true
  // 根路径不做前缀匹配，否则 `/index`、`/ask` 全会被它抢走。
  if (item.path === DEFAULT_PATH) return false
  return path.startsWith(`${item.path}/`)
}

/**
 * `pathname` 对应的页面。
 *
 * 精确匹配优先，其次取**最长**前缀——这样将来加了 `/notes/archive` 这种更具体的
 * 条目，它不会被 `/notes` 抢走。
 */
export function navItemFor(pathname: string): NavItem | undefined {
  const path = normalize(pathname)
  const exact = NAV_ITEMS.find((item) => item.path === path)
  if (exact !== undefined) return exact
  return NAV_ITEMS.filter((item) => matches(item, path)).sort(
    (left, right) => right.path.length - left.path.length,
  )[0]
}

/** 侧栏高亮规则：只有当前页面本身对应的那个条目亮。 */
export function isActivePath(pathname: string, item: NavItem): boolean {
  return navItemFor(pathname)?.path === item.path
}

/** `pathname` 相对该条目的剩余部分。`/notes/abc` 对 `/notes` 得 `abc`；根路径得空串。 */
function restOf(item: NavItem, pathname: string): string {
  const base = item.path === DEFAULT_PATH ? '' : item.path
  return normalize(pathname).slice(base.length).replace(/^\/+/, '')
}

export type Crumb = {
  label: string
  /** 有值则可点击；最后一项与根路径下的首项没有目标。 */
  to?: string
}

/**
 * 面包屑。
 *
 * 未登记在导航表里的路径不会显示"页面不存在"——路由可能是存在的（例如将来的
 * 一次性页面），面包屑不该替路由下结论。此时按路径层级原样展开。
 */
export function breadcrumbFor(pathname: string): readonly Crumb[] {
  const item = navItemFor(pathname)
  const atRoot = normalize(pathname) === DEFAULT_PATH
  const trail: Crumb[] = [{ label: APP_NAME, to: atRoot ? undefined : DEFAULT_PATH }]

  if (item !== undefined) {
    trail.push({ label: item.label })
    const rest = restOf(item, pathname)
    if (rest !== '') trail.push({ label: rest })
    return trail
  }

  for (const segment of normalize(pathname).split('/').filter((part) => part !== '')) {
    trail.push({ label: segment })
  }
  return trail
}

/** 浏览器标签页标题。叶子节点优先，其次是页面名。 */
export function documentTitleFor(pathname: string): string {
  const item = navItemFor(pathname)
  if (item === undefined) return `未知页面 · ${APP_NAME}`
  const rest = restOf(item, pathname)
  return `${rest === '' ? item.label : rest} · ${APP_NAME}`
}

/** 侧栏徽标：只在页面尚未实现时显示计划步骤。 */
export function badgeFor(item: NavItem): string | undefined {
  return item.status === 'planned' ? item.step : undefined
}
