import { useEffect, useState } from 'react'
import type { ReactElement } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuthStore } from '@/stores/auth-store'
import { trySilentRefresh } from '@/api/client'

/** 认证守卫（16 篇 §4.2）：匿名访问受保护区 → /login?next=回跳地址。
 *  本地仅存 refresh（access 过期）时先静默刷新尝试恢复会话（client 单飞），失败才重定向。 */
export function RequireAuth({ children }: { children: ReactElement }) {
  const status = useAuthStore(s => s.status)
  const refreshToken = useAuthStore(s => s.refreshToken)
  const location = useLocation()
  /** 本组件生命周期内只自动尝试一次静默恢复（防循环） */
  const [resumeAttempted, setResumeAttempted] = useState(false)

  const needResume = status === 'anonymous' && !!refreshToken && !resumeAttempted

  useEffect(() => {
    if (!needResume) return
    setResumeAttempted(true)
    void trySilentRefresh()
  }, [needResume])

  if (status === 'anonymous') {
    if (needResume) return null
    const next = encodeURIComponent(location.pathname + location.search)
    return <Navigate to={`/login?next=${next}`} replace />
  }
  return children
}
