import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Bell, ClipboardCheck, Activity } from 'lucide-react'
import { qk } from '@/lib/qk'
import { useAuthStore } from '@/stores/auth-store'
import { FloatingCard } from '@/components/popover'
import { countPendingReviews, listRecentTasks, DASH_TASK_STATUS, type DashTask } from '@/features/dashboard/api'
import {
  APPROVAL_TYPE_BADGE,
  APPROVAL_TYPE_LABEL,
  TARGET_TYPE_LABEL,
  TARGET_TYPE_TO_APPROVAL,
  type ApprovalType,
  type ReviewTicketRaw,
} from '@/features/approvals/api'

/** 通知铃铛（30 篇 R 清单口径：通知域后端/SSE 跨会话事件端点暂缺 → 轮询聚合方案，
 *  react-query 30s 轮询复用既有端点，待后端通知端点交付后切推送）：
 *  - 徽标计数 = 待审批 pending 数（仅审批角色）+ 失败任务数；0 不显示，>9 显示 9+；
 *    打开面板即视觉清零，关闭后聚合计数变化（新失败/新待审）才重现（已读游标端点暂缺）。
 *  - 面板两分组：a. 待审批（admin/curator/super_admin）：待办计数 + top3（类型徽标+标题），
 *    行点击 → /console/approvals?id=<id>（ApprovalListPage 深链）；b. 任务动态：最近 5 条
 *    （失败置顶），行点击 → /tasks?taskId=<id>（TasksPage 深链，IX-G-02 通知联动）。
 *  - 数据源复用（只读，零新端点）：countPendingReviews（qk.dashboard.pendingReviews，
 *    与启动台共享缓存）、listRecentTasks（qk.tasks.list）。角色口径=02 信息架构权限矩阵：
 *    member 无审批权限 → 不请求审批端点、只渲染任务动态组。 */

/** 审批组可见角色（02 信息架构/权限矩阵：admin/curator/super_admin；member 仅任务动态） */
const APPROVAL_ROLES = ['admin', 'curator', 'super_admin']

/** 任务状态 → 徽标（30s 轮询口径：运行中 b-orange 脉冲/成功 b-green/失败 b-red；
 *  标签复用 dashboard 域 DASH_TASK_STATUS（live/mock 两种状态方言同表），仅运行中色覆写） */
function taskBadgeMeta(t: DashTask): { label: string; cls: string } {
  if (t.status === 'running') return { label: '运行中', cls: 'b-orange animate-pulse' }
  const meta = DASH_TASK_STATUS[t.status ?? '']
  return meta ? { label: meta.label, cls: meta.badge } : { label: t.status ?? '未知', cls: 'b-gray' }
}

/** F-06（41 号验收 2026-10-05）：live AdminReviewOut 无富 title/type 字段（实测仅
 *  target_type/target_id）——行级兜底模型：标题=富 title ??「中文类型 · target_id 前 8 位」，
 *  类型=target_type 映射（未知 target_type → 无类型，徽标隐藏）。不再 undefined 直渲染空白行。 */
interface BellApprovalRow {
  id: string
  type: ApprovalType | undefined
  title: string
}
function toBellApprovalRow(r: ReviewTicketRaw): BellApprovalRow {
  return {
    id: r.id,
    type: r.type ?? TARGET_TYPE_TO_APPROVAL[r.target_type],
    title: r.title ?? `${TARGET_TYPE_LABEL[r.target_type] ?? r.target_type} · ${r.target_id.slice(0, 8)}`,
  }
}

/** W-08（41 号验收）：live TaskOut 无 name → 「任务 · 短id」（与工作台 recent-tasks 同款兜底），
 *  不再裸 UUID 直渲染 */
function taskRowTitle(t: DashTask): string {
  return t.name ?? `任务 · ${t.id.slice(0, 8)}`
}

