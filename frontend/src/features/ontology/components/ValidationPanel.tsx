import { useMemo, useState } from 'react'
import { AlertTriangle, ChevronDown, Download, RefreshCw, Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import type { ValidateReport } from '../api'
import { Sheet } from '@/components/sheet'
import { ChangeCountChips } from './shared'
import { useWorkbenchStore } from '../stores/workbench-store'

/** 底部校验面板（26 篇 §6.2 IX-ON-05；画板 ix-on-05，可折叠 220px）：
 *  SHACL 违例列表（违例路径 + 原因 + 「定位」→ 画布节点闪烁）/ 推理结果（新隐含三元组 +
 *  差异徽标）/ 变更预览（workbench-store pending 层真实草稿：「+ 新建类 N / + 属性边 N /
 *  − 标记删除 N」，空时显示空态）三 Tab；「试校验」调 POST validate 渲染结果——
 *  确定性校验在后端，前端不跑 SHACL（宪法 2）。
 *  「查看报告」= 报告 Sheet（三项指标行：SHACL 违例数 / 术语唯一性 / HermiT 一致性）
 *  +「导出报告」JSON 下载（纯客户端 Blob，同 memory 导出模式）。 */

type PanelTab = 'shacl' | 'inference' | 'preview'

/** mock validate 响应的追加指标（ontology-handlers term_uniqueness；后端契约回填前按可选读） */
type ValidateReportWithMetrics = ValidateReport & { term_uniqueness?: number }

export function ValidationPanel({
  report,
  loading,
  collapsed,
  onToggleCollapse,
  onRunValidate,
  onLocate,
}: {
  report: ValidateReport | null
  loading: boolean
  collapsed: boolean
  onToggleCollapse: () => void
  onRunValidate: () => void
  /** 「定位」回调：映射违例 focus → 画布节点闪烁 */
  onLocate: (focus: string) => void
}) {
  const [tab, setTab] = useState<PanelTab>('shacl')
  const [reportOpen, setReportOpen] = useState(false)

  // ---- 变更预览数据源 = workbench-store pending 层（真实草稿，候选非成品宪法 3）：
  //  refOnly 引用上屏是已有类的钉位引用（store 定义「非新建」），不计新建类；
  //  mod 信号 = _manualDirty（Inspector「应用修改」bumpDirty 唯一写入点，元素级计数）。
  const pendingClasses = useWorkbenchStore(s => s.pendingClasses)
  const pendingEdges = useWorkbenchStore(s => s.pendingEdges)
  const pendingDeletes = useWorkbenchStore(s => s.pendingDeletes)
  const inspectorMods = useWorkbenchStore(s => s._manualDirty)

  const newClassCount = useMemo(() => pendingClasses.filter(c => !c.refOnly).length, [pendingClasses])
  const propEdgeCount = useMemo(() => pendingEdges.filter(e => e.kind === 'property').length, [pendingEdges])
  const subEdgeCount = useMemo(() => pendingEdges.filter(e => e.kind === 'subclass').length, [pendingEdges])
  /** 三色计数 chips 口径：add=新建类+属性/层级边，del=标记删除，mod=Inspector 元数据修改数 */
  const draftOps = useMemo(
    () => ({ add: newClassCount + propEdgeCount + subEdgeCount, del: pendingDeletes.length, mod: inspectorMods }),
    [newClassCount, propEdgeCount, subEdgeCount, pendingDeletes.length, inspectorMods],
  )

  const violations = report?.results ?? []
  const reportMetrics = report as ValidateReportWithMetrics | null
  const tabBadge: Record<PanelTab, string> = {
    shacl: violations.length > 0 ? `${violations.length}` : '0',
    inference: report ? `${report.inferences.count}` : '—',
    preview: `+${draftOps.add} −${draftOps.del} ~${draftOps.mod}`,
  }

  /** 导出校验报告 JSON（纯客户端 Blob + a.download，无新端点；同 memory 导出模式） */
  function exportReport() {
    if (!report) return
    const now = new Date()
    const ymd = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}`
    const filename = `ontology-validate-report-${ymd}.json`
    const payload = {
      exported_at: now.toISOString(),
      conforms: report.conforms,
      shacl_violations: report.results.length,
      term_uniqueness: reportMetrics?.term_uniqueness ?? null,
      hermit_consistent: report.conforms,
      stats: report.stats,
      results: report.results,
      inferences: report.inferences,
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
    toast.success('校验报告已导出', { description: `${filename} · 纯客户端导出（无新端点）` })
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
        <button
          type="button"
          className="btn btn-g btn-sm ml-auto"
          data-testid="onto-report-open"
          disabled={!report}
          onClick={() => setReportOpen(true)}
        >
          查看报告
        </button>
        <button type="button" className="btn btn-g btn-sm" data-testid="run-validate" disabled={loading} onClick={onRunValidate}>
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
            {/* 41 §2 V4.3 ChangesetPreview 真实化：pending 层逐类生成预览行（仅计数 > 0 的行），
                不再有写死示例三元组；无任何草稿 → 空态文案 */}
            {[
              { op: 'add' as const, label: '新建类', n: newClassCount },
              { op: 'add' as const, label: '属性边', n: propEdgeCount },
              { op: 'add' as const, label: '层级边（subClassOf）', n: subEdgeCount },
              { op: 'del' as const, label: '标记删除', n: pendingDeletes.length },
            ]
              .filter(row => row.n > 0)
              .map(row => (
                <div
                  key={row.label}
                  className={`diffline mb-1 flex items-center gap-2 ${row.op === 'add' ? 'add' : 'del'}`}
                  data-testid={`preview-row-${row.label}`}
                >
                  <span className="font-bold">{row.op === 'add' ? '+' : '−'}</span>
                  <span>{row.label}</span>
                  <span className="ml-auto flex-none">×{row.n}</span>
                </div>
              ))}
            {draftOps.add === 0 && pendingDeletes.length === 0 && (
              <div className="empty !py-6" data-testid="preview-empty">
                <div className="t text-[13px]">暂无待提交变更</div>
                <div className="d text-[11px]">在画布新建类、连线或右键删除元素后，此处实时预览本变更单草稿（候选非成品）。</div>
              </div>
            )}
            <p className="mt-1 text-[11px] text-label-3">
              工作台 pending 草稿实时预览（另计 Inspector 元数据修改 {inspectorMods} 处）；保存草稿/提交评审后写入变更单进入审批中心。
            </p>
          </>
        )}
      </div>

      {/* 验证报告 Sheet（查看报告入口）：三项指标行 + 导出 JSON */}
      <Sheet open={reportOpen} onClose={() => setReportOpen(false)} title="验证报告" width={480}>
        <div className="px-5 py-4" data-testid="onto-report-sheet">
          <div className="space-y-2.5">
            <div className="flex items-center gap-2 rounded-xl border border-separator bg-surface-2 px-3.5 py-2.5" data-testid="onto-report-row-shacl">
              <span className="text-xs text-label-2">SHACL 违例数</span>
              <span className={`badge ml-auto ${violations.length > 0 ? 'b-red' : 'b-green'}`}>
                {report ? `${violations.length} 条` : '—'}
              </span>
            </div>
            <div className="flex items-center gap-2 rounded-xl border border-separator bg-surface-2 px-3.5 py-2.5" data-testid="onto-report-row-uniqueness">
              <span className="text-xs text-label-2">术语唯一性</span>
              <span className={`badge ml-auto ${reportMetrics?.term_uniqueness != null && reportMetrics.term_uniqueness >= 1 ? 'b-green' : 'b-gray'}`}>
                {reportMetrics?.term_uniqueness != null ? `${Math.round(reportMetrics.term_uniqueness * 100)}%` : '—'}
              </span>
            </div>
            <div className="flex items-center gap-2 rounded-xl border border-separator bg-surface-2 px-3.5 py-2.5" data-testid="onto-report-row-hermit">
              <span className="text-xs text-label-2">HermiT 一致性</span>
              <span className={`badge ml-auto ${report?.conforms ? 'b-green' : 'b-red'}`}>
                {report ? (report.conforms ? '通过' : `未通过 · ${violations.length} 违例`) : '—'}
              </span>
            </div>
          </div>
          <div className="mt-3 rounded-xl px-3.5 py-2.5 text-[11px] leading-relaxed text-label-3" style={{ background: 'var(--surface-2)', border: '1px solid var(--separator)' }}>
            {report
              ? `conforms=${String(report.conforms)} · ${report.stats.triples.toLocaleString()} 三元组 · ${report.stats.elapsed_ms} ms · 推理新增隐含三元组 ${report.inferences.count} 条（owlrl）。确定性校验在后端执行（宪法 2）。`
              : '尚未校验 · 保存草稿后自动运行，或点「试校验」。'}
          </div>
          <div className="mt-4 flex items-center gap-2">
            <button type="button" className="btn btn-p btn-sm" data-testid="onto-report-export" disabled={!report} onClick={exportReport}>
              <Download size={12} aria-hidden />
              导出报告
            </button>
            <span className="text-[11px] text-label-3">JSON · 纯客户端导出（无新端点）</span>
          </div>
        </div>
      </Sheet>
    </section>
  )
}
