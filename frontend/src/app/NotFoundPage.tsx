import { Link, useLocation } from 'react-router-dom'
import { EmptyState } from '@/components/states'
import { useUiStore } from '@/stores/ui-store'

/** 404 全局状态页（39 号对账 G-S1 / 画板 p-status 格 1）：App.tsx 通配 `*` 分支由静默重定向
 *  改挂本页——保留未匹配诊断价值（用户输入路径 mono 原样展示），主动作返回主页（btn-p），
 *  次动作 ⌘K 唤起命令面板「搜索内容」（CommandMenu 已上移 App 全局挂载，裸页可达）。
 *  基元纪律：EmptyState hero 档承载（404 mono 大字入 title 插槽，不加新样式族）。 */
export function NotFoundPage() {
  const location = useLocation()
  const setCommandOpen = useUiStore(s => s.setCommandOpen)
  return (
    <div
      className="flex min-h-screen flex-col items-center justify-center p-8 text-label"
      style={{ background: 'radial-gradient(40% 35% at 80% 10%, var(--accent-soft), transparent 60%), var(--bg)' }}
    >
      <EmptyState
        hero
        title={
          <>
            <span
              aria-hidden
              className="mono num-tick block leading-none"
              style={{ fontSize: 44, fontWeight: 700, letterSpacing: '-2px', color: 'var(--label-3)' }}
            >
              404
            </span>
            页面走丢了
          </>
        }
        desc={
          <>
            您访问的地址不存在或已被移动
            <span className="mono dim mt-1 block break-all" data-testid="notfound-path">
              {location.pathname}
              {location.search}
            </span>
          </>
        }
        action={
          <>
            <Link to="/" className="btn btn-p btn-sm">
              返回主页
            </Link>
            <button type="button" className="btn btn-g btn-sm" onClick={() => setCommandOpen(true)}>
              搜索内容（⌘K）
            </button>
          </>
        }
      />
    </div>
  )
}
