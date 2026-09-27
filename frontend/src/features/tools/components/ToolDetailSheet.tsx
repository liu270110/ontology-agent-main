import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Copy } from 'lucide-react'
import { toast } from 'sonner'
import { Sheet } from '@/components/sheet'
import { getTool, TOOL_SOURCE_LABEL, type ToolRow } from '../api'

/** IX-TLS-01 工具详情抽屉（26 篇 §9.2；画板 ix-tls-01）：480px——
 *  输入/输出 Schema 只读预览（示例值折叠）+ 30 天调用统计 mini 条形
 *  + 来源徽标（内置/MCP/插件/业务 API）+「关联本体行动类」链接（反跳工作台聚焦该类，
 *  /ontology/:id?focus=）。数据 GET /tools/{id} + 统计（R 预登记）。 */

function SchemaPreview({ title, badge, schema }: { title: string; badge: string; schema?: Record<string, unknown> }) {
  const [open, setOpen] = useState(true)
  const props = (schema?.properties ?? {}) as Record<string, { type?: string; description?: string; enum?: string[] }>
  const required = (schema?.required ?? []) as string[]
  return (
    <div className="rounded-xl border border-separator" style={{ background: 'var(--surface)' }}>
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2" onClick={() => setOpen(o => !o)}>
        <b className="text-[12px]">{title}</b>
        <span className="badge b-gray">{badge}</span>
        <span className="ml-auto text-label-3">{open ? <ChevronDown size={13} aria-hidden /> : <ChevronRight size={13} aria-hidden />}</span>
      </button>
      {open && (
        <div className="hairline-t divide-y" style={{ borderColor: 'var(--separator)' }}>
          {Object.entries(props).map(([name, p]) => (
            <div key={name} className="flex gap-3 px-3 py-2 text-[11px]">
              <span className="mono w-[90px] flex-none font-semibold">{name}</span>
              <span className="w-[110px] flex-none text-label-3">
                {p.type ?? 'any'}
                {required.includes(name) && ' · 必填'}
                {p.enum ? ` · 枚举 ${p.enum.length}` : ''}
              </span>
              <span className="text-label-2">{p.description ?? ''}</span>
            </div>
          ))}
          {Object.keys(props).length === 0 && <div className="px-3 py-2 text-[11px] text-label-3">无属性声明。</div>}
        </div>
      )}
    </div>
  )
}

