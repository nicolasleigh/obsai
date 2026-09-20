import { Link, useLocation } from 'react-router'

import { ConnectionStatus } from '@/components/connection-status'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from '@/components/ui/sidebar'
import { badgeFor, isActivePath, NAV_GROUPS } from '@/lib/navigation'

/**
 * 主侧栏。
 *
 * 它不定义任何导航项——那在 `lib/navigation.ts` 里，路由表也从同一张表生成。
 * 这里只负责渲染与高亮，因此"侧栏少了一项"这类问题不可能悄悄发生。
 */

function AppSidebar() {
  const { pathname } = useLocation()

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton asChild size="lg" tooltip="回到概览">
              <Link to="/">
                <div className="bg-sidebar-primary text-sidebar-primary-foreground flex aspect-square size-8 shrink-0 items-center justify-center rounded-lg text-xs font-semibold">
                  OA
                </div>
                <div className="grid flex-1 text-left text-sm leading-tight">
                  <span className="truncate font-medium">ObsAgent</span>
                  <span className="text-muted-foreground truncate text-xs">
                    本地 Obsidian 智能体
                  </span>
                </div>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        {/* 一个导航地标：屏幕阅读器可以按地标跳到主导航，测试也能精确定位到它，
            而不必靠"页面上第几个 ul"这种一改就坏的假设。 */}
        <nav aria-label="主导航" className="flex w-full flex-col gap-2">
          {NAV_GROUPS.map((group) => (
            <SidebarGroup key={group.label}>
              <SidebarGroupLabel>{group.label}</SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {group.items.map((item) => {
                    const active = isActivePath(pathname, item)
                    const badge = badgeFor(item)
                    const Icon = item.icon
                    return (
                      <SidebarMenuItem key={item.path}>
                        <SidebarMenuButton asChild isActive={active} tooltip={item.label}>
                          <Link to={item.path} aria-current={active ? 'page' : undefined}>
                            <Icon />
                            <span>{item.label}</span>
                          </Link>
                        </SidebarMenuButton>
                        {/* 未实现的页面挂上计划步骤，让"还有哪些没做"一眼可见；
                            实现后 `status` 翻成 ready，徽标自动消失。 */}
                        {badge !== undefined && (
                          <SidebarMenuBadge className="text-muted-foreground text-[10px]">
                            {badge}
                          </SidebarMenuBadge>
                        )}
                      </SidebarMenuItem>
                    )
                  })}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          ))}
        </nav>
      </SidebarContent>

      <SidebarFooter>
        <ConnectionStatus />
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}

export { AppSidebar }
