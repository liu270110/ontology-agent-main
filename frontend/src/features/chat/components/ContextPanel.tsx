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
 *  数据源：会话内收到 `run.usage` 帧后按真事件渲染（session-store.usageGroups）；
 *  无帧时退回下方 CTX_GROUPS 演示数据——M4 后 run.usage 随每轮回答常在，此回退仅兜底首帧前/异常态。 */

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

/** 演示回退数据（与 mock 种子同源）：收到 run.usage 帧前/异常时兜底渲染。
 *  M4 后真事件常在——每一轮回答完成前后端都会推 run.usage，此表仅保留作降级展示。 */
const CTX_GROUPS: { title: string; items: CtxItem[] }[] = [
  {
    title: '召回记忆',
    items: [
      {
        id: 'fact_0287', kind: 'memory', badge: 'L2 记忆 · 用户', badgeCls: 'b-orange', dotCls: 'bg-orange',
        label: 'L2 用户', summary: '城区配网抢修视角，偏好设备→馈线→用户顺序',
        score: 0.87,
        meta: [['层级', 'L2 · 用户画像（已审核）'], ['最近更新', '3 天前 · 由会话「馈线 F12 故障研判」沉淀'], ['复用次数', '6 次']],
      },
      {
        id: 'fact_0104', kind: 'memory', badge: 'L3 记忆 · 组织', badgeCls: 'b-purple', dotCls: 'bg-purple',
        label: 'L3 组织', summary: '团队正做配网停电本体试点（v1.4）',
        score: 0.74,
        meta: [['层级', 'L3 · 组织知识（已审核）'], ['最近更新', '1 周前 · 治理后台批量入库'], ['复用次数', '12 次']],
      },
    ],
  },
  {
    title: 'GraphRAG 路径',
    items: [
      {
        id: 'path_local', kind: 'graph', badge: 'GraphRAG · Local', badgeCls: 'badge-teal', dotCls: 'bg-teal',
        label: 'Local Search', summary: '6 实体 / 4 关系 / 2 社区摘要',
        score: 0.91,
        meta: [['检索模式', 'Local（实体邻域扩展）'], ['耗时', '612ms · knowledge.search'], ['降级', '否（向量+术语双通道）']],
      },
    ],
  },
  {
    title: '规则命中',
    items: [
      {
        id: 'rule_cl014', kind: 'rule', badge: '规则命中 · SHACL', badgeCls: 'b-green', dotCls: 'bg-green',
        label: '规则命中', summary: '停役联络 = 短时倒供（唯一解）',
        score: 1.0,
        meta: [['推理分级', '确定性高频逻辑 → 规则引擎（宪法 2）'], ['约束', 'CL-014 · 3 条 SHACL 全通过']],
      },
      {
        id: 'llm_judge', kind: 'rule', badge: 'LLM 判定', badgeCls: 'b-orange', dotCls: 'bg-orange',
        label: 'LLM 判定', summary: '影响归纳（输出已过 SHACL 校验）',
        score: 0.88,
        meta: [['推理分级', '低频语义判断 → LLM（宪法 2）'], ['校验', '输出过 SHACL 后才入答案（候选非成品）']],
      },
    ],
  },
  {
    title: '引用文档',
    items: [
      {
        id: 'doc_gf042', kind: 'doc', badge: '引用文档', badgeCls: 'b-blue', dotCls: 'bg-accent',
        label: '配网检修规程 §4.2', summary: '10kV 馈线停役挂牌与恢复送电条款',
        score: 0.92,
        meta: [['来源文档', '配网检修规程（2024 修订）'], ['分片', 'chunk_042 · 第 3 页'], ['入库', 'JOB #217 · 已终审']],
        chunk: {
          doc_id: '配网检修规程（2024 修订）', chunk_id: 'chunk_042', page: 3, score: 0.92, entity: 'power-ont#馈线F12',
          quote: '其 10kV 馈线 F12 应转入检修状态，并在操作把手上悬挂「禁止合闸，线路有人工作」标示牌；恢复送电前应核对接地线已全部拆除。',
          highlight: '10kV 馈线 F12 应转入检修状态',
        },
      },
      {
        id: 'doc_gbt', kind: 'doc', badge: '引用文档', badgeCls: 'b-blue', dotCls: 'bg-accent',
        label: 'GB/T 36276 · 循环寿命', summary: '1000 次循环后容量保持率 ≥80%',
        score: 0.83,
        meta: [['来源文档', 'GB/T 36276'], ['分片', 'chunk_017 · 第 3 页'], ['入库', 'JOB #198 · 已终审']],
        chunk: {
          doc_id: 'GB/T 36276', chunk_id: 'chunk_017', page: 3, score: 0.83, entity: 'power-ont#电池簇',
          quote: '电池簇经 1000 次循环后容量保持率应不低于 80%，且不应出现漏液、外壳破裂等异常。',
          highlight: '1000 次循环后容量保持率应不低于 80%',
        },
      },
    ],
  },
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

/** run.usage 四分组 → 分组渲染树；空分组整组隐藏，与演示回退同构 */
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
  // run.usage 真数据源（会话级）：有帧按帧渲染，无帧退回演示回退（见 CTX_GROUPS 注）
  const usageGroups = useSessionStore(s => s.usageGroups)
  const groups = usageGroups ? usageToCtxGroups(usageGroups) : CTX_GROUPS

  return (
    <aside data-testid="ctx-panel" className="flex w-60 flex-none flex-col border-l border-separator bg-surface">
      <div className="flex items-center gap-1.5 px-3.5 pb-1 pt-3">
        <Sparkles size={13} className="text-accent" aria-hidden />
        <b className="text-xs">本次回答的上下文</b>
      </div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-3.5 pb-3">
        {groups.map(g => (
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
        ))}
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