export function ToolDetailSheet({ tool, onClose }: { tool: ToolRow | null; onClose: () => void }) {
  const [exampleOpen, setExampleOpen] = useState(false)
  const { data: detail } = useQuery({
    queryKey: ['tools', 'detail', tool?.id],
    queryFn: () => getTool(tool!.id),
    enabled: !!tool,
  })

  if (!tool) return null
  const stats = detail?.stats

  return (
    <Sheet open onClose={onClose} title={tool.name} width={480}>
      <div className="px-5 py-4">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className={`badge ${tool.source === 'mcp' ? 'b-purple' : tool.source === 'plugin' ? 'b-blue' : tool.source === 'http' ? 'b-orange' : 'b-green'}`}>
            来源 · {TOOL_SOURCE_LABEL[tool.source]}
          </span>
          <span className="mono text-[10.5px] text-label-3">{tool.provider}</span>
          <span className={`badge ${tool.enabled ? 'b-green' : 'b-gray'} ml-auto`}>{tool.enabled ? '已启用' : '已停用'}</span>
        </div>

        <div className="mt-3 space-y-1.5 text-[11.5px]">
          <div className="flex justify-between gap-3"><span className="text-label-3">调用通道</span><span>平台网关（逐次审计 + trace_id）</span></div>
          <div className="flex justify-between gap-3"><span className="text-label-3">scope</span><span>{tool.scopes.length ? tool.scopes.join('、') : '—（无需特殊 scope）'}</span></div>
          {tool.danger && <div className="flex justify-between gap-3"><span className="text-label-3">风险</span><span className="badge b-red">高危 · 调用需高风险确认</span></div>}
        </div>

        <div className="mt-4 text-[11px] font-semibold text-label-3">Schema 只读预览</div>
        <div className="mt-2 space-y-2">
          <SchemaPreview title="输入 Schema" badge="JsonSchemaForm" schema={tool.input_schema} />
          {tool.output_schema && <SchemaPreview title="输出 Schema" badge="只读" schema={tool.output_schema} />}
          {tool.input_example && (
            <div className="rounded-xl border border-separator" style={{ background: 'var(--surface)' }}>
              <button type="button" className="flex w-full items-center gap-2 px-3 py-2" onClick={() => setExampleOpen(o => !o)}>
                <b className="text-[12px]">输入示例值</b>
                <span className="badge b-gray">{exampleOpen ? '已展开' : '已折叠'}</span>
                <span className="ml-auto text-label-3">{exampleOpen ? <ChevronDown size={13} aria-hidden /> : <ChevronRight size={13} aria-hidden />}</span>
              </button>
              {exampleOpen && (
                <pre className="hairline-t overflow-x-auto px-3 py-2 text-[10.5px] leading-4" style={{ fontFamily: 'var(--mono)' }}>
                  {JSON.stringify(tool.input_example)}
                </pre>
              )}
            </div>
          )}
        </div>

        {/* 30 天调用统计 mini 条形 */}
        <div className="mt-4">
          <div className="text-[11px] font-semibold text-label-3">调用统计 · 近 30 天</div>
          <div className="mt-2 space-y-1.5 text-[11px]">
            <StatBar label="调用次数" pct={100} value={stats ? stats.calls_30d.toLocaleString() : '—'} color="var(--accent)" />
            <StatBar label="成功率" pct={stats?.success_rate ?? 0} value={stats ? `${stats.success_rate}%` : '—'} color="var(--green)" />
            <StatBar label="平均耗时" pct={Math.min(100, (stats?.avg_ms ?? 0) / 10)} value={stats ? `${stats.avg_ms}ms` : '—'} color="var(--orange)" />
          </div>
          {/* mini 日调用量条形图 */}
          {stats && (
            <div className="mt-2 flex h-8 items-end gap-[3px]" aria-label="30 天日调用条形图" data-testid="tls-stats-bars">
              {stats.daily.map((v, i) => (
                <div
                  key={i}
                  className="flex-1 rounded-t-sm"
                  style={{ height: `${Math.max(8, (v / Math.max(...stats.daily)) * 100)}%`, background: 'var(--accent-soft)', borderTop: '2px solid var(--accent)' }}
                />
              ))}
            </div>
          )}
        </div>

        {/* 关联本体行动类（反跳工作台聚焦） */}
        {tool.action_class && (
          <Link
            to={`/ontology/${tool.action_class.onto_id}?focus=${tool.action_class.id}`}
            className="mt-4 flex items-center gap-2 rounded-xl px-3 py-2.5"
            style={{ background: 'var(--purple-soft)' }}
            data-testid="tls-action-class-link"
          >
            <span className="badge b-purple">本体行动类 {tool.action_class.id}</span>
            <span className="text-[11.5px] text-label-2">{tool.action_class.label}</span>
            <span className="mono ml-auto text-[10px] text-label-3">
              /ontology/{tool.action_class.onto_id}?focus={tool.action_class.id}
            </span>
          </Link>
        )}
        <div className="fhint">点击反跳本体工作台并聚焦该行动类（deep link 自动定位节点）。</div>

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
        </div>
      </div>
    </Sheet>
  )
}

function StatBar({ label, pct, value, color }: { label: string; pct: number; value: string; color: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-[52px] flex-none text-label-3">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full" style={{ background: 'var(--surface-2)' }}>
        <div className="h-full rounded-full" style={{ width: `${Math.min(100, pct)}%`, background: color }} />
      </div>
      <span className="w-[64px] flex-none text-right font-semibold">{value}</span>
    </div>
  )
}
