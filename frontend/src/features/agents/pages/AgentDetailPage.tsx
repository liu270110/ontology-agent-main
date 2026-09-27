import { useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Bot, CircleStop, MessageSquare, Pencil, Play, Send, ListChecks } from 'lucide-react'
import { useAuthStore } from '@/stores/auth-store'
import {
  getAgent,
  healthCheckAgent,
  debugChat,
  listAgentSessions,
  listAgentTasks,
  AGENT_STATUS_LABEL,
  type ConnectionTestResult,
} from '../api'
import { AgentStatusBadge } from './AgentListPage'
import { ToolPickerModal } from '../components/ToolPickerModal'
import { StartStopModal } from '../components/StartStopModal'

/** /agents/:agentId Agent 详情四 Tab（IX-AGT-02；画板 ix-agt-02）：
 *  基本信息（状态/版本/描述）· 工具配置（ToolPicker 只读 + 「调整」入口）
 *  · 适配器（参数表脱敏 + 测试连接）· 运行历史三区（会话列表「继续对话」→ /chat/:sid
 *  + 任务列表 → /tasks + 调试对话窗 trace 标 debug 不计正式历史）。
 *  深链 ?tab=info|tools|adapter|history（26 篇 §11 路由表）。 */

const TABS = [
  { key: 'info', label: '基本信息' },
  { key: 'tools', label: '工具配置' },
  { key: 'adapter', label: '适配器' },
  { key: 'history', label: '运行历史' },
] as const

type TabKey = (typeof TABS)[number]['key']

