import type { ReactNode } from 'react'
import { useAuthStore } from '@/stores/auth-store'
import { ForbiddenPage } from './ForbiddenPage'
import type { RouteMeta } from './routes'

/** 路由级权限守卫（16 篇 §4.2 步骤 3/4）：roles 粗过滤 + permission scope 细判；
 *  不足渲染 403 占位页（不重定向，防循环）。铁律：前端隐藏≠授权，服务端 PDP 五步判定兜底。 */
export function RouteGuard({ meta, children }: { meta: RouteMeta; children: ReactNode }) {
  const hasAnyRole = useAuthStore(s => s.hasAnyRole)
  const can = useAuthStore(s => s.can)

  if (meta.roles && meta.roles.length > 0 && !hasAnyRole(meta.roles)) {
    return <ForbiddenPage />
  }
  if (meta.permission && !can(meta.permission)) {
    return <ForbiddenPage permission={meta.permission} />
  }
  return children
}
