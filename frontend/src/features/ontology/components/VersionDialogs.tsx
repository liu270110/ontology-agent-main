import { useEffect, useRef, useState } from 'react'
import { CheckCircle2, XCircle } from 'lucide-react'
import { Modal } from '@/components/modal'
import type { Changeset, DiffImpact, DiffRow, OntoVersion } from '../api'
import { ChangeCountChips, CsStatusBadge } from './shared'
import { Select } from '@/components/select'

/** 版本评审弹窗组（26 篇 §7.1）：IX-VR-01 对比选择器（左右双列 + 差异计数）、
 *  IX-VR-03 通过并发布确认（发布说明必填 + 五步物化进度条「图谱物化→检索索引同步」
 *  中性词推进 + 完成点亮查看图谱）、IX-VR-04 驳回（意见必填 + 类型单选）、
 *  IX-VR-05 回滚（danger：目标版对照 + 审计理由必填 + 输入 ROLLBACK 解锁）。 */

// ---- IX-VR-01 版本对比选择器 ----

export function CompareSelectorDialog({
  open,
  versions,
  onClose,
  onCompare,
  onPreview,
}: {
  open: boolean
  versions: OntoVersion[]
  onClose: () => void
  onCompare: (base: string, target: string) => void
  /** 38-V1：差异计数走真实 diff 端点（不再硬编码 12/3/4）；未传则不渲染预览按钮 */
  onPreview?: (base: string, target: string) => Promise<{ add: number; del: number; mod: number }>
}) {
  const published = versions.filter(v => v.status === 'published')
  const draft = versions.filter(v => v.status === 'draft')
  // 默认值响应式回填：弹窗常挂载（open 受控），versions 可能晚于首挂载到达——
  // 初值不能写成 useState(published[0])（挂载时为空且不会更新）；用户已手选则不回填
  const [left, setLeft] = useState('')
  const [right, setRight] = useState('')
  const [stats, setStats] = useState<{ add: number; del: number; mod: number } | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [previewErr, setPreviewErr] = useState('')

  useEffect(() => {
    if (left) return
    const head = published[0]?.version ?? ''
    if (head) setLeft(head)
  }, [left, published])
  useEffect(() => {
    if (right) return
    const dft = draft[0]?.version ?? published[0]?.version ?? ''
    if (dft) setRight(dft)
  }, [right, draft, published])

  useEffect(() => {
    if (!open) {
      setStats(null)
      setPreviewErr('')
    }
  }, [open])

  async function previewDiff() {
    if (!left || !right || left === right || !onPreview) return
    setPreviewing(true)
    setPreviewErr('')
    try {
      setStats(await onPreview(left, right))
    } catch {
      setPreviewErr('差异预览失败，请重试')
    } finally {
      setPreviewing(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="版本对比"
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn btn-p btn-sm"
            data-testid="compare-go"
            disabled={!left || !right || left === right}
            onClick={() => onCompare(left, right)}
          >
            开始对比
          </button>
        </>
      }
    >
      <div className="grid grid-cols-2 gap-3">
        <div>
          <div className="field-label">基线版本（左）</div>
          <Select aria-label="基线版本" className="input h-8 text-xs" value={left} onChange={e => { setLeft(e.target.value); setStats(null) }}>
            {published.map(v => (
              <option key={v.version} value={v.version}>{v.version} · 已发布</option>
            ))}
          </Select>
          <div className="mt-1 text-[11px] text-label-3">时间线式：仅已发布可选</div>
        </div>
        <div>
          <div className="field-label">目标版本（右）</div>
          <Select aria-label="目标版本" className="input h-8 text-xs" value={right} onChange={e => { setRight(e.target.value); setStats(null) }}>
            {[...draft, ...published].map(v => (
              <option key={v.version} value={v.version}>
                {v.version} · {v.status === 'draft' ? '草稿' : '已发布'}
              </option>
            ))}
          </Select>
          <div className="mt-1 text-[11px] text-label-3">当前草稿默认在右</div>
        </div>
      </div>

      {onPreview && (
        <button type="button" className="btn btn-g btn-sm mt-3" data-testid="compare-preview" disabled={previewing} onClick={() => void previewDiff()}>
          {previewing ? '预览中…' : '预览差异计数'}
        </button>
      )}
      {previewErr && <div className="field-err">{previewErr}</div>}
      {stats && (
        <div className="mt-2 flex items-center gap-2 rounded-lg bg-surface-2 px-3 py-2 text-xs" data-testid="compare-stats">
          差异预览计数：<ChangeCountChips stats={stats} />
          <span className="text-label-3">（base={left} → target={right}）</span>
        </div>
      )}
      {left && right && left === right && <div className="field-err">左右版本相同，请重新选择</div>}
    </Modal>
  )
}

// ---- IX-VR-03 通过并发布确认（五步物化进度条） ----

/** 五步物化：变更写入 → SHACL 复检 → 图谱物化 → 检索索引同步 → 完成（中性词，26 篇口径） */
const PUBLISH_STEPS = ['变更写入', 'SHACL 复检', '图谱物化', '检索索引同步', '完成'] as const

/** 影响面徽标（41 §2 V4.2；画板 p-versions「影响 3 实体 · 2 规则」）：数据全部来自 diff 端点
 *  （api.ts diffImpact 派生，调用方传入）。rules=null（载荷缺四投影段）时只显实体半边；
 *  impact=null（diff 无元素级数据）整枚不渲染。「预计物化 +N 三元组」行无任何端点供数——
 *  数据不可得不显示（不造假），待物化预演端点登记后按数据驱动恢复。 */
export function ImpactBadge({
  impact,
  testid = 'impact-badge',
  className = '',
}: {
  impact: DiffImpact | null
  testid?: string
  className?: string
}) {
  if (!impact) return null
  return (
    <span className={`badge b-gray ${className}`} data-testid={testid}>
      影响 {impact.entities} 实体{impact.rules == null ? '' : ` · ${impact.rules} 规则`}
    </span>
  )
}

export function PublishDialog({
  open,
  changeset,
  impact,
  onClose,
  onPublish,
}: {
  open: boolean
  changeset: Changeset | null
  /** 影响面（41 §2 V4.2）：调用方从 diff 端点载荷派生（diffImpact）；无数据不渲染徽标 */
  impact?: DiffImpact | null
  onClose: () => void
  /** 发布已受理（202）；进度由本弹窗推进，完成后回调携带新版本号 */
  onPublish: (note: string) => Promise<{ version: string }>
}) {
  const [note, setNote] = useState('')
  const [err, setErr] = useState('')
  const [step, setStep] = useState(-1) // -1 未开始；0..4 推进
  const [doneVersion, setDoneVersion] = useState<string | null>(null)
  const [publishing, setPublishing] = useState(false)
  const timers = useRef<number[]>([])

  useEffect(() => {
    return () => {
      timers.current.forEach(t => window.clearTimeout(t))
    }
  }, [])

  function reset() {
    setStep(-1)
    setDoneVersion(null)
    setPublishing(false)
    timers.current.forEach(t => window.clearTimeout(t))
    timers.current = []
  }

  async function start() {
    if (!note.trim()) {
      setErr('发布说明必填')
      return
    }
    setErr('')
    setPublishing(true)
    setStep(0)
    try {
      const res = await onPublish(note) // POST publish 202 受理
      // SSE 实时推进（mock 轮询模拟）：步进到完成
      for (const s of [1, 2, 3, 4]) {
        timers.current.push(window.setTimeout(() => setStep(s), 650 * s))
      }
      timers.current.push(
        window.setTimeout(() => {
          setDoneVersion(res.version)
          setPublishing(false)
        }, 650 * 4 + 250),
      )
    } catch {
      reset()
      setErr('发布受理失败，请重试')
    }
  }

  return (
    <Modal
      open={open}
      onClose={() => {
        reset()
        onClose()
      }}
      title={doneVersion ? '发布完成' : '通过并发布'}
      width={560}
      footer={
        doneVersion ? (
          <>
            <button
              type="button"
              className="btn btn-g btn-sm"
              onClick={() => {
                reset()
                onClose()
              }}
            >
              关闭
            </button>
            <a className="btn btn-p btn-sm" href={`/kb/explore/outage-kb?focus=`} data-testid="publish-view-graph">
              查看图谱
            </a>
          </>
        ) : (
          <>
            <button
              type="button"
              className="btn btn-g btn-sm"
              onClick={() => {
                reset()
                onClose()
              }}
            >
              取消
            </button>
            <button type="button" className="btn btn-p btn-sm" data-testid="publish-go" disabled={publishing || !note.trim()} onClick={() => void start()}>
              {publishing ? '发布中…' : '确认发布'}
            </button>
          </>
        )
      }
    >
      {changeset && (
        <div className="mb-3 rounded-xl border border-separator px-4 py-3 text-xs">
          <div className="flex items-center gap-2">
            <b>变更单 {changeset.id}</b>
            <span className="text-label-3">{changeset.title}</span>
            <CsStatusBadge status={changeset.status} />
            <ImpactBadge impact={impact ?? null} testid="publish-impact" className="ml-auto" />
          </div>
          <div className="mt-1.5 flex items-center gap-2">
            变更计数：<ChangeCountChips stats={changeset.stats} />
          </div>
        </div>
      )}

      {!doneVersion ? (
        <>
          <div className="field">
            <label className="field-label" htmlFor="pub-note">发布说明（必填）</label>
            <textarea
              id="pub-note"
              rows={2}
              className={`input h-auto py-2 text-xs ${err ? 'err' : ''}`}
              placeholder="示例：停电工单行动类扩展；故障父类对齐设备事件"
              value={note}
              onChange={e => {
                setNote(e.target.value)
                if (e.target.value.trim()) setErr('')
              }}
            />
            {err && <div className="field-err">{err}</div>}
          </div>
          <div className="field mb-0">
            <span className="field-label">
              物化进度 <span className="badge b-blue">SSE 实时推进</span>
            </span>
            <div className="steps mt-2" aria-label="发布物化五步进度" data-testid="publish-steps">
              {PUBLISH_STEPS.map((name, i) => (
                <div key={name} className={`step ${step > i ? 'done' : step === i ? 'cur' : ''}`}>
                  <span className="s-dot">{step > i ? '✓' : ''}</span>
                  <span className="s-name">{name}</span>
                </div>
              ))}
            </div>
            {publishing && (
              <p className="fhint" data-testid="publish-progress-text">
                {PUBLISH_STEPS[step] ?? '受理中'}…物化为异步幂等任务，任一步失败将回滚本次发布并告警。
              </p>
            )}
          </div>
        </>
      ) : (
        <div className="rounded-xl border px-4 py-4 text-center" style={{ borderColor: 'var(--green)', background: 'var(--green-soft)' }} data-testid="publish-done">
          <CheckCircle2 size={22} className="mx-auto text-green" aria-hidden />
          <div className="mt-1.5 text-sm font-bold">已发布 {doneVersion} · 全站生效</div>
          <div className="mt-1 text-[11px] text-label-2">版本 +1 已通知订阅人；检索与对话即时切新版本。</div>
        </div>
      )}
    </Modal>
  )
}

// ---- IX-VR-04 驳回 ----

const REJECT_TYPES = ['需修改', '需讨论', '超范围'] as const

export function RejectDialog({
  open,
  changeset,
  onClose,
  onReject,
}: {
  open: boolean
  changeset: Changeset | null
  onClose: () => void
  onReject: (reason: string, type: (typeof REJECT_TYPES)[number]) => Promise<void>
}) {
  const [reason, setReason] = useState('')
  const [type, setType] = useState<(typeof REJECT_TYPES)[number]>('需修改')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit() {
    if (!reason.trim()) {
      setErr('驳回意见必填')
      return
    }
    setErr('')
    setBusy(true)
    try {
      await onReject(reason, type)
      setReason('')
      onClose()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`驳回 ${changeset?.id ?? ''}`}
      width={480}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-d btn-sm" data-testid="reject-go" disabled={busy} onClick={() => void submit()}>
            确认驳回
          </button>
        </>
      }
    >
      <div className="field">
        <label className="field-label" htmlFor="rj-reason">驳回意见（必填，随决策留审计）</label>
        <textarea
          id="rj-reason"
          rows={3}
          className={`input h-auto py-2 text-xs ${err ? 'err' : ''}`}
          placeholder="示例：越层 partOf 断言需先在故障域内对齐父类"
          value={reason}
          onChange={e => {
            setReason(e.target.value)
            if (e.target.value.trim()) setErr('')
          }}
        />
        {err && <div className="field-err">{err}</div>}
      </div>
      <fieldset>
        <legend className="field-label">驳回类型</legend>
        {REJECT_TYPES.map(t => (
          <label key={t} className="flex cursor-pointer items-center gap-2 py-1 text-xs">
            <input type="radio" name="rj-type" checked={type === t} onChange={() => setType(t)} />
            {t}
          </label>
        ))}
      </fieldset>
      <p className="mt-2 text-[11px] text-label-3">提交后变更单回到提交人，工作台解锁。</p>
    </Modal>
  )
}