export function AgentDetailPage() {
  const { agentId = '' } = useParams()
  const navigate = useNavigate()
  const [sp, setSp] = useSearchParams()
  const tab = (sp.get('tab') ?? 'info') as TabKey
  const canWrite = useAuthStore(s => s.can('agent:write'))

  const [pickerOpen, setPickerOpen] = useState(false)
  const [toggle, setToggle] = useState<'start' | 'stop' | null>(null)
  const [probe, setProbe] = useState<ConnectionTestResult | 'testing' | null>(null)
  const [debugInput, setDebugInput] = useState('')
  const [debugLog, setDebugLog] = useState<{ role: 'user' | 'agent'; text: string; trace?: string }[]>([])

  const { data: agent } = useQuery({ queryKey: ['agents', agentId], queryFn: () => getAgent(agentId) })
  const sessions = useQuery({
    queryKey: ['agents', agentId, 'sessions'],
    queryFn: () => listAgentSessions(agentId),
    enabled: tab === 'history',
  })
  const tasks = useQuery({
    queryKey: ['agents', agentId, 'tasks'],
    queryFn: () => listAgentTasks(agentId),
    enabled: tab === 'history',
  })

  function switchTab(t: TabKey) {
    setSp(prev => {
      const next = new URLSearchParams(prev)
      next.set('tab', t)
      return next
    })
  }

  async function runProbe() {
    setProbe('testing')
    setProbe(await healthCheckAgent(agentId))
  }

  async function sendDebug() {
    const content = debugInput.trim()
    if (!content) return
    setDebugInput('')
    setDebugLog(l => [...l, { role: 'user', text: content }])
    const res = await debugChat(agentId, content)
    setDebugLog(l => [...l, { role: 'agent', text: res.reply, trace: res.trace_id }])
  }

  if (!agent) {
    return (
      <div className="mx-auto max-w-[1080px]">
        <div className="empty mt-10"><div className="t">加载中…</div></div>
      </div>
    )
  }

  const boundNames = agent.tools

  return (
    <div className="mx-auto max-w-[1080px]" data-testid="agent-detail">
      <div className="flex flex-wrap items-center gap-2">
        <Bot size={18} aria-hidden />
        <h1 className="text-lg font-bold">{agent.name}</h1>
        <AgentStatusBadge status={agent.status} />
        <span className="badge b-gray">{agent.version}</span>
        {canWrite && (
          <span className="ml-auto flex gap-1.5">
            <button type="button" className="btn btn-g btn-sm">
              <Pencil size={12} aria-hidden /> 编辑
            </button>
            {agent.status === 'running' ? (
              <button type="button" className="btn btn-d btn-sm" data-testid="agt-detail-stop" onClick={() => setToggle('stop')}>
                <CircleStop size={12} aria-hidden /> 停止
              </button>
            ) : (
              <button type="button" className="btn btn-p btn-sm" data-testid="agt-detail-start" onClick={() => setToggle('start')}>
                <Play size={12} aria-hidden /> 启动
              </button>
            )}
          </span>
        )}
      </div>

      {/* 四 Tab */}
      <div className="seg mt-3" role="tablist" aria-label="Agent 详情四视图">
        {TABS.map(t => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={`seg-btn ${tab === t.key ? 'on' : ''}`}
            data-testid={`agt-tab-${t.key}`}
            onClick={() => switchTab(t.key)}
          >
            {t.label}
          </button>
        ))}
        <span className="mono ml-auto self-center text-[11px] text-label-3">?tab=history 深链可达</span>
      </div>

      {/* 基本信息 */}
      {tab === 'info' && (
        <div className="card mt-4 !p-5" data-testid="agt-panel-info">
          <div className="grid grid-cols-1 gap-x-6 gap-y-2 text-xs md:grid-cols-2">
            <div className="flex justify-between gap-3"><span className="text-label-3">实例 ID</span><span className="mono">{agent.id}</span></div>
            <div className="flex justify-between gap-3"><span className="text-label-3">状态 / 版本</span><span>{AGENT_STATUS_LABEL[agent.status]} · {agent.version}</span></div>
            <div className="flex justify-between gap-3"><span className="text-label-3">适配器</span><span>{agent.adapter} · {agent.adapter_version}</span></div>
            <div className="flex justify-between gap-3"><span className="text-label-3">负责人</span><span>{agent.owner}</span></div>
            <div className="flex justify-between gap-3"><span className="text-label-3">描述</span><span className="max-w-[320px] text-right">{agent.description}</span></div>
            <div className="flex justify-between gap-3"><span className="text-label-3">健康</span><span>最近探活 RTT {agent.health.rtt_ms || '—'}ms · 连续失败 {agent.health.consecutive_failures}</span></div>
          </div>
        </div>
      )}

      {/* 工具配置 */}
      {tab === 'tools' && (
        <div className="card mt-4 !p-5" data-testid="agt-panel-tools">
          <div className="text-[11px] font-semibold text-label-3">已注入工具（ToolPicker 只读视图）</div>
          <div className="mt-2 space-y-1.5">
            {boundNames.length === 0 && <div className="text-[11px] text-label-3">未注入任何工具。</div>}
            {boundNames.map(n => (
              <div key={n} className="flex items-center gap-2 rounded-lg border border-separator px-3 py-2 text-xs">
                <span className="badge b-blue">已启用</span>
                <span className="mono font-semibold">{n}</span>
                <span className="ml-auto text-[11px] text-label-3">来源见工具注册中心</span>
              </div>
            ))}
          </div>
          {canWrite && (
            <div className="mt-3 flex items-center gap-2">
              <button type="button" className="btn btn-p btn-sm" data-testid="agt-tools-adjust" onClick={() => setPickerOpen(true)}>
                调整（打开 ToolPicker）
              </button>
              <span className="text-[11px] text-label-3">保存经 PUT /agents/{agentId}/tools 生效</span>
            </div>
          )}
        </div>
      )}

      {/* 适配器 */}
      {tab === 'adapter' && (
        <div className="card mt-4 !p-5" data-testid="agt-panel-adapter">
          <div className="grid grid-cols-1 gap-x-6 gap-y-2 text-xs md:grid-cols-2">
            <div className="flex justify-between gap-3"><span className="text-label-3">适配器</span><span>{agent.adapter} · RPC 模式 · {agent.adapter_version}</span></div>
            <div className="flex justify-between gap-3"><span className="text-label-3">endpoint（脱敏）</span><span className="mono">{agent.endpoint_masked}</span></div>
            <div className="flex justify-between gap-3">
              <span className="text-label-3">token（脱敏）</span>
              <span className="flex items-center gap-1.5">
                <span className="mono">{agent.token_masked}</span>
                <span className="badge b-gray">脱敏</span>
              </span>
            </div>
            <div className="flex justify-between gap-3"><span className="text-label-3">调用超时</span><span>{agent.timeout_ms} ms</span></div>
          </div>
          <div className="mt-3 flex items-center gap-2">
            <button type="button" className="btn btn-s btn-sm" data-testid="agt-adapter-probe" onClick={() => void runProbe()}>
              测试连接
            </button>
            {probe === 'testing' && <span className="text-[11px] text-label-3">探活中…</span>}
            {probe && probe !== 'testing' && probe.ok && (
              <span className="badge b-green" data-testid="agt-adapter-probe-ok">
                上次探活 · RTT {probe.rtt_ms}ms · {probe.protocol}
              </span>
            )}
            {probe && probe !== 'testing' && !probe.ok && (
              <span className="badge b-red" data-testid="agt-adapter-probe-fail">
                {probe.code} {probe.message}（建议：{probe.suggestion}）
              </span>
            )}
          </div>
          <div className="fhint">探活：POST /agents/{agentId}/health-check（E-5003 时给出诊断建议）。</div>
        </div>
      )}

      {/* 运行历史三区 */}
      {tab === 'history' && (
        <div className="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-3" data-testid="agt-panel-history">
          {/* 会话列表 */}
          <div className="card !p-4">
            <div className="flex items-center gap-2">
              <MessageSquare size={13} aria-hidden />
              <b className="text-xs">会话列表</b>
              <span className="badge b-gray ml-auto">近 7 天 {sessions.data?.items.length ?? 0} 条</span>
            </div>
            <div className="mt-1 text-[11px] text-label-3">列表口径 GET /sessions?agent={agentId}</div>
            <div className="mt-2 space-y-2">
              {(sessions.data?.items ?? []).map(s => (
                <div key={s.id} className="rounded-xl border border-separator px-3 py-2">
                  <div className="text-xs font-semibold">{s.title}</div>
                  <div className="mt-0.5 text-[11px] text-label-3">
                    {s.status} · 消息 {s.message_count}
                  </div>
                  <Link className="btn btn-p btn-sm mt-1.5" to={`/chat/${s.id}`}>
                    继续对话
                  </Link>
                </div>
              ))}
              {(sessions.data?.items ?? []).length === 0 && <div className="text-[11px] text-label-3">暂无会话。</div>}
            </div>
          </div>

          {/* 任务列表 */}
          <div className="card !p-4">
            <div className="flex items-center gap-2">
              <ListChecks size={13} aria-hidden />
              <b className="text-xs">任务列表</b>
              <span className="badge b-gray ml-auto">{tasks.data?.items.length ?? 0} 条</span>
            </div>
            <div className="mt-1 text-[11px] text-label-3">跳转 /tasks?agent={agentId}</div>
            <div className="mt-2 space-y-2">
              {(tasks.data?.items ?? []).map(t => (
                <div key={t.id} className="rounded-xl border border-separator px-3 py-2 text-[11px]">
                  <div className="mono font-semibold">{t.id}</div>
                  <div className="mt-0.5 text-label-2">{t.title}</div>
                  <div className="mt-1 flex items-center gap-2">
                    <span className={`badge ${t.status === 'done' ? 'b-green' : t.status === 'failed' ? 'b-red' : t.status === 'running' ? 'b-blue' : 'b-gray'}`}>
                      {t.status === 'done' ? '已完成' : t.status === 'failed' ? '失败' : t.status === 'running' ? '运行中' : '排队中'}
                    </span>
                    {t.status === 'failed' && (
                      <button type="button" className="btn btn-g btn-sm !px-2" onClick={() => navigate(`/tasks?agent=${agentId}`)}>
                        重试
                      </button>
                    )}
                  </div>
                </div>
              ))}
              {(tasks.data?.items ?? []).length === 0 && <div className="text-[11px] text-label-3">暂无任务。</div>}
            </div>
          </div>

          {/* 调试对话窗 */}
          <div className="card !p-4">
            <div className="flex items-center gap-2">
              <b className="text-xs">调试对话</b>
              <span className="badge b-purple">debug trace</span>
            </div>
            <div className="mt-2 min-h-[160px] space-y-2" data-testid="agt-debug-log">
              {debugLog.length === 0 && (
                <div className="text-[11px] text-label-3">发送测试消息… 调试会话不计入正式历史（trace 独立标记 debug）。</div>
              )}
              {debugLog.map((m, i) => (
                <div
                  key={i}
                  className="max-w-[95%] rounded-xl px-3 py-2 text-[11px] leading-5"
                  style={
                    m.role === 'user'
                      ? { background: 'var(--accent)', color: 'var(--on-accent)', marginLeft: 'auto' }
                      : { background: 'var(--surface-2)' }
                  }
                >
                  {m.text}
                  {m.trace && <span className="mono mt-1 block text-2xs opacity-70">trace {m.trace} · debug</span>}
                </div>
              ))}
            </div>
            <div className="mt-2 flex gap-1.5">
              <input
                className="input !h-8"
                placeholder="发送调试消息…"
                aria-label="调试消息输入"
                data-testid="agt-debug-input"
                value={debugInput}
                onChange={e => setDebugInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') void sendDebug()
                }}
              />
              <button type="button" className="btn btn-p btn-sm" aria-label="发送调试消息" data-testid="agt-debug-send" onClick={() => void sendDebug()}>
                <Send size={12} aria-hidden />
              </button>
            </div>
            <div className="fhint">调试会话不计入正式历史（trace 独立标记 debug）。</div>
          </div>
        </div>
      )}

      <ToolPickerModal
        open={pickerOpen}
        agentId={agentId}
        initialTools={agent.tools}
        onClose={() => setPickerOpen(false)}
      />
      <StartStopModal agent={agent} action={toggle} onClose={() => setToggle(null)} />
    </div>
  )
}
