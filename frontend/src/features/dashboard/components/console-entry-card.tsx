import { Link } from 'react-router-dom'
import { ArrowRight, ShieldCheck } from 'lucide-react'

/** 管理控制台入口卡（模块化第一批自 DashboardPage 拆出，行为零变化）：
 *  双区 IA：不常用功能收进控制台，与主页形成双区心智；
 *  桌面端经壳桥开独立窗口，Web 降级为路由跳转。 */

export function ConsoleEntryCard({
  pendingTotal,
  isPending,
  isError,
}: {
  pendingTotal: number
  isPending: boolean
  isError: boolean
}) {
  return (
    <Link
      to="/console"
      data-testid="console-entry-card"
      onClick={e => {
        const bridge = (window as { oaDesktop?: { openConsole?(): Promise<boolean> } }).oaDesktop
        if (bridge?.openConsole) {
          e.preventDefault()
          void bridge.openConsole()
        }
      }}
      className="card glass-interactive flex flex-none items-center gap-3 rounded-xl border border-separator bg-surface px-4 py-3 hover:border-accent"
    >
      <span className="flex h-9 w-9 flex-none items-center justify-center rounded-lg bg-accent text-white">
        <ShieldCheck size={17} aria-hidden />
      </span>
      <span className="min-w-0">
        <b className="flex items-center gap-1 text-sm">管理控制台</b>
        <small className="block text-[11px] text-label-3">
          {pendingTotal > 0 && !isPending && !isError
            ? `${pendingTotal} 项待审批 · 治理/配置/观测`
            : '治理 · 能力配置 · 观测'}
        </small>
      </span>
      <ArrowRight size={14} aria-hidden className="flex-none text-label-3" />
    </Link>
  )
}
