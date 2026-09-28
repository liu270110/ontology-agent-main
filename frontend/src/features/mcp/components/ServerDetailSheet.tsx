import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Pencil, RefreshCw, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { Sheet } from '@/components/sheet'
import { Tooltip } from '@/components/tooltip'
import { enableMcpTool, getServer, refreshServer, MCP_STATUS_LABEL, type McpServerRow } from '../api'

/** IX-MCP-02 Server 详情抽屉（26 篇 §9.3；画板 ix-mcp-02）：480px——
 *  连接信息（传输/URL 脱敏/鉴权方式）+ 健康状态卡（最近探活/延迟/连续失败 + 24h 探活带）
 *  + 纳管工具列表（启停开关 + 跳 IX-TLS-01 详情）+ 刷新清单 / 编辑连接 / 移除（IX-MCP-03）。 */

export function ServerDetailSheet({
  serverId,
  onClose,
  onRemove,
}: {
  serverId: string | null
  onClose: () => void
  onRemove: (s: McpServerRow) => void
}) {
  const qc = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)

  const { data: server } = useQuery({
    queryKey: ['mcp', 'detail', serverId],
    queryFn: () => getServer(serverId!),
    enabled: !!serverId,
  })

  const toggle = useMutation({
    mutationFn: (t: { tool_id: string; enabled: boolean }) => enableMcpTool(t.tool_id, !t.enabled),
    onSuccess: res => {
      void qc.invalidateQueries({ queryKey: ['mcp'] })
      toast.success(`${res.tool_id} 已${res.enabled ? '审核开启' : '停用'}`, {
        description: res.enabled ? '外部工具逐项审核开启；annotations 仅作提示，不作授权依据' : '停用写审计，可再次开启',
      })
    },
  })

  const refresh = useMutation({
    mutationFn: () => refreshServer(serverId!),
    onSuccess: async res => {
      setRefreshing(false)
      void qc.invalidateQueries({ queryKey: ['mcp'] })
      toast.success(`清单已刷新 · 延迟 ${res.latency_ms}ms`, { description: `发现 ${res.discovered_count} 项` })
    },
    onError: () => setRefreshing(false),
  })

  if (!serverId || !server) return null
  const adopted = server.tools.filter(t => t.adopted)
  const notAdopted = server.tools.filter(t => !t.adopted)
  const okProbes = server.probes_24h.filter(p => p.ok).length

  return (
    <Sheet open onClose={onClose} title={`${server.name} · ${MCP_STATUS_LABEL[server.status]}`} width={480}>
      <div className="px-5 py-4">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className={`badge ${server.status === 'healthy' ? 'b-green' : server.status === 'failing' ? 'b-red' : 'b-gray'}`}>
            {MCP_STATUS_LABEL[server.status]}
          </span>
          <span className="mono text-[11px] text-label-3">{server.url_masked}</span>
        </div>

        {/* 连接信息 */}
        <div className="mt-3">
          <div className="text-[11px] font-semibold text-label-3">连接信息</div>
          <div className="mt-1.5 space-y-1.5 text-[11px]">
            <div className="flex justify-between gap-3"><span className="text-label-3">传输</span><span>{server.transport}</span></div>
            <div className="flex justify-between gap-3"><span className="text-label-3">URL</span><span className="mono">{server.url_masked}</span></div>
            <div className="flex justify-between gap-3">
              <span className="text-label-3">鉴权方式</span>
              <span className="flex items-center gap-1.5">
                {server.auth} <span className="mono badge b-gray">{server.token_masked}</span>
              </span>
            </div>
            <div className="flex justify-between gap-3"><span className="text-label-3">协议版本</span><span>{server.protocol} · Server {server.server_version}</span></div>
            <div className="flex justify-between gap-3"><span className="text-label-3">接入记录</span><span>{server.added_at.slice(0, 10)} · {server.added_by} · 接入向导</span></div>
          </div>
        </div>

        {/* 健康状态卡 */}
        <div
          className="mt-4 rounded-xl px-3.5 py-3"
          style={{ background: server.status === 'failing' ? 'var(--red-soft)' : 'var(--green-soft)' }}
          data-testid="mcp-health-card"
        >
          <div className="flex items-center gap-2">
            <b className={`text-xs ${server.status === 'failing' ? 'text-red' : 'text-green'}`}>
              {server.status === 'healthy' ? '健康 · 捂断路径闭合' : server.status === 'failing' ? `连续失败 ${server.consecutive_failures} 次` : '健康 · 未知（待首次探活）'}
            </b>
            <span className="badge b-gray ml-auto">连续失败 {server.consecutive_failures}</span>
          </div>
          <div className="mt-1 text-[11px] text-label-2">
            最近探活 {server.last_probe.slice(11, 16)} · 延迟 {server.latency_ms || '—'}ms · 探活周期 60s
          </div>
          {server.probes_24h.length > 0 && (
            <>
              <div className="mt-2 flex gap-[2px]" aria-label="近 24 小时探活" data-testid="mcp-probe-strip">
                {server.probes_24h.map((p, i) => (
                  <div
                    key={i}
                    className="h-3 flex-1 rounded-[2px]"
                    style={{ background: p.ok ? 'var(--green)' : 'var(--orange)' }}
                    title={p.ok ? '成功' : '超时'}
                  />
                ))}
              </div>
              <div className="mt-1 text-2xs text-label-3">
                近 24h 探活：{okProbes} 成功 / {server.probes_24h.length - okProbes} 超时（自动恢复）
              </div>
            </>
          )}
        </div>

        {/* 纳管工具 */}
        <div className="mt-4">
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-semibold text-label-3">纳管工具</span>
            <span className="badge b-blue">
              纳管 {server.adopted_count} / 发现 {server.discovered_count}
            </span>
            <button
              type="button"
              className="btn btn-g btn-sm ml-auto !px-2"
              data-testid="mcp-refresh"
              onClick={() => {
                setRefreshing(true)
                refresh.mutate()
              }}
            >
              <RefreshCw size={11} className={refreshing ? 'animate-spin' : ''} aria-hidden /> 刷新清单
            </button>
          </div>
          {notAdopted.length > 0 && (
            <div className="mt-1 text-[11px] text-label-3">{notAdopted.length} 项写入类未纳管（需单独申请）；外部工具默认未启用，逐项审核开启。</div>
          )}
          <div className="mt-2 space-y-1.5">
            {adopted.map(t => (
              <div key={t.tool_id} className="flex items-center gap-2 rounded-xl border border-separator px-3 py-2" data-testid={`mcp-tool-${t.name}`}>
                <span className="mono text-xs font-semibold">{t.name}</span>
                {t.read_only && <span className="badge b-gray">只读</span>}
                {t.write && <span className="badge b-orange">写入 · 需审批</span>}
                <span className="ml-auto flex items-center gap-2">
                  {/* S8 Tooltip 切片：启停动作影响面提示（审计留痕 + 沙箱白名单同步） */}
                  <Tooltip content="启停会写审计日志并同步沙箱工具白名单">
                    <button
                      type="button"
                      role="switch"
                      aria-checked={t.enabled}
                      aria-label={`启停 ${t.name}`}
                      data-testid={`mcp-switch-${t.name}`}
                      className="relative h-5 w-9 rounded-full transition-colors"
                      style={{ background: t.enabled ? 'var(--green)' : 'var(--surface-2)', border: '1px solid var(--separator)' }}
                      onClick={() => toggle.mutate({ tool_id: t.tool_id, enabled: t.enabled })}
                    >
                      <span className="absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white shadow transition-all" style={{ left: t.enabled ? 18 : 3 }} />
                    </button>
                  </Tooltip>
                  <Link to="/console/tools" className="text-[11px] text-accent hover:underline" title="跳 IX-TLS-01 工具详情">
                    详情
                  </Link>
                </span>
              </div>
            ))}
            {adopted.length === 0 && <div className="text-[11px] text-label-3">未纳管任何工具。</div>}
          </div>
        </div>

        {/* 操作条 */}
        <div className="hairline-t mt-4 flex gap-2 pt-3">
          <button type="button" className="btn btn-g btn-sm">
            <Pencil size={12} aria-hidden /> 编辑连接
          </button>
          <button type="button" className="btn btn-d btn-sm ml-auto" data-testid="mcp-remove-open" onClick={() => onRemove(server)}>
            <Trash2 size={12} aria-hidden /> 移除 Server
          </button>
        </div>
      </div>
    </Sheet>
  )
}
