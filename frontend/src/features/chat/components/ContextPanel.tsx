import { useState } from 'react'
import { ArrowRight, ChevronDown, ChevronUp, Sparkles } from 'lucide-react'
import { FloatingCard } from '@/components/popover'
import { useSessionStore } from '@/stores/session-store'
import type { EvidenceChunk } from '@/stores/session-store'
import type { RunUsageEventData } from '@/sse/events'
import type { EvidenceFocus } from './EvidenceSheet'

/** 上下文面板（IX-CHT-04，右栏 240 可折叠）：本次回答的四类来源——
 *  召回记忆（L1/L2/L3）· GraphRAG 路径 · 规则命中（推理分级）· 引用文档；
 *  条目点击 → 溯源 Popover 320（类型徽标 + 摘要 + 命中得分 + 查看全部/跳转）。
 *  F-03（41 号验收 2026-10-05）：数据源=会话内 `run.usage` 真帧（session-store.usageGroups）
 *  ——无帧时渲染空态占位，不再回退演示假数据（mock 时代 CTX_GROUPS 已删，候选非成品/真数据）。 */

type CtxKind = 'memory' | 'graph' | 'rule' | 'doc'

interface CtxItem {
  id: string
  kind: CtxKind
  /** 类型徽标文案（IX-CHT-04：L1/L2/L3 记忆 · GraphRAG 路径 · 规则命中） */
  badge: string
  badgeCls: string
  dotCls: string
  label: string
  summary: string
  score: number
  meta: [string, string][]
  /** 引用文档类可下钻证据抽屉（IX-CHT-03） */
  chunk?: EvidenceChunk
}

const BADGE_LEGEND: { t: string; cls?: string; style?: React.CSSProperties }[] = [
  { t: 'L1 工作记忆', cls: 'b-gray' },
  { t: 'L2 用户', cls: 'b-orange' },
  { t: 'L3 组织', cls: 'b-purple' },
  { t: 'GraphRAG 路径', style: { background: 'var(--teal-soft)', color: 'var(--teal)' } },
  { t: '规则命中', cls: 'b-green' },
]

// ---- run.usage 真数据 → 面板渲染模型（徽标映射：L1/L2/L3→灰/橙/紫 · Local/Global/Drift→teal · SHACL→绿 · LLM→橙 · 文档→蓝） ----

type UsageGroups = RunUsageEventData['groups']

const MEMORY_BADGE: Record<UsageGroups['memory'][number]['layer'], { badge: string; badgeCls: string; dotCls: string; label: string; layerMeta: string }> = {
  L1: { badge: 'L1 记忆 · 工作', badgeCls: 'b-gray', dotCls: 'bg-separator', label: 'L1 工作', layerMeta: 'L1 · 工作记忆（会话内）' },
  L2: { badge: 'L2 记忆 · 用户', badgeCls: 'b-orange', dotCls: 'bg-orange', label: 'L2 用户', layerMeta: 'L2 · 用户画像（已审核）' },
  L3: { badge: 'L3 记忆 · 组织', badgeCls: 'b-purple', dotCls: 'bg-purple', label: 'L3 组织', layerMeta: 'L3 · 组织知识（已审核）' },
}

const GRAPH_MODE_DESC: Record<UsageGroups['graph'][number]['mode'], string> = {
  Local: 'Local（实体邻域扩展）',
  Global: 'Global（社区摘要全局）',
  Drift: 'Drift（漂移检测）',
}

