import { useState } from 'react'
import { AlertTriangle, ChevronDown, RefreshCw, Sparkles } from 'lucide-react'
import type { ValidateReport } from '../api'
import { ChangeCountChips } from './shared'

/** 底部校验面板（26 篇 §6.2 IX-ON-05；画板 ix-on-05，可折叠 220px）：
 *  SHACL 违例列表（违例路径 + 原因 + 「定位」→ 画布节点闪烁）/ 推理结果（新隐含三元组 +
 *  差异徽标）/ 变更预览（本变更单增删改三色列表）三 Tab；「试校验」调 POST validate
 *  渲染结果——确定性校验在后端，前端不跑 SHACL（宪法 2）。 */

type PanelTab = 'shacl' | 'inference' | 'preview'

export function ValidationPanel({
  report,
  loading,
  collapsed,
  onToggleCollapse,
  onRunValidate,
  onLocate,
  draftOps,
}: {
  report: ValidateReport | null
  loading: boolean
  collapsed: boolean
  onToggleCollapse: () => void
  onRunValidate: () => void
  /** 「定位」回调：映射违例 focus → 画布节点闪烁 */
  onLocate: (focus: string) => void
  draftOps: { add: number; del: number; mod: number }
}) {
  const [tab, setTab] = useState<PanelTab>('shacl')

  const violations = report?.results ?? []
  const tabBadge: Record<PanelTab, string> = {
    shacl: violations.length > 0 ? `${violations.length}` : '0',
    inference: report ? `${report.inferences.count}` : '—',
    preview: `+${draftOps.add} −${draftOps.del} ~${draftOps.mod}`,
  }

  if (collapsed) {
    return (
      <button
        type="button"
        className="hairline-t flex h-10 flex-none items-center gap-2 bg-surface px-4 text-left text-xs text-label-2 hover:text-label"
        onClick={onToggleCollapse}
        aria-expanded={false}
        data-testid="validation-panel-collapsed"
      >
        <span className={`dot ${violations.length > 0 ? 'd-red' : 'd-green'}`} style={{ width: 7, height: 7 }} aria-hidden />
        校验面板（ON-05）
        <span className="text-label-3">SHACL {violations.length} 违例 · 推理新增隐含三元组 {report?.inferences.count ?? '—'} · 点击底部展开按钮展开</span>
        <span className="badge b-blue ml-auto">
          展开 <ChevronDown size={11} className="inline rotate-180" aria-hidden />
        </span>
      </button>
    )
  }

  return (
    <section
      className="hairline-t flex h-[220px] flex-none flex-col bg-surface"
      data-testid="validation-panel"
      aria-label="校验面板"
    >
      {/* Tab 头 + 统计 + 试校验 */}
      <div className="flex flex-none items-center gap-2 px-4 pt-2.5">
        {(
          [
            { k: 'shacl', label: `SHACL 违例`, badge: tabBadge.shacl, cls: violations.length > 0 ? 'b-red' : 'b-green' },
            { k: 'inference', label: '推理结果', badge: tabBadge.inference, cls: 'b-blue' },
            { k: 'preview', label: '变更预览', badge: '', cls: '' },
          ] as { k: PanelTab; label: string; badge: string; cls: string }[]
        ).map(t => (
          <button
            key={t.k}
            type="button"
            aria-pressed={tab === t.k}
            onClick={() => setTab(t.k)}
            className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs ${
              tab === t.k ? 'bg-accent-soft font-semibold text-accent' : 'text-label-2 hover:bg-surface-2'
            }`}
          >
            {t.label}
            {t.badge !== '' && <span className={`badge ${t.cls}`}>{t.badge}</span>}
            {t.k === 'preview' && <ChangeCountChips stats={draftOps} />}
          </button>
        ))}
        <span className="mono ml-3 hidden text-[11px] text-label-3 lg:inline">
          {report
            ? `conforms=${String(report.conforms)} · ${report.stats.triples.toLocaleString()} 三元组 · ${report.stats.elapsed_ms} ms · shapes=current`
            : '尚未校验 · 保存草稿后自动运行'}
        </span>
        <button type="button" className="btn btn-g btn-sm ml-auto" data-testid="run-validate" disabled={loading} onClick={onRunValidate}>
          <RefreshCw size={12} className={loading ? 'animate-spin' : ''} aria-hidden />
          {loading ? '校验中…' : '试校验'}
        </button>
        <button type="button" aria-label="折叠校验面板" className="flex h-7 w-7 items-center justify-center rounded-lg text-label-3 hover:bg-surface-2" onClick={onToggleCollapse}>
          <ChevronDown size={14} aria-hidden />
        </button>
      </div>

      {/* 内容区 */}
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-4 py-2">
        {tab === 'shacl' && (
          <>
            {violations.map((v, i) => (
              <div key={`${v.focus}-${i}`} className="flex items-center gap-2 rounded-lg px-1 py-1.5 text-xs hover:bg-surface-2">
                <AlertTriangle size={13} className="flex-none text-red" aria-hidden />
                <span className="badge b-red flex-none">{v.severity}</span>
                <span className="mono flex-none text-label-3">{v.focus}</span>
                <span className="mono flex-none font-semibold">
                  path <span style={{ color: 'var(--accent)' }}>{v.path}</span>
                </span>
                {v.value && <span className="mono flex-none text-label-3">value={v.value}</span>}
                <span className="truncate text-label-2">{v.message}</span>
                <span className="badge b-gray ml-auto flex-none">{v.source_shape}</span>
                <button type="button" className="btn btn-g btn-sm flex-none" data-testid={`locate-${i}`} onClick={() => onLocate(v.focus)}>
                  定位
                </button>
              </div>
            ))}
            {violations.length === 0 && (
              <div className="empty !py-6">
                <div className="t text-[13px]">{loading ? '校验中…' : report ? 'SHACL 全绿，无违例' : '尚无校验结果'}</div>
                <div className="d text-[11px]">点击右上「试校验」运行 SHACL + 一致性检查（在后端执行）。</div>
              </div>
            )}
          </>
        )}

        {tab === 'inference' && (
          <>
            {(report?.inferences.samples ?? []).map(s => (
              <div key={s} className="flex items-center gap-2 rounded-lg px-1 py-1.5 text-xs hover:bg-surface-2">
                <Sparkles size={13} className="flex-none" style={{ color: 'var(--indigo)' }} aria-hidden />
                <span className="badge b-blue flex-none">新隐含</span>
                <span className="mono truncate text-label-2">{s}</span>
              </div>
            ))}
            {report && (
              <div className="mt-1 text-[11px] text-label-3">
                共 {report.inferences.count} 条新隐含三元组（owlrl 前向链推理）；差异徽标随发布在 diff 视图对账。
              </div>
            )}
            {!report && <div className="empty !py-6"><div className="t text-[13px]">尚无推理结果</div></div>}
          </>
        )}

        {tab === 'preview' && (
          <>
            {[
              { op: 'add' as const, n: draftOps.add, text: '（检修工单, subClassOf, 行动）' },
              { op: 'del' as const, n: draftOps.del, text: '（部件, partOf, 车间）' },
              { op: 'mod' as const, n: draftOps.mod, text: '（停电范围, rdfs:label）「停电范围」→「停电区域」' },
            ].map(row => (
              <div
                key={row.op}
                className={`diffline mb-1 flex items-center gap-2 ${row.op === 'add' ? 'add' : row.op === 'del' ? 'del' : ''}`}
                style={row.op === 'mod' ? { background: 'var(--orange-soft)', color: 'var(--orange)' } : undefined}
              >
                <span className="font-bold">{row.op === 'add' ? '+' : row.op === 'del' ? '−' : '~'}</span>
                <span className="truncate">{row.text}</span>
                <span className="ml-auto flex-none">×{row.n}</span>
              </div>
            ))}
            <p className="mt-1 text-[11px] text-label-3">本变更单增删改三元组三色列表；与 ON-08 摘要同源，提交评审后进入审批中心。</p>
          </>
        )}
      </div>
    </section>
  )
}