export function NotificationBell() {
  const navigate = useNavigate()
  const user = useAuthStore(s => s.user)
  const canApprove = !!user && APPROVAL_ROLES.some(r => user.roles.includes(r))

  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState<DOMRect | null>(null)
  /** 打开面板时刻的聚合计数（视觉清零游标：此后计数变化才重现徽标） */
  const [cleared, setCleared] = useState<number | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  /** 面板开着时点铃铛：FloatingCard 外点逻辑已先行关闭，吞掉随后 click 防重开 */
  const suppressClickRef = useRef(false)

  // ---- 轮询聚合（30s；成员不拉审批端点） ----
  const pendingQ = useQuery({
    queryKey: qk.dashboard.pendingReviews,
    queryFn: countPendingReviews,
    enabled: canApprove,
    refetchInterval: 30_000,
  })
  const tasksQ = useQuery({
    queryKey: qk.tasks.list({ limit: 5 }),
    queryFn: () => listRecentTasks(5),
    refetchInterval: 30_000,
  })

  // fe3 信封收口：pendingQ=countPendingReviews 内部已改 api.list 归一（返回 {items,total} 复合形状不变）；
  // tasksQ=listRecentTasks 改 api.list 归一（{data,meta}）→ 读 .data（be2 后 .items 恒 undefined 被吞空）
  // F-06：items 为 live AdminReviewOut 原始形状（非富 Approval）→ 经行级兜底模型映射后再渲染
  const pendingItems = ((pendingQ.data?.items ?? []) as ReviewTicketRaw[]).map(toBellApprovalRow)
  const pendingCount = pendingQ.data?.total ?? 0
  const allTasks = tasksQ.data?.data ?? []
  const failedTasks = allTasks.filter(t => t.status === 'failed')
  const failedCount = failedTasks.length

  // 徽标 = 待审批 pending 数 + 失败任务数（member 无审批项 → 仅失败任务数）
  const badgeCount = (canApprove ? pendingCount : 0) + failedCount
  const showBadge = badgeCount > 0 && badgeCount !== cleared
  const badgeText = badgeCount > 9 ? '9+' : String(badgeCount)

  // 面板列表：待审批 top3；任务失败置顶后取最近 5
  const topApprovals = pendingItems.slice(0, 3)
  const topTasks = [...failedTasks, ...allTasks.filter(t => t.status !== 'failed')].slice(0, 5)

  // Esc 关闭（外点关闭由 FloatingCard 负责）
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  function toggle() {
    if (suppressClickRef.current) {
      suppressClickRef.current = false
      setOpen(false)
      return
    }
    if (open) {
      setOpen(false)
      return
    }
    setAnchor(btnRef.current?.getBoundingClientRect() ?? null)
    setCleared(badgeCount)
    setOpen(true)
  }

  function go(path: string) {
    setOpen(false)
    navigate(path)
  }

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        aria-label="通知"
        aria-haspopup="dialog"
        aria-expanded={open}
        data-testid="notification-bell"
        onMouseDown={() => {
          if (open) suppressClickRef.current = true
        }}
        onClick={toggle}
        className="relative flex h-8 w-8 items-center justify-center rounded-lg text-label-2 hover:bg-black/5 dark:hover:bg-white/[.08]"
      >
        <Bell size={15} aria-hidden />
        {showBadge && (
          <span
            data-testid="bell-badge"
            style={{ background: 'var(--red)' }}
            className="absolute -right-1.5 -top-1.5 flex h-6 w-6 items-center justify-center rounded-full text-2xs font-semibold text-white"
          >
            {badgeText}
          </span>
        )}
      </button>
      <FloatingCard open={open} anchor={anchor} onClose={() => setOpen(false)} width={360}>
        <div data-testid="notification-panel" className="flex flex-col gap-3">
          {/* a. 待审批（仅 admin/curator/super_admin） */}
          {canApprove && (
            <section>
              <header className="mb-1 flex items-center gap-1.5">
                <ClipboardCheck size={13} aria-hidden className="text-label-3" />
                <b className="text-xs">待审批</b>
                <span className="badge b-orange">{pendingCount}</span>
              </header>
              {pendingQ.isPending ? (
                <p className="px-2 py-1 text-xs text-label-3">正在加载待审批…</p>
              ) : topApprovals.length === 0 ? (
                <p className="px-2 py-1 text-xs text-label-3">暂无待审批工单</p>
              ) : (
                <ul>
                  {topApprovals.map(a => (
                    <li key={a.id}>
                      <button
                        type="button"
                        data-testid="bell-approval-row"
                        title={a.title}
                        onClick={() => go(`/console/approvals?id=${a.id}`)}
                        className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-label-2 hover:bg-black/5 dark:hover:bg-white/[.07]"
                      >
                        {/* F-06：类型可映射才渲染徽标（未知 target_type 隐藏，不留 undefined 空徽标） */}
                        {a.type && (
                          <span className={`badge flex-none ${APPROVAL_TYPE_BADGE[a.type] ?? 'b-gray'}`}>
                            {APPROVAL_TYPE_LABEL[a.type] ?? a.type}
                          </span>
                        )}
                        <span className="truncate">{a.title}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <button
                type="button"
                data-testid="bell-approvals-enter"
                onClick={() => go('/console/approvals')}
                className="mt-1 w-full rounded-lg px-2 py-1.5 text-left text-xs text-accent hover:bg-black/5 dark:hover:bg-white/[.07]"
              >
                进入审批中心 →
              </button>
            </section>
          )}
          {/* b. 任务动态（全员可见；失败置顶） */}
          <section className={canApprove ? 'border-t border-separator pt-3' : undefined}>
            <header className="mb-1 flex items-center gap-1.5">
              <Activity size={13} aria-hidden className="text-label-3" />
              <b className="text-xs">任务动态</b>
              {failedCount > 0 && <span className="badge b-red">{failedCount} 失败</span>}
            </header>
            {tasksQ.isPending ? (
              <p className="px-2 py-1 text-xs text-label-3">正在加载任务动态…</p>
            ) : topTasks.length === 0 ? (
              <p className="px-2 py-1 text-xs text-label-3">暂无任务动态</p>
            ) : (
              <ul>
                {topTasks.map(t => {
                  const meta = taskBadgeMeta(t)
                  const title = taskRowTitle(t)
                  return (
                    <li key={t.id}>
                      <button
                        type="button"
                        data-testid="bell-task-row"
                        title={title}
                        onClick={() => go(`/tasks?taskId=${t.id}`)}
                        className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-label-2 hover:bg-black/5 dark:hover:bg-white/[.07]"
                      >
                        <span className="truncate">{title}</span>
                        <span className={`badge ml-auto flex-none ${meta.cls}`}>{meta.label}</span>
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </div>
      </FloatingCard>
    </>
  )
}