/** run.usage 四分组 → 分组渲染树；空分组整组隐藏（P-006：演示回退已删，无帧只走空态占位） */
export function usageToCtxGroups(u: UsageGroups): { title: string; items: CtxItem[] }[] {
  const out: { title: string; items: CtxItem[] }[] = []
  if (u.memory.length) {
    out.push({
      title: '召回记忆',
      items: u.memory.map(m => {
        const b = MEMORY_BADGE[m.layer]
        const meta: [string, string][] = [['层级', b.layerMeta]]
        if (m.updated_at) meta.push(['最近更新', m.updated_at])
        if (m.reused != null) meta.push(['复用次数', `${m.reused} 次`])
        return { id: m.id, kind: 'memory', badge: b.badge, badgeCls: b.badgeCls, dotCls: b.dotCls, label: b.label, summary: m.summary, score: m.score, meta }
      }),
    })
  }
  if (u.graph.length) {
    out.push({
      title: 'GraphRAG 路径',
      items: u.graph.map(g => ({
        id: g.id, kind: 'graph', badge: `GraphRAG · ${g.mode}`, badgeCls: 'badge-teal', dotCls: 'bg-teal',
        label: `${g.mode} Search`,
        summary: `${g.entities} 实体 / ${g.relations} 关系 / ${g.communities} 社区摘要`,
        score: g.score,
        meta: [['检索模式', GRAPH_MODE_DESC[g.mode]], ['耗时', `${g.latency_ms}ms · knowledge.search`]] as [string, string][],
      })),
    })
  }
  if (u.rules.length) {
    out.push({
      title: '规则命中',
      items: u.rules.map(r => {
        const shacl = r.kind === 'SHACL'
        const meta: [string, string][] = [['推理分级', shacl ? '确定性高频逻辑 → 规则引擎（宪法 2）' : '低频语义判断 → LLM（宪法 2）']]
        if (r.constraint) meta.push(['约束', r.constraint])
        if (!shacl) meta.push(['校验', '输出过 SHACL 后才入答案（候选非成品）'])
        return {
          id: r.id, kind: 'rule', badge: shacl ? '规则命中 · SHACL' : 'LLM 判定', badgeCls: shacl ? 'b-green' : 'b-orange',
          dotCls: shacl ? 'bg-green' : 'bg-orange', label: shacl ? '规则命中' : 'LLM 判定', summary: r.summary, score: r.score, meta,
        }
      }),
    })
  }
  if (u.docs.length) {
    out.push({
      title: '引用文档',
      items: u.docs.map(d => ({
        id: d.id, kind: 'doc', badge: '引用文档', badgeCls: 'b-blue', dotCls: 'bg-accent',
        label: d.label, summary: d.summary, score: d.score, chunk: d.chunk,
        meta: [['来源文档', d.chunk.doc_id], ['分片', `${d.chunk.chunk_id}${d.chunk.page != null ? ` · 第 ${d.chunk.page} 页` : ''}`], ['命中得分', d.score.toFixed(2)]] as [string, string][],
      })),
    })
  }
  return out
}

/** 溯源 Popover 内容（IX-CHT-04：类型徽标 + 摘要 + 得分 + 元数据 + 徽标图例 + 动作） */
function TracePopover({
  item,
  anchor,
  onClose,
  onOpenEvidence,
}: {
  item: CtxItem
  anchor: DOMRect | null
  onClose: () => void
  onOpenEvidence: (f: EvidenceFocus) => void
}) {
  return (
    <FloatingCard open anchor={anchor} onClose={onClose} width={320}>
      <div className="p-title flex items-center gap-2 text-[13px] font-bold">
        <span className={`badge ${item.badgeCls}`} style={item.badgeCls === 'badge-teal' ? { background: 'var(--teal-soft)', color: 'var(--teal)' } : undefined}>
          {item.badge}
        </span>
        <span className="mono ml-auto text-2xs text-label-3">{item.id}</span>
      </div>
      <p className="p-body mt-1 text-xs leading-6 text-label-2">{item.summary}</p>
      <dl className="mt-2.5 text-[11px]">
        <div className="flex justify-between border-b border-separator py-1">
          <dt className="text-label-3">命中得分</dt>
          <dd className="mono">{item.score.toFixed(2)}</dd>
        </div>
        {item.meta.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-3 border-b border-separator py-1 last:border-b-0">
            <dt className="flex-none text-label-3">{k}</dt>
            <dd className="text-right text-label-2">{v}</dd>
          </div>
        ))}
      </dl>
      <div className="mt-2.5 flex flex-wrap gap-1 border-t border-separator pt-2.5">
        {BADGE_LEGEND.map(b => (
          <span key={b.t} className={`badge text-2xs ${b.cls ?? ''}`} style={b.style}>{b.t}</span>
        ))}
      </div>
      <div className="p-acts mt-2.5 flex justify-end gap-1.5">
        <button type="button" className="btn btn-g btn-sm" onClick={onClose}>查看全部</button>
        {item.kind === 'memory' && (
          <a className="btn btn-s btn-sm" href="/memory?fact=" onClick={onClose}>
            转记忆管理 <ArrowRight size={12} aria-hidden />
          </a>
        )}
        {(item.kind === 'graph' || item.kind === 'doc') && item.chunk && (
          <button
            type="button"
            className="btn btn-s btn-sm"
            onClick={() => {
              onClose()
              onOpenEvidence({ chunk: item.chunk!, graph_paths: item.kind === 'doc' ? [] : [{ nodes: ['OutageEvent', 'Feeder'], edges: ['locatedOn'] }] })
            }}
          >
            打开证据原文 <ArrowRight size={12} aria-hidden />
          </button>
        )}
      </div>
    </FloatingCard>
  )
}

