import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, Bot, CircleStop, Play, Plus } from 'lucide-react'
import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth-store'
import { ErrorState, SkeletonCards } from '@/components/states'
import { listAgents, AGENT_STATUS_LABEL, adapterText, adapterVersionText, type AgentStatus, type PlatformAgent } from '../api'
import { RegisterWizard } from '../components/RegisterWizard'
import { StartStopModal } from '../components/StartStopModal'

/** /agents Agent 管理（宿主画框 p-agents；26 篇 §8.2）：
 *  卡片列表（状态徽标 运行中/已停止/异常 + 活跃会话/排队任务计数）
 *  + IX-AGT-01 注册向导 + IX-AGT-04 启停确认。
 *  roles=member 可见自己实例、admin 全量（routes meta.roles；前端隐藏≠授权，服务端 PDP 兜底）。 */

/** 双口径「活跃」判定：mock running / live enabled（fe1-F2）均视为可停止态 */
export function isAgentActive(status: AgentStatus): boolean {
  return status === 'running' || status === 'enabled'
}

export function AgentStatusBadge({ status }: { status: AgentStatus }) {
  // live 后端 enabled/disabled 口径（fe1-F2）：enabled 视同运行中（绿），disabled 视同已停止（灰）
  const cls = status === 'running' || status === 'enabled' ? 'b-green' : status === 'stopped' || status === 'disabled' ? 'b-gray' : 'b-red'
  const dot = status === 'running' || status === 'enabled' ? 'd-green' : status === 'error' ? 'd-red' : ''
  return (
    <span className={`badge ${cls}`}>
      <span className={`dot ${dot}`} style={status === 'stopped' || status === 'disabled' ? { background: 'var(--label-3)' } : undefined} />
      {AGENT_STATUS_LABEL[status]}
    </span>
  )
}

export function AgentListPage() {
  const navigate = useNavigate()
  const canWrite = useAuthStore(s => s.can('agent:write'))
  const [wizardOpen, setWizardOpen] = useState(false)
  const [toggle, setToggle] = useState<{ agent: PlatformAgent; action: 'start' | 'stop' } | null>(null)

  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ['agents', 'list'], queryFn: listAgents })
  // {items} 解包防御（fe1-F2）：live 后端裸分页体 {items,offset,limit}（agents.json 实测）经 client
  // 双形态兼容后 items 应在；容忍 items 缺失/非数组与裸数组两种降级，绝不让 undefined 进 map。
  const agents = useMemo<PlatformAgent[]>(() => {
    if (Array.isArray(data)) return data
    const items = (data as { items?: unknown } | undefined)?.items
    return Array.isArray(items) ? (items as PlatformAgent[]) : []
  }, [data])

  return (
    <div className="mx-auto max-w-[1080px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">Agent 管理</h1>
        <span className="text-xs text-label-3">适配器实例 · 启停带健康自检与审计 · 工具注入留痕</span>
        {canWrite && (
          <button type="button" className="btn btn-p btn-sm ml-auto" onClick={() => setWizardOpen(true)} data-testid="agt-register-open">
            <Plus size={13} aria-hidden /> 注册 Agent
          </button>
        )}
      </div>

      <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {agents.map(a => (
          <div key={a.id} className="card flex flex-col !p-4" data-testid={`agent-card-${a.id}`}>
            <div className="flex items-center gap-2">
              <Bot size={15} aria-hidden />
              <b className="truncate text-sm">{a.name}</b>
              <span className="mono ml-auto text-[11px] text-label-3">{adapterText(a)} {adapterVersionText(a)}</span>
            </div>
            <div className="mt-1.5 flex items-center gap-2">
              <AgentStatusBadge status={a.status} />
              {a.version && <span className="badge b-gray">{a.version}</span>}
            </div>
            <p className="mt-2 line-clamp-2 min-h-8 text-[11px] leading-4 text-label-2">{a.description}</p>
            <div className="mt-2 text-[11px] text-label-3">
              {/* live 后端无 tools/active_sessions/queued_tasks（fe1-F2 实测）：缺省按 0/— 展示不崩 */}
              活跃会话 {a.active_sessions ?? 0} · 排队任务 {a.queued_tasks ?? 0} · 工具 {a.tools?.length ?? 0} 个
            </div>
            <div className="hairline-t mt-3 flex items-center gap-1.5 pt-3">
              <button type="button" className="btn btn-g btn-sm" onClick={() => navigate(`/agents/${a.id}`)}>
                详情 <ArrowRight size={12} aria-hidden />
              </button>
              {canWrite && isAgentActive(a.status) && (
                <button type="button" className="btn btn-d btn-sm" data-testid={`agt-stop-${a.id}`} onClick={() => setToggle({ agent: a, action: 'stop' })}>
                  <CircleStop size={12} aria-hidden /> 停止
                </button>
              )}
              {canWrite && !isAgentActive(a.status) && (
                <button type="button" className="btn btn-p btn-sm" data-testid={`agt-start-${a.id}`} onClick={() => setToggle({ agent: a, action: 'start' })}>
                  <Play size={12} aria-hidden /> 启动
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* S8 状态切片：加载骨架卡（数量≈mock 实例 3）/ 错误态（重试=refetch） */}
      {isLoading && (
        <div className="mt-4">
          <SkeletonCards count={3} />
        </div>
      )}
      {isError && (
        <div className="mt-4">
          <ErrorState
            message={error instanceof Error ? error.message : undefined}
            code={error instanceof ApiError ? error.code : undefined}
            onRetry={() => void refetch()}
          />
        </div>
      )}
      {!isLoading && !isError && agents.length === 0 && (
        <div className="empty mt-6">
          <div className="t">还没有注册的 Agent</div>
          <div className="d">从「注册 Agent」开始：选适配器 → 填接入参数 → 注入工具。</div>
        </div>
      )}

      <RegisterWizard open={wizardOpen} onClose={() => setWizardOpen(false)} />
      <StartStopModal agent={toggle?.agent ?? null} action={toggle?.action ?? null} onClose={() => setToggle(null)} />
    </div>
  )
}
