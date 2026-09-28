import { useEffect, useRef, useState } from 'react'
import { Blocks, Bot, ChevronsLeft, GitBranch, Merge, Play, Plus, Search, Shield, Shuffle, Wrench } from 'lucide-react'
import { NODE_KINDS, type WfNodeKind } from '../api'

/** NodePalette —— 节点库悬浮薄栏（B5-C 画布布局切片，Dify 工作流编辑器布局语言）。
 *
 *  收起态：左侧 48px 玻璃竖栏（absolute left-3 top-3，悬浮于全幅画布之上），八类节点
 *  图标竖排（lucide 映射与旧节点库一致），当前过滤类型高亮；底部「+」提示可展开。
 *  展开态：点击栏/任意图标 → 280px 玻璃面板（分类分组 + 名称 + 一句话描述 + 点击加入
 *  画布——沿用页面 addNode 调用路径）；面板顶部 = 原「八类节点」Tab 过滤 chips
 *  （功能等价迁移，testid wf-tab-* 保留）；Esc / 点外部 / 再次点击收起。
 *  不占常驻宽度：画布在收起/展开两态下均保持全幅（旧 180/240px 常驻左栏删除）。 */

export const KIND_ICON: Record<WfNodeKind, React.ReactNode> = {
  start_end: <Play size={14} aria-hidden />,
  agent: <Bot size={14} aria-hidden />,
  tool: <Wrench size={14} aria-hidden />,
  retrieval: <Search size={14} aria-hidden />,
  condition: <GitBranch size={14} aria-hidden />,
  parallel: <Merge size={14} aria-hidden />,
  approval: <Shield size={14} aria-hidden />,
  template: <Shuffle size={14} aria-hidden />,
}

/** 分类分组（展开面板两级结构，Dify 同款「分类 → 条目」） */
const KIND_GROUPS: { group: string; kinds: WfNodeKind[] }[] = [
  { group: '基础', kinds: ['start_end'] },
  { group: '智能执行', kinds: ['agent', 'tool', 'retrieval'] },
  { group: '流程控制', kinds: ['condition', 'parallel'] },
  { group: '治理与转换', kinds: ['approval', 'template'] },
]

/** 一句话描述（展开面板条目副文案） */
const KIND_DESC: Record<WfNodeKind, string> = {
  start_end: '定义流程入口 / 出口',
  agent: '绑定 Agent 插槽执行研判，继承群聊成员参数（编排不提权）',
  tool: '调用工具注册表能力，高危 scope 运行期审计',
  retrieval: 'GraphRAG 知识检索（local / global / hybrid）',
  condition: '确定性表达式路由 · 禁裸 LLM 分支（宪法 2）',
  parallel: '汇聚节点：等待全部入边到达后放行',
  approval: '运行期生成审批工单，人工终审硬门禁',
  template: '变量映射汇总上游输出（研判意见模板）',
}

export interface NodePaletteProps {
  onAdd: (kind: WfNodeKind) => void
  kindFilter: WfNodeKind | null
  onFilterChange: (kind: WfNodeKind | null) => void
}