export function ContextPanel({ onOpenEvidence }: { onOpenEvidence: (f: EvidenceFocus) => void }) {
  const [trace, setTrace] = useState<{ item: CtxItem; anchor: DOMRect } | null>(null)
  // F-03：run.usage 真数据源（会话级）——无帧时空态占位（演示回退已删，见文件头注）
  const usageGroups = useSessionStore(s => s.usageGroups)
  const groups = usageGroups ? usageToCtxGroups(usageGroups) : []

  return (
    <aside data-testid="ctx-panel" className="flex w-60 flex-none flex-col border-l border-separator bg-surface">
      <div className="flex items-center gap-1.5 px-3.5 pb-1 pt-3">
        <Sparkles size={13} className="text-accent" aria-hidden />
        <b className="text-xs">本次回答的上下文</b>
      </div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-3.5 pb-3">
        {groups.length === 0 ? (
          <div
            data-testid="ctx-panel-empty"
            className="mt-3 rounded-lg border border-dashed border-separator px-3 py-4 text-[11px] leading-5 text-label-3"
          >
            {/* P-006 假数据收敛（2026-10-07）：无 run.usage 帧只显空态语义 + 协议注记，不造演示数据 */}
            <div className="font-semibold text-label-2">本 Run 无用量数据</div>
            <p className="mt-1">完成一轮回答后，此处展示本次回答召回的记忆、GraphRAG 路径与引用文档。</p>
            <p className="mt-1">协议注记：run.usage 帧随 B7 后端批转正后自动点亮。</p>
          </div>
        ) : (
          groups.map(g => (
          <section key={g.title} className="mt-2.5">
            <div className="text-[11px] font-semibold text-label-3">{g.title}</div>
            <div className="mt-1 flex flex-col gap-1">
              {g.items.map(it => (
                <button
                  key={it.id}
                  type="button"
                  data-testid={`ctx-item-${it.id}`}
                  onClick={e => setTrace({ item: it, anchor: (e.currentTarget as HTMLElement).getBoundingClientRect() })}
                  className={`flex items-start gap-1.5 rounded-lg px-2 py-1.5 text-left text-[11px] leading-5 hover:bg-surface-2 ${
                    trace?.item.id === it.id ? 'bg-accent-soft' : ''
                  }`}
                >
                  <span className={`mt-[5px] h-1.5 w-1.5 flex-none rounded-full ${it.dotCls}`} aria-hidden />
                  <span className="min-w-0">
                    <b>{it.label}</b>
                    <span className="text-label-2"> · {it.summary}</span>
                  </span>
                </button>
              ))}
            </div>
          </section>
          ))
        )}
      </div>
      {trace && (
        <TracePopover item={trace.item} anchor={trace.anchor} onClose={() => setTrace(null)} onOpenEvidence={onOpenEvidence} />
      )}
    </aside>
  )
}

/** 折叠轨（面板收起后的 24px 竖条：只留展开钮，IX-CHT-04 宿主可折叠） */
export function ContextPanelRail({ onExpand }: { onExpand: () => void }) {
  return (
    <button
      type="button"
      data-testid="ctx-panel-expand"
      aria-label="展开上下文面板"
      onClick={onExpand}
      className="flex w-7 flex-none items-center justify-center border-l border-separator bg-surface text-label-3 hover:text-accent"
    >
      <ChevronDown size={14} className="-rotate-90" aria-hidden />
    </button>
  )
}

/** 面板头部折叠钮（收起方向图标随状态翻转） */
export function ContextCollapseButton({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      data-testid="ctx-panel-toggle"
      aria-label={collapsed ? '展开上下文面板' : '折叠上下文面板'}
      title={collapsed ? '展开上下文面板' : '折叠上下文面板'}
      onClick={onToggle}
      className="flex h-7 w-7 items-center justify-center rounded-lg text-label-3 hover:bg-surface-2 hover:text-label"
    >
      {collapsed ? <ChevronUp size={14} aria-hidden /> : <ChevronDown size={14} className="rotate-90" aria-hidden />}
    </button>
  )
}
