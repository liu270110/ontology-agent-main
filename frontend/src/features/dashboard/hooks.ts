import { useQuery } from '@tanstack/react-query'
import { qk } from '@/lib/qk'
import { countKbDocuments, countOntologies, countPendingReviews, countTodaySessions, listRecentSessions, listRecentTasks } from './api'

/** 工作台查询集中 hook（模块化第一批自 DashboardPage 拆出，行为零变化）：
 *  聚合计数（真数据；供启动台指标与副行）+ 入口列表（真数据；空列表=合法真实态 →
 *  优雅空态；启动台只露 top3）。TanStack Query 调用全部收敛于此，展示组件只吃数据+回调。 */

export function useDashboardQueries() {
  // ---- 聚合计数（真数据；供启动台指标与副行） ----
  const pendingQ = useQuery({ queryKey: qk.dashboard.pendingReviews, queryFn: countPendingReviews })
  const ontoQ = useQuery({ queryKey: qk.dashboard.ontologyCount, queryFn: countOntologies })
  const todayQ = useQuery({ queryKey: qk.dashboard.todaySessions, queryFn: countTodaySessions })
  // ---- 入口列表（真数据；空列表=合法真实态 → 优雅空态；启动台只露 top3） ----
  const sessionsQ = useQuery({ queryKey: qk.session.list({ limit: 3 }), queryFn: () => listRecentSessions(3) })
  const tasksQ = useQuery({ queryKey: qk.tasks.list({ limit: 3 }), queryFn: () => listRecentTasks(3) })
  // ---- 新手引导第③步判定（S-AD 切片）：知识文档非空（limit=1 判存在，live 挂起则保持未完成） ----
  const docsQ = useQuery({ queryKey: ['dashboard', 'kb-doc-count'], queryFn: countKbDocuments })

  // 指标口径用全量返回（mock 忽略 limit 参数），展示口径收敛 top3
  const allSessions = sessionsQ.data?.items ?? []
  const allTasks = tasksQ.data?.items ?? []
  const sessions = allSessions.slice(0, 3)
  const tasks = allTasks.slice(0, 3)
  const pendingTotal = pendingQ.data?.total ?? 0
  const runningTasks = allTasks.filter(t => t.status === 'running').length

  const subLine = (() => {
    const parts: string[] = []
    if (!pendingQ.isPending && !pendingQ.isError) parts.push(`今天有 ${pendingTotal} 项审批待处理`)
    if (!tasksQ.isPending && !tasksQ.isError) parts.push(`抽取流水线 ${runningTasks} 个任务运行中`)
    return parts.length > 0 ? `${parts.join('，')}。` : '正在从各域汇聚今日工作台数据…'
  })()

  return {
    pendingQ,
    ontoQ,
    todayQ,
    sessionsQ,
    tasksQ,
    docsQ,
    sessions,
    tasks,
    pendingTotal,
    runningTasks,
    subLine,
  }
}