export function NodePalette({ onAdd, kindFilter, onFilterChange }: NodePaletteProps) {
  const [expanded, setExpanded] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  // 展开时：Esc / 点外部收起（点外部不吞事件——mousedown 只收面板，画布照常响应该次点击）
  useEffect(() => {
    if (!expanded) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setExpanded(false)
    }
    const onDown = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) setExpanded(false)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('mousedown', onDown)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mousedown', onDown)
    }
  }, [expanded])

  // ---- 收起态：48px 玻璃薄栏 ----
  if (!expanded) {
    return (
      <div
        className="glass absolute left-3 top-3 z-20 flex w-12 flex-col items-center gap-0.5 rounded-xl p-1"
        style={{ boxShadow: 'var(--sh-float)' }}
        data-testid="wf-library"
      >
        <button
          type="button"
          className="flex h-9 w-9 flex-none items-center justify-center rounded-lg text-accent hover:bg-black/5 dark:hover:bg-white/[.07]"
          title="展开节点库"
          aria-label="展开节点库"
          data-testid="wf-palette-open"
          onClick={() => setExpanded(true)}
        >
          <Blocks size={16} aria-hidden />
        </button>
        <div className="my-0.5 h-px w-6 flex-none bg-separator" aria-hidden />
        {NODE_KINDS.map(k => {
          const active = kindFilter === k.kind
          return (
            <button
              key={k.kind}
              type="button"
              className="flex h-8 w-9 flex-none items-center justify-center rounded-lg transition-colors hover:bg-black/5 dark:hover:bg-white/[.07]"
              style={active ? { background: 'var(--accent-soft)', color: 'var(--accent)' } : { color: 'var(--label-2)' }}
              title={`${k.label} · 点击展开节点库`}
              aria-label={`节点库 · ${k.label}`}
              aria-pressed={active}
              data-testid={`wf-rail-${k.kind}`}
              onClick={() => setExpanded(true)}
            >
              {KIND_ICON[k.kind]}
            </button>
          )
        })}
        <div className="mt-0.5 flex h-7 w-9 flex-none items-center justify-center text-label-3" title="点击图标展开节点库添加" aria-hidden>
          <Plus size={14} />
        </div>
      </div>
    )
  }

  // ---- 展开态：280px 玻璃面板（过滤 chips + 分类分组条目） ----
  return (
    <div
      ref={panelRef}
      className="glass absolute left-3 top-3 z-20 flex max-h-[calc(100%-24px)] w-[280px] flex-col overflow-hidden rounded-xl"
      style={{ boxShadow: 'var(--sh-float)' }}
      data-testid="wf-palette-panel"
    >
      {/* 面板头 */}
      <div className="flex flex-none items-center gap-1.5 border-b border-separator px-3 py-2.5">
        <Blocks size={14} style={{ color: 'var(--accent)' }} aria-hidden />
        <b className="text-[13px]">节点库</b>
        <span className="ml-auto text-2xs text-label-3">点击条目加入画布</span>
        <button
          type="button"
          className="flex h-6 w-6 flex-none items-center justify-center rounded-lg text-label-3 hover:bg-black/5 dark:hover:bg-white/[.07]"
          title="收起（Esc）"
          aria-label="收起节点库"
          data-testid="wf-palette-close"
          onClick={() => setExpanded(false)}
        >
          <ChevronsLeft size={14} aria-hidden />
        </button>
      </div>

      {/* 八类节点过滤 chips（原第二行 Tab 功能等价迁移；点击已选中 chip 取消过滤） */}
      <div className="flex flex-none flex-wrap items-center gap-1 border-b border-separator px-2.5 py-2" data-testid="wf-kind-tabs">
        <span className="mr-0.5 text-2xs font-bold tracking-wide text-label-3">八类节点</span>
        {NODE_KINDS.map(k => (
          <button
            key={k.kind}
            type="button"
            className="rounded-full px-2 py-0.5 text-[11px] transition-colors"
            style={
              kindFilter === k.kind
                ? { color: 'var(--accent)', border: '1.5px solid var(--accent)', background: 'var(--accent-soft)', fontWeight: 700 }
                : { color: 'var(--label-2)', border: '1px solid var(--separator)', background: 'var(--surface)' }
            }
            data-testid={`wf-tab-${k.kind}`}
            onClick={() => onFilterChange(kindFilter === k.kind ? null : k.kind)}
          >
            {k.label}
          </button>
        ))}
      </div>

      {/* 分类分组条目（按当前过滤过滤；组内无匹配整组隐藏） */}
      <div className="min-h-0 flex-1 overflow-y-auto px-1.5 py-1.5">
        {KIND_GROUPS.map(g => {
          const items = NODE_KINDS.filter(k => g.kinds.includes(k.kind) && (!kindFilter || k.kind === kindFilter))
          if (items.length === 0) return null
          return (
            <div key={g.group} className="mb-1">
              <div className="px-2 pb-0.5 pt-1.5 text-2xs font-bold tracking-wide text-label-3">{g.group}</div>
              {items.map(k => (
                <button
                  key={k.kind}
                  type="button"
                  className="flex w-full items-start gap-2.5 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-black/[.04] dark:hover:bg-white/[.06]"
                  data-testid={`wf-add-${k.kind}`}
                  onClick={() => onAdd(k.kind)}
                >
                  <span
                    className="mt-0.5 flex h-7 w-7 flex-none items-center justify-center rounded-lg"
                    style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}
                    aria-hidden
                  >
                    {KIND_ICON[k.kind]}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5 text-xs font-medium text-label">
                      {k.label}
                      {k.badge && (
                        <span className="badge b-orange" style={{ fontSize: 8.5, padding: '0 5px' }}>
                          {k.badge}
                        </span>
                      )}
                    </span>
                    <span className="mt-0.5 block text-[11px] leading-4 text-label-3">{KIND_DESC[k.kind]}</span>
                  </span>
                  <Plus size={12} className="mt-2 flex-none text-label-3" aria-hidden />
                </button>
              ))}
            </div>
          )
        })}
      </div>

      {/* 面板脚注（原左栏提示迁入；循环子图 v2 另议） */}
      <div className="flex-none border-t border-separator px-3 py-2">
        <div className="fhint">Agent 节点绑插槽实例并继承群聊成员参数；编排不提权（ACL 约束）。循环子图 v2 另议。</div>
      </div>
    </div>
  )
}
