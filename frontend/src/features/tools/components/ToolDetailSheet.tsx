import { useState } from 'react'
import { ChevronDown, ChevronRight, Copy } from 'lucide-react'
import { toast } from 'sonner'
import { Sheet } from '@/components/sheet'
import { TOOL_CHANNEL_LABEL, TOOL_STATUS_LABEL, type ToolRow } from '../api'

/** IX-TLS-01 工具详情抽屉（26 篇 §9.2；画板 ix-tls-01）：480px——S1 ToolOut 同构详情：
 *  语义标注只读预览（semantic_annotation 键值）+ 行动类 IRI（本体对账键）/来源通道
 *  L0~L3/版本/健康提示/证据 URI。
 *  诚实态（S1 契约收敛，2026-10-05）：mock 时代的 30 天调用统计与输入/输出 Schema 预览
 *  无对应端点（S1 详情=ToolOut 同构，无 stats/schema 字段），面板移除不造假；后续统计
 *  端点登记（api/01 §5.6 ★）后再恢复。 */

function AnnotationPreview({ annotation }: { annotation: Record<string, unknown> }) {
  const [open, setOpen] = useState(true)
  const entries = Object.entries(annotation)
  return (
    <div className="rounded-xl border border-separator" style={{ background: 'var(--surface)' }}>
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2" onClick={() => setOpen(o => !o)}>
        <b className="text-xs">语义标注</b>
        <span className="badge b-gray">semantic_annotation</span>
        <span className="ml-auto text-label-3">{open ? <ChevronDown size={13} aria-hidden /> : <ChevronRight size={13} aria-hidden />}</span>
      </button>
      {open && (
        <div className="hairline-t divide-y" style={{ borderColor: 'var(--separator)' }}>
          {entries.map(([name, value]) => (
            <div key={name} className="flex gap-3 px-3 py-2 text-[11px]">
              <span className="mono w-[90px] flex-none font-semibold">{name}</span>
              <span className="min-w-0 break-all text-label-2">{typeof value === 'string' ? value : JSON.stringify(value)}</span>
            </div>
          ))}
          {entries.length === 0 && <div className="px-3 py-2 text-[11px] text-label-3">空标注（注册清单校验不允许，防御展示）。</div>}
        </div>
      )}
    </div>
  )
}

export function ToolDetailSheet({ tool, onClose }: { tool: ToolRow | null; onClose: () => void }) {
  if (!tool) return null

  return (
    <Sheet open onClose={onClose} title={tool.name} width={480}>
      <div className="px-5 py-4">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="badge b-purple">来源 · {TOOL_CHANNEL_LABEL[tool.source_channel]}</span>
          <span className={`badge ${tool.status === 'listed' ? 'b-green' : tool.status === 'revoked' ? 'b-red' : 'b-gray'} ml-auto`}>
            {TOOL_STATUS_LABEL[tool.status]}
          </span>
        </div>

        <div className="mt-3 space-y-1.5 text-[11px]">
          <div className="flex justify-between gap-3"><span className="text-label-3">版本</span><span className="mono">{tool.version}</span></div>
          <div className="flex justify-between gap-3">
            <span className="text-label-3">行动类 IRI</span>
            <span className="mono max-w-[300px] truncate" title={tool.action_iri}>{tool.action_iri}</span>
          </div>
          <div className="flex justify-between gap-3"><span className="text-label-3">健康提示</span><span>{tool.health_hint ?? '—'}</span></div>
          <div className="flex justify-between gap-3">
            <span className="text-label-3">证据 URI</span>
            <span className="mono max-w-[300px] truncate" title={tool.evidence_uri ?? undefined}>{tool.evidence_uri ?? '—'}</span>
          </div>
        </div>

        {/* 语义标注只读预览（S1 详情同构；无 schema/统计端点——不造假面板） */}
        <div className="mt-4">
          <div className="text-[11px] font-semibold text-label-3">语义标注（对齐 kernel ExtensionMeta 口径）</div>
          <div className="mt-2">
            <AnnotationPreview annotation={tool.semantic_annotation} />
          </div>
        </div>

        <div className="fhint">
          S1 详情=ToolOut 同构（services/tools/api/schemas/tool.py）：无统计/Schema 端点，
          统计面板待 api/01 §5.6 登记后恢复。
        </div>

        <div className="mt-3 flex gap-2">
          <button
            type="button"
            className="btn btn-g btn-sm"
            onClick={() => {
              void navigator.clipboard?.writeText(tool.name).catch(() => {})
              toast.success(`已复制工具名 ${tool.name}`)
            }}
          >
            <Copy size={12} aria-hidden /> 复制工具名
          </button>
          <button
            type="button"
            className="btn btn-g btn-sm"
            onClick={() => {
              void navigator.clipboard?.writeText(tool.action_iri).catch(() => {})
              toast.success(`已复制行动类 IRI ${tool.action_iri}`)
            }}
          >
            <Copy size={12} aria-hidden /> 复制 IRI
          </button>
        </div>
      </div>
    </Sheet>
  )
}
