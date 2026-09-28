import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Check, GitBranch } from 'lucide-react'
import { KIND_LABEL, listToolRegistry, toolScopeLabel, type WfNode, type WfEdge, type WfAgentSlot, type WfToolRow } from '../api'

/** IX-GRP-07 节点参数检查器（右栏面板态）：八类类型化表单——Agent=绑插槽实例（继承群聊成员
 *  参数）/ 工具=注册表选取 + scope 徽标 / 知识检索=GraphRAG 三模式 / 条件=确定性表达式编辑器
 *  （mono + 模板 + 就地试算；禁裸 LLM 分支警示条，宪法 2）/ 审批=模板选择。表单底座按 29 篇
 *  降级预案：八类字段形态简单，直接 RHF 自建（.field/.input 令牌类），不引 RJSF。 */

/** 最终回退清单：工具下拉改拉 GET /tools 注册表（B3-P 转实）；仅当注册表请求失败时
 *  作为最后回退仍可选取（语境与 mocks/platform-handlers.ts 一致的 3 条），成功路径不使用。 */
const TOOL_REGISTRY_FALLBACK: WfToolRow[] = [
  { id: 'tool-scada-query', name: 'scada.query', desc: 'SCADA 遥测实时查询', source: 'http', scopes: [], danger: false, enabled: true },
  { id: 'tool-grid-write', name: 'grid.write', desc: '电网回写（高危 scope，运行期审计）', source: 'http', scopes: ['grid:write'], danger: true, enabled: true },
  { id: 'tool-kb-search', name: 'kb.search', desc: 'GraphRAG 知识检索', source: 'builtin', scopes: ['kb.read'], danger: false, enabled: true },
]

const EXPR_TEMPLATES = [
  { id: 'fault_count', name: '模板：故障计数比较', expr: 'nodes.fault.count > 3' },
  { id: 'load_ratio', name: '模板：负载率越限', expr: 'nodes.load.ratio >= 0.9' },
  { id: 'risk_gate', name: '模板：风险门限与', expr: 'nodes.fault.count > 3 && nodes.load.ratio >= 0.9' },
]

/** 确定性表达式试算（前端仅做语法/形态校验与演示试算；终审在后端表达式引擎，宪法 2）。
 *  sample 由表达式自身推导（如 nodes.fault.count>3 → nodes.fault.count=RHS+2），确定性、
 *  无任何 LLM 参与。 */
function evalExpr(expr: string, sample: Record<string, unknown>): { ok: boolean; value?: boolean; error?: string } {
  const normalized = expr.replace(/\s+/g, '')
  if (!normalized) return { ok: false, error: '表达式为空' }
  if (/[;{}=$]|function|eval|llm|prompt/i.test(normalized)) return { ok: false, error: '仅允许确定性比较与布尔组合' }
  try {
    const value = normalized.split('||').map(p => p.split('&&').every(cmp => evalCmp(cmp, sample))).reduce((a, b) => a || b)
    return { ok: true, value }
  } catch {
    return { ok: false, error: '形态应为 nodes.<x>.<y> <op> <数值>（支持 && / ||）' }
  }
}

function evalCmp(cmp: string, sample: Record<string, unknown>): boolean {
  const m = /^([a-zA-Z_][\w.]*)(>=|<=|==|!=|>|<)(-?\d+(?:\.\d+)?)$/.exec(cmp)
  if (!m) throw new Error('bad')
  let left: unknown = sample
  for (const k of m[1].split('.')) {
    if (left && typeof left === 'object' && k in (left as Record<string, unknown>)) left = (left as Record<string, unknown>)[k]
    else throw new Error('bad path')
  }
  if (typeof left !== 'number') throw new Error('not number')
  const right = Number(m[3])
  switch (m[2]) {
    case '>': return left > right
    case '<': return left < right
    case '>=': return left >= right
    case '<=': return left <= right
    case '==': return left === right
    default: return left !== right
  }
}

