import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Plug, Plus } from 'lucide-react'
import { listServers, MCP_STATUS_LABEL, type McpServerRow } from '../api'
import { AddServerWizard } from '../components/AddServerWizard'
import { ServerDetailSheet } from '../components/ServerDetailSheet'
import { RemoveServerModal } from '../components/RemoveServerModal'

/** /mcp MCP 管理（宿主画框 p-mcp；26 篇 §9.3）：
 *  Server 列表（传输/健康徽标/工具数）+ IX-MCP-01 接入向导 + IX-MCP-02 详情抽屉
 *  + IX-MCP-03 移除确认。外部 Server 默认不可信（发现 → 逐项纳管 → 审核开启）。 */

export function McpPage() {
  const [wizardOpen, setWizardOpen] = useState(false)
  const [detailId, setDetailId] = useState<string | null>(null)
  const [removing, setRemoving] = useState<McpServerRow | null>(null)

  const { data, isLoading } = useQuery({ queryKey: ['mcp', 'list'], queryFn: listServers })
  const servers = data?.items ?? []
  const healthy = servers.filter(s => s.status === 'healthy').length

  return (
    <div className="mx-auto max-w-[1080px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">MCP 管理</h1>
        <span className="badge b-green">出口运行中</span>
        <span className="text-xs text-label-3">外部 Server 默认不可信 · 调用统一经 L7 MCP 网关</span>
        <button type="button" className="btn btn-p btn-sm ml-auto" onClick={() => setWizardOpen(true)} data-testid="mcp-wizard-open">
          <Plus size={13} aria-hidden /> 接入 Server
        </button>
      </div>

      <div className="card mt-4 !p-0" data-testid="mcp-table">
        <table className="tbl">
          <thead>
            <tr>
              <th>Server</th>
              <th>传输</th>
              <th>鉴权</th>
              <th>健康</th>
              <th>延迟</th>
              <th>工具</th>
            </tr>
          </thead>
          <tbody>
            {servers.map(s => (
              <tr key={s.id} className="cursor-pointer" data-testid={`mcp-tr-${s.name}`} onClick={() => setDetailId(s.id)}>
                <td>
                  <span className="mono font-semibold">{s.name}</span>
                  <div className="text-[10.5px] text-label-3">{s.desc}</div>
                </td>
                <td className="mono text-[11.5px]">{s.transport}</td>
                <td className="text-[11.5px]">{s.auth}</td>
                <td>
                  <span className={`badge ${s.status === 'healthy' ? 'b-green' : s.status === 'failing' ? 'b-red' : 'b-gray'}`}>
                    {MCP_STATUS_LABEL[s.status]}
                  </span>
                </td>
                <td className="text-[11.5px]">{s.latency_ms ? `${s.latency_ms}ms` : '—'}</td>
                <td className="text-[11.5px]">
                  纳管 {s.adopted_count} / 发现 {s.discovered_count}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {isLoading && <div className="empty"><div className="t">加载中…</div></div>}
      </div>

      {!isLoading && servers.length === 0 && (
        <div className="empty mt-6">
          <Plug size={28} aria-hidden />
          <div className="t">还没有接入的 MCP Server</div>
          <div className="d">「接入 Server」三步：连接配置 → 测试与发现 → 确认摘要。</div>
        </div>
      )}

      {servers.length > 0 && (
        <div className="mt-3 text-[11px] text-label-3">
          {servers.length} 个 Server · {healthy} 个健康 · 平台能力出口清单 GET /mcp/capabilities（§5.7）
        </div>
      )}

      <AddServerWizard open={wizardOpen} onClose={() => setWizardOpen(false)} />
      <ServerDetailSheet serverId={detailId} onClose={() => setDetailId(null)} onRemove={s => setRemoving(s)} />
      <RemoveServerModal server={removing} onClose={() => setRemoving(null)} />
    </div>
  )
}
