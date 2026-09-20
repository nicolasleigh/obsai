import { Fragment } from 'react'
import { Link, useLocation } from 'react-router'

import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb'
import { breadcrumbFor } from '@/lib/navigation'

/**
 * 页头面包屑。
 *
 * 层级来自 `lib/navigation.ts`，不来自路由本身——路由只知道路径串，不知道
 * "`/notes/abc` 属于笔记页"这种语义。这样加一个动态段不需要动这里。
 */
function AppBreadcrumb() {
  const { pathname } = useLocation()
  const trail = breadcrumbFor(pathname)

  return (
    <Breadcrumb>
      <BreadcrumbList>
        {trail.map((crumb, index) => {
          const last = index === trail.length - 1
          return (
            <Fragment key={`${crumb.label}-${index}`}>
              {index > 0 && <BreadcrumbSeparator className="hidden md:block" />}
              <BreadcrumbItem className={last ? undefined : 'hidden md:block'}>
                {/* 只有最后一项是"当前页"；中间的层级要么是链接，要么是纯文本，
                    不会出现两个 aria-current="page"。 */}
                {last ? (
                  <BreadcrumbPage>{crumb.label}</BreadcrumbPage>
                ) : crumb.to !== undefined ? (
                  <BreadcrumbLink asChild>
                    <Link to={crumb.to}>{crumb.label}</Link>
                  </BreadcrumbLink>
                ) : (
                  <span>{crumb.label}</span>
                )}
              </BreadcrumbItem>
            </Fragment>
          )
        })}
      </BreadcrumbList>
    </Breadcrumb>
  )
}

export { AppBreadcrumb }