/** 从表达式提取嵌套 sample（nodes.fault.count > 3 → { nodes: { fault: { count: 5 } } }） */
function flattenSample(expr: string): Record<string, unknown> {
  const sample: Record<string, unknown> = {}
  const re = /nodes\.([\w.]+)\s*(?:>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)/g
  for (const m of expr.matchAll(re)) {
    const parts = m[1].split('.')
    let cur = sample
    parts.forEach((p, i) => {
      if (i === parts.length - 1) cur[p] = Number(m[2]) + 2
      else {
        cur[p] = (cur[p] as Record<string, unknown>) ?? {}
        cur = cur[p] as Record<string, unknown>
      }
    })
  }
  return sample
}

export function NodeInspector({
  node,
  edges,
  slots,
  onUpdate,
  onDelete,
}: {
  node: WfNode | null
  edges: WfEdge[]
  slots: WfAgentSlot[]
  onUpdate: (id: string, patch: Partial<WfNode>) => void
  onDelete: (id: string) => void
}) {
  if (!node) {
    return (
      <aside className="hidden w-[256px] flex-none overflow-y-auto border-l border-separator bg-surface p-3.5 xl:block" data-testid="wf-inspector">
        <h4 className="text-[13px] font-bold">检查器</h4>
        <div className="fhint mt-2">选中画布或节点库中的节点后，在此编辑类型化参数（八类表单）。</div>
      </aside>
    )
  }
  return <InspectorBody key={node.id} node={node} edges={edges} slots={slots} onUpdate={onUpdate} onDelete={onDelete} />
}