// ---- IX-VR-05 回滚（danger） ----

export function RollbackDialog({
  open,
  currentVersion,
  targetVersion,
  onClose,
  onRollback,
}: {
  open: boolean
  currentVersion: string
  targetVersion: string
  onClose: () => void
  onRollback: (reason: string) => Promise<void>
}) {
  const [reason, setReason] = useState('')
  const [confirmText, setConfirmText] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit() {
    if (!reason.trim()) {
      setErr('审计理由必填')
      return
    }
    setErr('')
    setBusy(true)
    try {
      await onRollback(reason)
      setReason('')
      setConfirmText('')
      onClose()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`回滚到此版本 ${targetVersion}`}
      width={560}
      danger
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn btn-d btn-sm"
            data-testid="rollback-go"
            disabled={busy || confirmText !== 'ROLLBACK' || !reason.trim()}
            onClick={() => void submit()}
          >
            确认回滚
          </button>
        </>
      }
    >
      {/* 当前版 → 目标版对照摘要 */}
      <div className="mb-3 grid grid-cols-[1fr_auto_1fr] items-center gap-2 rounded-xl bg-surface-2 px-4 py-3 text-xs">
        <div>
          <div className="text-2xs text-label-3">当前版本</div>
          <b>{currentVersion}</b>
        </div>
        <span className="text-label-3">→</span>
        <div>
          <div className="text-2xs text-label-3">目标版本</div>
          <b style={{ color: 'var(--red)' }}>{targetVersion}</b>
        </div>
      </div>
      <div className="alert mb-3" style={{ background: 'var(--red-soft)', borderColor: 'transparent', color: 'var(--red)' }}>
        <XCircle size={15} aria-hidden />
        <div className="text-[11px] leading-5">
          <b>数据不丢失</b>：回滚生成逆向变更单重新走评审（候选非成品）；操作全程审计留痕，任何治理档不可跳过。
        </div>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="rb-reason">审计理由（必填）</label>
        <textarea
          id="rb-reason"
          rows={2}
          className={`input h-auto py-2 text-xs ${err ? 'err' : ''}`}
          placeholder="示例：v2.2 引入的越层断言造成推理冲突，需回退至 v2.1"
          value={reason}
          onChange={e => {
            setReason(e.target.value)
            if (e.target.value.trim()) setErr('')
          }}
        />
        {err && <div className="field-err">{err}</div>}
      </div>
      <div className="field mb-0">
        <label className="field-label" htmlFor="rb-confirm">二次确认：输入 ROLLBACK 解锁</label>
        <input
          id="rb-confirm"
          className="input mono h-8 text-xs"
          placeholder="ROLLBACK"
          value={confirmText}
          onChange={e => setConfirmText(e.target.value)}
        />
      </div>
    </Modal>
  )
}

/** 供 DiffViewer 行渲染使用的 op 元数据（+绿/−红/~黄） */
export function diffRowStyle(op: DiffRow['op']): { bg: string; color: string; sign: string } {
  if (op === 'add') return { bg: 'var(--green-soft)', color: 'var(--green)', sign: '+' }
  if (op === 'del') return { bg: 'var(--red-soft)', color: 'var(--red)', sign: '−' }
  return { bg: 'var(--orange-soft)', color: 'var(--orange)', sign: '~' }
}
