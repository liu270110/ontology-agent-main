import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { AlertTriangle, ArrowRight, Check, History, Plus, Search, Shield } from 'lucide-react'
import { Modal } from '@/components/modal'
import { ApiError } from '@/api/client'
import {
  createWorkflow,
  listTemplates,
  publishWorkflow,
  rollbackWorkflow,
  type WfDetail,
} from '../api'

/** 工作流三弹窗（26 篇 §15 矩阵）：GRP-06 新建/从模板 · GRP-10 提交发布确认（治理分流）·
 *  GRP-11 版本对比/回滚（以旧版新建草稿）。（GRP-09 断点恢复=X16 真事件版收进 TestRunPanel
 *  一键继续——审批类暂停凭审批中心回执续跑，mock 快照弹窗退役。） */

// ============================================================
// IX-GRP-06 新建 / 从模板（Modal 560px）
// ============================================================

export function NewWorkflowDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  const [templates, setTemplates] = useState<{ id: string; name: string; desc: string }[]>([])
  const [name, setName] = useState('停电故障研判 · 检索问答')
  const [desc, setDesc] = useState('输入故障现象 → 检索台账与规程 → 生成研判意见并附出处 → 人工确认归档')
  const [template, setTemplate] = useState('rag_qa')
  const [busy, setBusy] = useState(false)
  const [errMsg, setErrMsg] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    void listTemplates().then(r => setTemplates(r.items ?? []))
  }, [open])

  async function submit() {
    setBusy(true)
    setErrMsg(null)
    try {
      const created = await createWorkflow({ name: name.trim(), description: desc.trim(), template })
      toast.success('草稿 v1 已创建 · 可试运行后才可提交发布')
      onClose()
      navigate(`/workflows/${created.id}`)
    } catch (e) {
      setErrMsg(e instanceof ApiError ? e.message : '创建失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="新建工作流" width={560}>
      <div className="mb-2 text-xs text-label-2">从模板开始或空白编排；创建即建草稿 v1，可试运行后才可提交发布。</div>
      <div className="field">
        <label className="field-label" htmlFor="wf-new-name">名称</label>
        <input id="wf-new-name" className="input" data-testid="wf-new-name" value={name} onChange={e => setName(e.target.value)} />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="wf-new-desc">描述</label>
        <input id="wf-new-desc" className="input" data-testid="wf-new-desc" value={desc} onChange={e => setDesc(e.target.value)} />
      </div>
      <div className="field-label">模板选择（含示例节点与参数，可在编辑器中改删）</div>
      <div className="grid grid-cols-3 gap-2.5">
        {templates.map(t => {
          const active = template === t.id
          return (
            <button
              key={t.id}
              type="button"
              data-testid={`wf-tpl-${t.id}`}
              className="relative rounded-[13px] p-3 text-left"
              style={{
                border: active ? '1.5px solid var(--accent)' : '1px solid var(--separator)',
                background: active ? 'var(--accent-soft)' : 'transparent',
              }}
              onClick={() => setTemplate(t.id)}
            >
              {active && <span className="badge b-blue absolute right-2 top-2">已选</span>}
              <span
                className="flex h-[30px] w-[30px] items-center justify-center rounded-lg"
                style={{ background: t.id === 'blank' ? 'var(--surface-2)' : t.id === 'approval_flow' ? 'var(--green-soft)' : 'var(--accent-soft)', color: t.id === 'blank' ? 'var(--label-2)' : t.id === 'approval_flow' ? 'var(--green)' : 'var(--accent)' }}
                aria-hidden
              >
                {t.id === 'blank' ? <Plus size={15} /> : t.id === 'approval_flow' ? <Shield size={15} /> : <Search size={15} />}
              </span>
              <b className="mt-2 block text-xs" style={active ? { color: 'var(--accent)' } : undefined}>{t.name}</b>
              <div className="mt-0.5 text-[11px] leading-relaxed text-label-2">{t.desc}</div>
            </button>
          )
        })}
      </div>
      {errMsg && <div className="field-err mt-2">{errMsg}</div>}
      <div className="hairline-t mt-4 flex items-center gap-2 pt-3">
        <span className="mr-auto text-[11px] text-label-3">草稿可随意改 · 发布生成不可变版本（候选非成品）</span>
        <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
        <button type="button" className="btn btn-p" data-testid="wf-new-create" disabled={!name.trim() || busy} onClick={() => void submit()}>
          <ArrowRight size={13} aria-hidden />
          创建并进入编辑器
        </button>
      </div>
    </Modal>
  )
}

// ============================================================
// IX-GRP-10 提交发布确认（Modal 560px）
// ============================================================

export function PublishDialog({
  open,
  onClose,
  detail,
  onPublished,
}: {
  open: boolean
  onClose: () => void
  detail: WfDetail | null
  onPublished: () => void
}) {
  const navigate = useNavigate()
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [errMsg, setErrMsg] = useState<string | null>(null)

  useEffect(() => {
    if (open) { setNote(''); setErrMsg(null) }
  }, [open])

  const counts = useMemo(() => ({
    nodes: detail?.nodes.length ?? 0,
    edges: detail?.edges.length ?? 0,
    params: Object.values(detail?.nodes ?? {}).filter(n => (n.params ? Object.keys(n.params).length : 0) > 0).length,
  }), [detail])

  async function submit() {
    if (!detail) return
    setBusy(true)
    setErrMsg(null)
    try {
      const r = await publishWorkflow(detail.id, note.trim())
      if (r.status === 'pending_approval') {
        // 治理分流：team/enterprise → workflow_publish 审批工单（第七类对象候选，X16）
        toast.success('已转审批中心 workflow_publish 工单（team 档）· 审批通过后版本 +1')
        onClose()
        onPublished()
        navigate(`/console/approvals?ref=${r.approval_id ?? ''}`)
      } else {
        toast.success(`发布完成 · 版本 +1（${r.next_version ?? ''}）`)
        onClose()
        onPublished()
      }
    } catch (e) {
      setErrMsg(e instanceof ApiError ? e.message : '提交失败')
    } finally {
      setBusy(false)
    }
  }

  const v = detail?.draft_version ?? 'v1'
  const nextV = `v${Number(v.replace('v', '')) + 1}`

  return (
    <Modal open={open} onClose={onClose} title="提交发布 · 生成不可变版本" width={560}>
      <div className="mb-2.5 text-xs text-label-2">候选非成品：发布即版本化归档；草稿 {v} 仍可继续修改，不受发布影响。</div>
      <div className="rounded-xl border border-separator p-3">
        <div className="flex flex-wrap items-center gap-2">
          <b className="text-xs">版本摘要</b>
          <span className="mono text-[11px] text-label-3">草稿 {v} → 新版本 {nextV}（预览）</span>
          <span className="ml-auto flex gap-1.5">
            <span className="badge b-green">+{counts.nodes} 节点</span>
            <span className="badge b-blue">{counts.edges} 边</span>
            <span className="badge b-orange">~{counts.params} 参数调整</span>
          </span>
        </div>
        <div className="mt-2.5 grid grid-cols-2 gap-1.5 text-[11px]">
          {([
            ['DAG 无环校验', detail?.validation.dag],
            ['节点 ACL 校验（编排不提权）', detail?.validation.acl],
            ['表达式确定性校验 · 无裸 LLM 分支', detail?.validation.expression],
            [`试运行通过 · ${detail?.validation.test_run ?? '—'}`, detail?.validation.test_run ? true : false],
          ] as [string, boolean | undefined][]).map(([label, okFlag]) => (
            <span key={label} className="flex items-center gap-1.5 text-label-2">
              <Check size={12} style={{ color: okFlag ? 'var(--green)' : 'var(--orange)', flex: 'none' }} aria-hidden />
              {label} · {okFlag ? '通过' : '待试运行'}
            </span>
          ))}
        </div>
      </div>
      <div className="field mt-3 mb-0">
        <label className="field-label" htmlFor="wf-publish-note">发布说明（必填，进入版本历史与审计）</label>
        <input
          id="wf-publish-note"
          className="input"
          data-testid="wf-publish-note"
          value={note}
          onChange={e => setNote(e.target.value)}
          placeholder="概要说明本次版本变更…"
        />
      </div>
      <div className="mt-3 flex gap-2.5">
        <div className="flex-1 rounded-xl border border-separator p-2.5 opacity-70">
          <div className="flex items-center gap-1.5 text-xs font-bold text-label-2"><AlertTriangle size={12} aria-hidden />solo 档</div>
          <div className="mt-1 text-[11px] leading-relaxed text-label-3">直发：校验通过即版本 +1 并通知协作者。</div>
        </div>
        <div className="flex-[1.25] rounded-xl p-2.5" style={{ border: '1.5px solid var(--accent)', background: 'var(--accent-soft)' }}>
          <div className="flex items-center gap-1.5 text-xs font-bold" style={{ color: 'var(--accent)' }}>
            <Shield size={12} aria-hidden />
            team · enterprise 档
            <span className="badge b-blue ml-auto">当前 · team</span>
          </div>
          <div className="mt-1 text-[11px] leading-relaxed text-label-2">转审批中心 workflow_publish 工单（第七类对象候选，挂账 X16），审批通过后版本 +1 并通知。</div>
        </div>
      </div>
      {errMsg && <div className="field-err mt-2">{errMsg}</div>}
      <div className="hairline-t mt-3.5 flex items-center gap-2 pt-3">
        <span className="mr-auto text-[11px] text-label-3">提交人 刘以在 · 操作写审计</span>
        <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
        <button type="button" className="btn btn-p" data-testid="wf-publish-go" disabled={!note.trim() || busy} onClick={() => void submit()}>
          <ArrowRight size={13} aria-hidden />
          提交发布（转审批）
        </button>
      </div>
    </Modal>
  )
}

// ============================================================
// IX-GRP-11 版本对比 / 回滚（Modal，复用 IX-VR-01/VR-05 模式）
// ============================================================

export function VersionDialog({
  open,
  onClose,
  detail,
  onRolledBack,
}: {
  open: boolean
  onClose: () => void
  detail: WfDetail | null
  onRolledBack: () => void
}) {
  const [baseline, setBaseline] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [errMsg, setErrMsg] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setBaseline(detail?.versions?.[detail.versions.length - 1]?.version ?? '')
      setErrMsg(null)
    }
  }, [open, detail])

  async function rollback() {
    if (!detail || !baseline) return
    setBusy(true)
    setErrMsg(null)
    try {
      const r = await rollbackWorkflow(detail.id, baseline)
      toast.success(`已以 ${r.copied_from} 新建草稿 ${r.draft_version}（已发布版本不可变可考）`)
      onClose()
      onRolledBack()
    } catch (e) {
      setErrMsg(e instanceof ApiError ? e.message : '回滚失败')
    } finally {
      setBusy(false)
    }
  }

  const diff = detail?.draft_diff

  return (
    <Modal open={open} onClose={onClose} title="版本对比 / 回滚" width={560}>
      <div className="mb-3 text-xs text-label-2">左侧选对比基线，右侧为对比目标（默认当前草稿）；回滚 = 以旧版新建草稿。</div>
      <div className="flex gap-3.5">
        <div className="min-w-0 flex-1">
          <div className="field-label">对比基线（已发布可选）</div>
          <div className="overflow-hidden rounded-[11px] border border-separator" data-testid="wf-version-list">
            {(detail?.versions ?? []).map(v => (
              <label
                key={v.version}
                className="flex cursor-pointer items-center gap-2 border-b border-separator px-2.5 py-2 text-xs last:border-b-0"
                style={baseline === v.version ? { background: 'var(--accent-soft)' } : undefined}
              >
                <input
                  type="radio"
                  className="sr-only"
                  checked={baseline === v.version}
                  onChange={() => setBaseline(v.version)}
                />
                <span
                  className="flex h-4 w-4 flex-none items-center justify-center rounded-full border"
                  style={{ borderColor: baseline === v.version ? 'var(--accent)' : 'var(--separator)' }}
                  aria-hidden
                >
                  {baseline === v.version && <span className="h-2 w-2 rounded-full" style={{ background: 'var(--accent)' }} />}
                </span>
                <span className="mono font-bold" style={{ color: baseline === v.version ? 'var(--accent)' : 'var(--label-3)' }}>{v.version}</span>
                <span>已发布 · {v.published_at}</span>
                <span className="badge b-gray ml-auto">{v.diff || v.note}</span>
              </label>
            ))}
          </div>
        </div>
        <div className="min-w-0 flex-1">
          <div className="field-label">对比目标</div>
          <div className="rounded-[11px] border p-3" style={{ border: '1.5px dashed var(--accent)', background: 'var(--accent-soft)' }}>
            <div className="flex items-center gap-2 text-xs">
              <span className="mono font-bold" style={{ color: 'var(--accent)' }}>草稿 {detail?.draft_version}</span>
              <span className="badge b-orange">当前编辑中</span>
            </div>
            <div className="mt-2 flex flex-col gap-1" data-testid="wf-version-diff">
              <span className="diffline add">+{diff?.add ?? 0} 节点（Agent +2 · 工具 +1 · 条件 +1 …）</span>
              <span className="diffline del">−{diff?.del ?? 0} 节点（冗余模板转换）</span>
              <span className="diffline" style={{ background: 'var(--orange-soft)', color: 'var(--orange)' }}>~{diff?.mod ?? 0} 边（条件阈值 · 并行汇入）</span>
            </div>
          </div>
        </div>
      </div>
      <div className="alert al-info mt-3">
        <History size={15} aria-hidden />
        <div><b>回滚 = 以旧版新建草稿</b>将复制所选旧版全部节点与参数为新草稿，不影响已发布版本与运行历史；新草稿发布仍走 GRP-10 治理分流。</div>
      </div>
      {errMsg && <div className="field-err mt-2">{errMsg}</div>}
      <div className="hairline-t mt-3.5 flex items-center gap-2 pt-3">
        <span className="mr-auto text-[11px] text-label-3">已发布版本不可变 · 回滚可逆</span>
        <button type="button" className="btn btn-g" onClick={onClose}>关闭</button>
        <button type="button" className="btn btn-p" data-testid="wf-rollback-go" disabled={!baseline || busy} onClick={() => void rollback()}>
          <Plus size={13} aria-hidden />
          以 {baseline || '—'} 新建草稿
        </button>
      </div>
    </Modal>
  )
}
