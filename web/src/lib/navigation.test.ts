import { Bot } from 'lucide-react'
import { describe, expect, it } from 'vitest'

import {
  APP_NAME,
  DEFAULT_PATH,
  EXTRA_ITEMS,
  NAV_GROUPS,
  NAV_ITEMS,
  SIDEBAR_GROUPS,
  badgeFor,
  breadcrumbFor,
  documentTitleFor,
  isActivePath,
  navItemFor,
} from './navigation'

/**
 * 这一组测试守的是 B-3 的验收：「各路由可切换，侧栏高亮正确」。
 *
 * 高亮不是一个 CSS 类的问题，而是"`/notes/<id>` 应该点亮谁"这种匹配规则的问题——
 * 它可以在没有 DOM 的情况下被完整验证，因此放在这里，而不是留给手工点击。
 */

const PATHS = NAV_ITEMS.map((item) => item.path)

describe('导航表', () => {
  it('路径唯一', () => {
    expect(new Set(PATHS).size).toBe(PATHS.length)
  })

  it('路径都是绝对路径', () => {
    for (const path of PATHS) expect(path.startsWith('/')).toBe(true)
  })

  it('默认路径在表内', () => {
    expect(NAV_ITEMS.some((item) => item.path === DEFAULT_PATH)).toBe(true)
  })

  it('计划 §5 B-3 要求的八个入口都在侧栏', () => {
    const labels = SIDEBAR_GROUPS.flatMap((group) => group.items.map((item) => item.label))
    expect(labels).toEqual(['概览', '搜索', '问答', '索引', '整理', '写入', 'Agent', '恢复'])
  })

  it('每个条目都写全了说明与验收标准', () => {
    for (const item of NAV_ITEMS) {
      expect(item.summary.length, item.path).toBeGreaterThan(0)
      expect(item.criteria.length, item.path).toBeGreaterThan(0)
      expect(item.step, item.path).toMatch(/^[A-F]-\d+$/)
    }
  })

  it('带参数的路由只出现在无侧栏入口的条目上', () => {
    for (const item of NAV_ITEMS) {
      if (item.routePath !== undefined) {
        expect(item.routePath).toContain(':')
        expect(SIDEBAR_GROUPS.flatMap((group) => group.items)).not.toContain(item)
      }
    }
  })

  it('无侧栏入口的条目不出现在任何分组里', () => {
    const grouped = NAV_GROUPS.flatMap((group) => group.items)
    for (const item of EXTRA_ITEMS) expect(grouped).not.toContain(item)
  })

  it('侧栏条目是全部条目的真子集', () => {
    expect(EXTRA_ITEMS.length).toBeGreaterThan(0)
    expect(NAV_ITEMS.length).toBe(
      SIDEBAR_GROUPS.flatMap((group) => group.items).length + EXTRA_ITEMS.length,
    )
  })
})

describe('navItemFor', () => {
  it('精确命中', () => {
    expect(navItemFor('/')?.label).toBe('概览')
    expect(navItemFor('/search')?.label).toBe('搜索')
    expect(navItemFor('/recovery')?.label).toBe('恢复')
  })

  it('尾部斜杠等价', () => {
    expect(navItemFor('/search/')?.path).toBe('/search')
    expect(navItemFor('/')?.path).toBe('/')
    expect(navItemFor('')?.path).toBe('/')
  })

  it('子路径落到父条目上', () => {
    expect(navItemFor('/notes/abc-123')?.path).toBe('/notes')
    expect(navItemFor('/notes/abc-123')?.label).toBe('笔记')
  })

  it('根路径不做前缀匹配，不会抢走其它页面', () => {
    expect(navItemFor('/index')?.label).toBe('索引')
    expect(navItemFor('/ask')?.label).toBe('问答')
  })

  it('未登记的路径没有对应条目', () => {
    expect(navItemFor('/nope')).toBeUndefined()
    expect(navItemFor('/nope/deep')).toBeUndefined()
  })
})

