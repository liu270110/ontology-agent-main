import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Trash2, TriangleAlert } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { removeServer, type McpServerRow } from '../api'

/** IX-MCP-03 移除 Server 确认（26 篇 §9.3；画板 ix-mcp-03）：danger 弹窗——
 *  级联影响（N 个纳管工具将从注册中心移除 + M 个在用 Agent 提示清单）
 *  + 输入 Server 名强制确认；动作写审计（trace_id 落 admin 审计日志）。
 *  DELETE /mcp/servers/{id}（R 预登记端点，见 R 清单）。 */

export function RemoveServerModal({ server, onClose }: { server: McpServerRow | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [confirmName, setConfirmName] = useState('')

  const remove = useMutation({
    mutationFn: () => removeServer(server!.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['mcp'] })
      toast.success(`已移除 ${server!.name}`, {
        description: `${server!.adopted_count} 个纳管工具同步移除；动作写入审计日志（trace_id 落 admin 审计）`,
      })
      onClose()
    },
  })

  if (!server) return null

  // 在用 Agent 清单（mock 静态口径与画板 ix-mcp-03 一致：按已注入对应工具的 Agent 列）
  const affectedAgents = server.adopted_count > 0
    ? [
        { name: '停电分析助手', agent_id: 'agt_pwr_01', count: 2 },
        { name: '95598 抢修指挥助手', agent_id: 'agt_grid_07', count: 1 },
      ]
    : []

  const matched = confirmName === server.name

  return (
    <Modal
      open
      onClose={onClose}
      title="移除 MCP Server?"
      width={480}
      danger
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose} data-testid="mcp-remove-cancel">
            取消
          </button>
          <button
            type="button"
            className="btn btn-d btn-sm"
            data-testid="mcp-remove-confirm"
            disabled={!matched || remove.isPending}
            onClick={() => remove.mutate()}
          >
            <TrashIcon /> 确认移除
          </button>
        </>
      }
    >
      <div className="text-[11.5px] text-label-2">
        移除后连接与纳管关系即刻解除；动作写入审计日志（含操作者与 trace_id），历史调用记录保留可回放。
      </div>

      {/* 级联影响：工具注册中心 */}
      <div className="mt-3 rounded-xl border px-3.5 py-3" style={{ background: 'var(--red-soft)', borderColor: 'var(--red)' }}>
        <b className="text-[12px] text-red">工具注册中心 · {server.adopted_count} 项将移除</b>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {server.tools.filter(t => t.adopted).map(t => (
            <span key={t.tool_id} className="badge b-gray mono">{t.name}</span>
          ))}
          {server.adopted_count === 0 && <span className="text-[11px] text-label-3">无纳管工具。</span>}
        </div>
      </div>

      {/* 级联影响：在用 Agent */}
      {affectedAgents.length > 0 && (
        <div className="mt-2 rounded-xl border border-separator px-3.5 py-3" style={{ background: 'var(--surface)' }}>
          <b className="text-[12px]">
            <TriangleAlert size={12} className="mr-1 inline text-orange" aria-hidden />
            Agent 在用清单 · {affectedAgents.length} 个
          </b>
          <div className="mt-1.5 space-y-1.5">
            {affectedAgents.map(a => (
              <div key={a.agent_id} className="flex items-center gap-2 text-[11.5px]">
                <b>{a.name}</b>
                <span className="mono text-[10px] text-label-3">{a.agent_id}</span>
                <span className="text-label-3">· 注入 {a.count} 项工具</span>
                <span className="badge b-red ml-auto">移除后调用失败 2001</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 输入 Server 名确认 */}
      <div className="mt-3">
        <label className="field-label" htmlFor="mcp-remove-confirm-name">
          输入 Server 名以确认
        </label>
        <div className="flex items-center gap-2">
          <input
            id="mcp-remove-confirm-name"
            className="input"
            data-testid="mcp-remove-name"
            placeholder={server.name}
            value={confirmName}
            onChange={e => setConfirmName(e.target.value)}
          />
          {matched && <span className="badge b-green">已匹配</span>}
        </div>
        <div className="fhint">名称一致「确认移除」才可点击；建议先在影响清单中为在用 Agent 调整工具注入。</div>
      </div>
    </Modal>
  )
}

function TrashIcon() {
  return <Trash2 size={12} aria-hidden />
}