function InspectorBody({
  node,
  edges,
  slots,
  onUpdate,
  onDelete,
}: {
  node: WfNode
  edges: WfEdge[]
  slots: WfAgentSlot[]
  onUpdate: (id: string, patch: Partial<WfNode>) => void
  onDelete: (id: string) => void
}) {
  const params = node.params ?? {}
  const [expr, setExpr] = useState<string>(String(params.expression ?? ''))
  const [evalResult, setEvalResult] = useState<{ ok: boolean; value?: boolean; error?: string } | null>(null)

  // 工具注册表（B3-P 转实）：工具节点下拉改拉 GET /tools；失败回退 TOOL_REGISTRY_FALLBACK
  const toolsQuery = useQuery({ queryKey: ['tools', 'registry'], queryFn: listToolRegistry, enabled: node.kind === 'tool' })
  const registry = toolsQuery.data?.items ?? TOOL_REGISTRY_FALLBACK
  const curTool = String(params.tool ?? '')
  // 历史工作流引用已下架工具：当前值在注册表（含回退清单）中解析不到 → 下拉头部插入「（已下架）」保留原值不丢数据
  const curEntry = registry.find(t => t.name === curTool || t.id === curTool)
  const curDelisted = curTool !== '' && !curEntry

  const outgoing = useMemo(() => edges.filter(e => e.source === node.id), [edges, node.id])
  const patchParam = (patch: Record<string, unknown>) => onUpdate(node.id, { params: { ...params, ...patch } })

  return (
    <aside className="hidden w-[256px] flex-none overflow-y-auto border-l border-separator bg-surface p-3.5 xl:block" data-testid="wf-inspector">
      <h4 className="flex items-center gap-1.5 text-[13px] font-bold">
        {node.kind === 'condition' && <GitBranch size={14} style={{ color: 'var(--accent)' }} aria-hidden />}
        {KIND_LABEL[node.kind]}节点
      </h4>
      <div className="mono mt-0.5 text-[11px] text-label-3">node:{node.id} · 选中态</div>

      <div className="field mt-3">
        <label className="field-label">节点名称</label>
        <input className="input" style={{ height: 34 }} data-testid="wf-node-name" value={node.label} onChange={e => onUpdate(node.id, { label: e.target.value })} />
      </div>

      {node.kind === 'agent' && (
        <div className="field">
          <label className="field-label">绑定 Agent 插槽</label>
          <select className="input" data-testid="wf-param-slot" value={String(params.slot_id ?? '')} onChange={e => { patchParam({ slot_id: e.target.value }); onUpdate(node.id, { sub: e.target.value }) }}>
            <option value="">未绑定</option>
            {slots.map(s => (
              <option key={s.id} value={s.id}>{s.name} · {s.model}</option>
            ))}
          </select>
          <div className="fhint">继承群聊成员参数；编排不提权（运行期仍受原 Agent ACL 约束）。</div>
        </div>
      )}

      {node.kind === 'tool' && (
        <div className="field">
          <label className="field-label">注册表选取</label>
          <select
            className="input"
            data-testid="wf-param-tool"
            value={toolsQuery.isPending ? '__loading' : curTool}
            disabled={toolsQuery.isPending}
            onChange={e => {
              const v = e.target.value
              if (v === '__retry') { void toolsQuery.refetch(); return }
              const t = registry.find(x => x.name === v || x.id === v)
              patchParam({ tool: v })
              onUpdate(node.id, { sub: `scope: ${t ? toolScopeLabel(t) : 'read'}`, label: `工具 · ${v}` })
            }}
          >
            {toolsQuery.isPending ? (
              <option value="__loading">加载工具注册表…</option>
            ) : (
              <>
                {toolsQuery.isError ? (
                  <option value="__retry">加载失败，点击重试</option>
                ) : (
                  <option value="">未选择</option>
                )}
                {curDelisted && <option value={curTool}>{curTool}（已下架）</option>}
                {registry.map(t => (
                  <option key={t.id} value={t.name} title={t.desc}>
                    {t.name} · {toolScopeLabel(t)}
                  </option>
                ))}
              </>
            )}
          </select>
          {typeof params.tool === 'string' && params.tool && !toolsQuery.isPending && (
            <div className="mt-1.5">
              {curEntry ? (
                <div data-testid="wf-param-tool-scope">
                  <span className={`badge ${toolScopeLabel(curEntry) === 'high-risk' ? 'b-orange' : 'b-gray'}`}>
                    scope: {toolScopeLabel(curEntry)}
                  </span>
                </div>
              ) : (
                <div data-testid="wf-param-tool-delisted">
                  <span className="badge b-orange">已下架 · 历史引用 {params.tool}</span>
                </div>
              )}
            </div>
          )}
          {toolsQuery.isError && !toolsQuery.data && <div className="fhint">注册表暂不可用，以下为内置回退清单。</div>}
        </div>
      )}

      {node.kind === 'retrieval' && (
        <div className="field">
          <label className="field-label">GraphRAG 三模式</label>
          <select className="input" data-testid="wf-param-mode" value={String(params.mode ?? 'hybrid')} onChange={e => patchParam({ mode: e.target.value })}>
            <option value="local">local · 局部</option>
            <option value="global">global · 全局</option>
            <option value="hybrid">hybrid · 混合</option>
          </select>
          <div className="field mt-2 mb-0">
            <label className="field-label">top_k</label>
            <input className="input" style={{ height: 34 }} type="number" data-testid="wf-param-topk" value={Number(params.top_k ?? 6)} onChange={e => patchParam({ top_k: Number(e.target.value) })} />
          </div>
        </div>
      )}

      {node.kind === 'condition' && (
        <>
          <label className="field-label flex items-center gap-1.5">
            确定性表达式
            {evalResult?.ok && <span className="badge b-green ml-auto">校验通过</span>}
          </label>
          <div className="overflow-hidden rounded-[10px] border border-separator" style={{ background: 'var(--surface-2)' }}>
            <div className="flex items-center gap-2 border-b border-separator px-2.5 py-1.5 text-2xs text-label-3">
              <span className="mono">expr.editor · 仅确定性</span>
              <span className="mono ml-auto">草稿</span>
            </div>
            <textarea
              className="mono w-full resize-none bg-transparent px-3 py-2 text-xs leading-relaxed outline-none"
              style={{ minHeight: 56 }}
              rows={2}
              data-testid="wf-expr-input"
              aria-label="确定性表达式"
              value={expr}
              onChange={e => { setExpr(e.target.value); setEvalResult(null); patchParam({ expression: e.target.value }) }}
            />
          </div>
          <div className="mt-2 flex items-center gap-1.5">
            <select
              className="input h-[30px] flex-1 text-xs"
              aria-label="表达式模板"
              data-testid="wf-expr-template"
              value=""
              onChange={e => {
                const tpl = EXPR_TEMPLATES.find(t => t.id === e.target.value)
                if (tpl) { setExpr(tpl.expr); setEvalResult(null); patchParam({ expression: tpl.expr }) }
              }}
            >
              <option value="">选择模板…</option>
              {EXPR_TEMPLATES.map(t => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-s btn-sm flex-none"
              data-testid="wf-expr-eval"
              onClick={() => setEvalResult(evalExpr(expr, { nodes: flattenSample(expr) }))}
            >
              试算
            </button>
          </div>
          {evalResult && (
            <div
              className="mt-2 flex items-center gap-1.5 rounded-[9px] px-2.5 py-1.5 text-[11px]"
              data-testid="wf-expr-result"
              style={evalResult.ok ? { background: 'var(--green-soft)', color: 'var(--green)' } : { background: 'var(--red-soft)', color: 'var(--red)' }}
            >
              {evalResult.ok ? <Check size={12} className="flex-none" aria-hidden /> : <AlertTriangle size={12} className="flex-none" aria-hidden />}
              <span>
                {evalResult.ok
                  ? `试算通过 · 表达式确定性求值 → ${evalResult.value}`
                  : `试算失败 · ${evalResult.error}`}
              </span>
            </div>
          )}
          <div className="alert al-warn mt-2.5" data-testid="wf-llm-warning">
            <AlertTriangle size={15} aria-hidden />
            <div>
              <b>禁裸 LLM 分支</b>
              条件节点仅接受确定性表达式；需要语义路由时，用 Agent 节点输出 + 表达式判断（宪法 2 · 推理分级）。
            </div>
          </div>
          <div className="mt-2.5">
            {outgoing.map((e, i) => (
              <div key={e.target} className="kv flex items-center gap-2 py-0.5 text-[11px]">
                <span className="w-[76px] flex-none text-label-3">{e.label ?? (i === 0 ? '是 →' : '否 →')}</span>
                <span className="mono truncate text-label-2">{e.target}</span>
              </div>
            ))}
            <div className="kv flex items-center gap-2 py-0.5 text-[11px]">
              <span className="w-[76px] flex-none text-label-3">重试 / 超时</span>
              <span className="text-label-2">{Number(params.retry ?? 0)} 次 · {Number(params.timeout ?? 30)}s</span>
            </div>
          </div>
        </>
      )}

      {node.kind === 'approval' && (
        <div className="field">
          <label className="field-label">审批模板</label>
          <select className="input" data-testid="wf-param-approval" value={String(params.template ?? '')} onChange={e => patchParam({ template: e.target.value })}>
            <option value="">未选择</option>
            <option value="检修申请审批">检修申请审批</option>
            <option value="发布终审">发布终审</option>
          </select>
          <div className="fhint">运行期生成审批中心工单并等待回执（候选非成品，硬门禁）。</div>
        </div>
      )}

      {node.kind === 'parallel' && (
        <div className="fhint">汇聚节点：等待全部入边到达后放行（fan-in {edges.filter(e => e.target === node.id).length}）。</div>
      )}
      {node.kind === 'template' && (
        <div className="fhint">模板转换：变量映射（输出 ← 上游节点输出汇总），M1 内置研判意见模板。</div>
      )}
      {node.kind === 'start_end' && <div className="fhint">入口 / 出口节点，无参数。</div>}

      <div className="fhint mt-2">修改即写入草稿（顶部「保存」PUT /workflows/&#123;id&#125; 持久化）。</div>
      {!['start_end'].includes(node.kind) && (
        <button type="button" className="btn btn-d btn-sm mt-2 w-full" data-testid="wf-node-delete" onClick={() => onDelete(node.id)}>
          删除节点
        </button>
      )}
    </aside>
  )
}