describe('侧栏高亮', () => {
  const sidebarItems = SIDEBAR_GROUPS.flatMap((group) => group.items)

  it('每个页面只点亮一个条目', () => {
    for (const item of sidebarItems) {
      const lit = sidebarItems.filter((candidate) => isActivePath(item.path, candidate))
      expect(lit.map((entry) => entry.path), item.path).toEqual([item.path])
    }
  })

  it('笔记详情不点亮任何侧栏条目', () => {
    expect(sidebarItems.filter((item) => isActivePath('/notes/abc', item))).toEqual([])
  })

  it('未登记的路径不点亮任何侧栏条目', () => {
    expect(sidebarItems.filter((item) => isActivePath('/nope', item))).toEqual([])
  })
})

describe('面包屑', () => {
  it('根路径是「应用名 / 概览」，且都不可点', () => {
    expect(breadcrumbFor('/')).toEqual([{ label: APP_NAME, to: undefined }, { label: '概览' }])
  })

  it('二级页面把应用名做成回首页的链接', () => {
    expect(breadcrumbFor('/search')).toEqual([
      { label: APP_NAME, to: DEFAULT_PATH },
      { label: '搜索' },
    ])
  })

  it('动态段追加为最后一项', () => {
    expect(breadcrumbFor('/notes/abc-123')).toEqual([
      { label: APP_NAME, to: DEFAULT_PATH },
      { label: '笔记' },
      { label: 'abc-123' },
    ])
  })

  it('未登记的路径按层级原样展开，而不是断言页面不存在', () => {
    expect(breadcrumbFor('/nope/deep')).toEqual([
      { label: APP_NAME, to: DEFAULT_PATH },
      { label: 'nope' },
      { label: 'deep' },
    ])
  })
})

describe('页面标题', () => {
  it('用页面名', () => {
    expect(documentTitleFor('/search')).toBe(`搜索 · ${APP_NAME}`)
  })

  it('动态段优先', () => {
    expect(documentTitleFor('/notes/abc')).toBe(`abc · ${APP_NAME}`)
  })

  it('未登记的路径有确定文案', () => {
    expect(documentTitleFor('/nope')).toBe(`未知页面 · ${APP_NAME}`)
  })
})

describe('侧栏徽标', () => {
  it('未实现的条目显示计划步骤', () => {
    const plannedItem = {
      path: '/future',
      label: '未来规划',
      icon: Bot,
      step: 'F-1' as const,
      status: 'planned' as const,
      summary: '未来功能概要',
      criteria: '未来功能验收标准',
    }
    expect(badgeFor(plannedItem)).toBe('F-1')
  })

  it('已实现的页面不显示徽标', () => {
    const overview = navItemFor('/')
    expect(overview).toBeDefined()
    expect(badgeFor(overview!)).toBeUndefined()

    const search = navItemFor('/search')
    expect(search).toBeDefined()
    expect(badgeFor(search!)).toBeUndefined()

    const ask = navItemFor('/ask')
    expect(ask).toBeDefined()
    expect(badgeFor(ask!)).toBeUndefined()

    // B-7 交付后 `/notes` 从 planned 变 ready。条目徽标消失意味着
    // 路由表里也注册了它，两者由 `router.tsx` 的 `checkPagesAreInSync` 双向约束。
    const note = navItemFor('/notes')
    expect(note).toBeDefined()
    expect(badgeFor(note!)).toBeUndefined()

    // C-3 交付后 `/index` 从 planned 变 ready。
    const index = navItemFor('/index')
    expect(index).toBeDefined()
    expect(badgeFor(index!)).toBeUndefined()

    // D-3 交付后 `/changes` 从 planned 变 ready。
    const changes = navItemFor('/changes')
    expect(changes).toBeDefined()
    expect(badgeFor(changes!)).toBeUndefined()

    // D-4 交付后 `/organize` 从 planned 变 ready。
    const organize = navItemFor('/organize')
    expect(organize).toBeDefined()
    expect(badgeFor(organize!)).toBeUndefined()

    // D-5 交付后 `/recovery` 从 planned 变 ready。
    const recovery = navItemFor('/recovery')
    expect(recovery).toBeDefined()
    expect(badgeFor(recovery!)).toBeUndefined()

    // E-2 交付后 `/agent` 从 planned 变 ready。
    const agent = navItemFor('/agent')
    expect(agent).toBeDefined()
    expect(badgeFor(agent!)).toBeUndefined()
  })
})
