import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { removeServer, type McpServerRow } from '../api'

/** IX-MCP-03 移除 Server 确认（26 篇 §9.3；画板 ix-mcp-03）：danger 弹窗——
 *  级联影响（N 个纳管工具将从注册中心移除，数量取行内真实 adopted 工具集）
 *  + 输入 Server 名强制确认；动作写审计（trace_id 落 admin 审计日志）。
 *  真调 DELETE /mcp/servers/{id}：204 + X-Removed-Tools/X-Affected-Agents 提示头（级联
 *  工具行）；失败（未知 404 等）toast 透出结构化错误文案。
 *  诚实态：mock 时代的「在用 Agent 清单」为无数据源静态演示，随 live 收敛移除——
 *  受影响 Agent 数以后端提示头/审计为准，前端不再虚构。 */

export function RemoveServerModal({ server, onClose }: { server: McpServerRow | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [confirmName, setConfirmName] = useState('')

  const remove = useMutation({
    mutationFn: () => removeServer(server!.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['mcp'] })
      toast.success(`已移除 ${server!.name}`, {
        description: `${server!.adopted_count} 个纳管工具级联移除（X-Removed-Tools）；动作写入审计日志（trace_id 落 admin 审计）`,
      })
      onClose()
    },
    onError: e => {
      toast.error('Server 移除失败', { description: e instanceof Error ? e.message : undefined })
    },
  })

  if (!server) return null

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
      <div className="text-[11px] text-label-2">
        移除后连接与纳管关系即刻解除；动作写入审计日志（含操作者与 trace_id），历史调用记录保留可回放。
      </div>

      {/* 级联影响：工具注册中心（真实 adopted 工具集）；受影响 Agent 数以后端提示头/审计为准 */}
      <div className="mt-3 rounded-xl border px-3.5 py-3" style={{ background: 'var(--red-soft)', borderColor: 'var(--red)' }}>
        <b className="text-xs text-red">工具注册中心 · {server.adopted_count} 项将移除</b>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {server.tools.filter(t => t.adopted).map(t => (
            <span key={t.tool_id} className="badge b-gray mono">{t.name}</span>
          ))}
          {server.adopted_count === 0 && <span className="text-[11px] text-label-3">无纳管工具。</span>}
        </div>
        {server.adopted_count > 0 && (
          <div className="mt-1.5 text-[11px] text-label-2">
            注入以上工具的 Agent 将在移除后调用失败，请先在各 Agent 的工具配置中调整（受影响面见响应提示头 X-Affected-Agents 与审计）。
          </div>
        )}
      </div>

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

