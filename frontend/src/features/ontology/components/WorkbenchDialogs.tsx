import { useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2 } from 'lucide-react'
import { Modal } from '@/components/modal'
import type { Changeset, OntoClassNode, OntoPropertyRow, ValidateReport } from '../api'
import { ChangeCountChips } from './shared'
import { Select } from '@/components/select'

/** 工作台弹窗组（26 篇 §6.2）：IX-ON-01 新建类/属性/规则（类型随左树 Tab）、
 *  IX-ON-02 连线（关系）编辑（谓词下拉 + 基数 min/max + 双向开关）、
 *  IX-ON-06 提交评审（三色计数 + 说明必填 + 评审人 + 校验全绿才可提交 → /approvals）。
 *  创建/编辑一律写入变更单草稿，不直写 TBox（设计宪法 3：候选非成品）。 */

const CURATORS = ['刘以在（curator）', '陈评（curator）']

// ---- IX-ON-01 新建类/属性/规则 ----

export function NewElementDialog({
  open,
  elementType,
  namespace,
  classes,
  onClose,
  onCreate,
}: {
  open: boolean
  elementType: 'class' | 'property' | 'rule'
  namespace: string
  classes: OntoClassNode[]
  onClose: () => void
  onCreate: (label: string) => void
}) {
  const [name, setName] = useState('')
  const [label, setLabel] = useState('')
  const [iri, setIri] = useState('')
  const [parentId, setParentId] = useState('')
  const [abstract, setAbstract] = useState(false)
  const [domain, setDomain] = useState('')
  const [range, setRange] = useState('')
  const [propType, setPropType] = useState<'data' | 'object'>('data')
  const [min, setMin] = useState(0)
  const [max, setMax] = useState(1)
  const [ruleName, setRuleName] = useState('')
  const [construct, setConstruct] = useState('CONSTRUCT { ?a ?b ?c } WHERE { ?a ?b ?c }')
  const [template, setTemplate] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})

  const TITLE: Record<'class' | 'property' | 'rule', string> = {
    class: '新建类',
    property: '新建属性',
    rule: '新建规则',
  }

  const TEMPLATES: Record<string, string> = {
    '规则：故障触发抢修工单': 'CONSTRUCT { ?fault out:triggers ?order } WHERE { ?fault a out:Fault . ?order a out:WorkOrder ; out:forFault ?fault }',
    '规则：停电范围沿馈线传播': 'CONSTRUCT { ?area out:affectedBy ?fault } WHERE { ?fault out:locatedIn ?feeder . ?area out:servedBy ?feeder }',
    '规则：复电报告合并': 'CONSTRUCT { ?r1 out:mergedInto ?r2 } WHERE { ?r1 a out:RestoreReport ; out:forEvent ?e . ?r2 out:forEvent ?e }',
  }

  function autoIri(raw: string) {
    setIri(`${namespace}${raw}`)
  }

  function submit() {
    const errs: Record<string, string> = {}
    if (elementType === 'class') {
      if (!/^[A-Z][A-Za-z0-9]*$/.test(name)) errs.name = 'GB/T 48000.3：唯一名需 ^[A-Z][A-Za-z0-9]*$'
      if (!label.trim()) errs.label = '中文标签必填'
    } else if (elementType === 'property') {
      if (!/^[a-z][A-Za-z0-9]*$/.test(name)) errs.name = '属性名需小驼峰（示例：hasStatus）'
      if (!domain) errs.domain = '定义域必填'
      if (!range) errs.range = '值域必填'
    } else {
      if (!ruleName.trim()) errs.name = '规则编号必填（示例：R-021）'
      if (!/^CONSTRUCT\s*\{/i.test(construct)) errs.construct = '必须是 SPARQL CONSTRUCT 模板'
    }
    setErrors(errs)
    if (Object.keys(errs).length > 0) return
    onCreate(label || name || ruleName)
    setName(''); setLabel(''); setIri(''); setRuleName('')
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`${TITLE[elementType]}（写入变更单草稿）`}
      width={560}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-p btn-sm" data-testid="new-element-submit" onClick={submit}>
            创建
          </button>
        </>
      }
    >
      {elementType === 'class' && (
        <>
          <div className="field">
            <label className="field-label" htmlFor="ne-name">Name（唯一名）</label>
            <input
              id="ne-name"
              className={`input h-8 text-xs ${errors.name ? 'err' : ''}`}
              placeholder="示例：OutageEvent"
              value={name}
              onChange={e => {
                setName(e.target.value)
                autoIri(e.target.value)
              }}
            />
            {errors.name && <div className="field-err">{errors.name}</div>}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="ne-iri">IRI（自动生成，可改）</label>
            <input id="ne-iri" className="input mono h-8 text-xs" value={iri} onChange={e => setIri(e.target.value)} />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="ne-label">中文标签</label>
            <input id="ne-label" className={`input h-8 text-xs ${errors.label ? 'err' : ''}`} value={label} onChange={e => setLabel(e.target.value)} />
            {errors.label && <div className="field-err">{errors.label}</div>}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="ne-parent">父类（多选树取其一）</label>
            <Select id="ne-parent" className="input h-8 text-xs" value={parentId} onChange={e => setParentId(e.target.value)}>
              <option value="">— 无（顶层类）—</option>
              {classes.map(c => (
                <option key={c.id} value={c.id}>{c.label} {c.name}</option>
              ))}
            </Select>
          </div>
          <label className="flex cursor-pointer items-center gap-2 text-xs">
            <input type="checkbox" checked={abstract} onChange={e => setAbstract(e.target.checked)} />
            抽象类 abstract（不落实例）
          </label>
        </>
      )}

      {elementType === 'property' && (
        <>
          <div className="field">
            <label className="field-label" htmlFor="ne-pname">属性名（小驼峰）</label>
            <input
              id="ne-pname"
              className={`input h-8 text-xs ${errors.name ? 'err' : ''}`}
              placeholder="示例：hasStatus"
              value={name}
              onChange={e => setName(e.target.value)}
            />
            {errors.name && <div className="field-err">{errors.name}</div>}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="ne-ptype">类型</label>
            <Select id="ne-ptype" className="input h-8 text-xs" value={propType} onChange={e => setPropType(e.target.value as 'data' | 'object')}>
              <option value="data">数据属性（xsd）</option>
              <option value="object">对象属性（→ 类）</option>
            </Select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="ne-domain">定义域</label>
            <Select id="ne-domain" className={`input h-8 text-xs ${errors.domain ? 'err' : ''}`} value={domain} onChange={e => setDomain(e.target.value)}>
              <option value="">— 选择类 —</option>
              {classes.map(c => (
                <option key={c.id} value={c.iri}>{c.label} {c.name}</option>
              ))}
            </Select>
            {errors.domain && <div className="field-err">{errors.domain}</div>}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="ne-range">值域</label>
            <Select id="ne-range" className={`input h-8 text-xs ${errors.range ? 'err' : ''}`} value={range} onChange={e => setRange(e.target.value)}>
              <option value="">— 选择 —</option>
              {propType === 'data' ? (
                ['xsd:string', 'xsd:decimal', 'xsd:dateTime', 'xsd:boolean'].map(x => (
                  <option key={x} value={x}>{x}</option>
                ))
              ) : (
                classes.map(c => (
                  <option key={c.id} value={c.iri}>{c.label} {c.name}</option>
                ))
              )}
            </Select>
            {errors.range && <div className="field-err">{errors.range}</div>}
          </div>
          <div className="flex gap-3">
            <div className="field flex-1">
              <label className="field-label" htmlFor="ne-min">基数 min</label>
              <input id="ne-min" type="number" min={0} className="input h-8 text-xs" value={min} onChange={e => setMin(Number(e.target.value))} />
            </div>
            <div className="field flex-1">
              <label className="field-label" htmlFor="ne-max">基数 max</label>
              <input id="ne-max" type="number" min={0} className="input h-8 text-xs" value={max} onChange={e => setMax(Number(e.target.value))} />
            </div>
          </div>
        </>
      )}

      {elementType === 'rule' && (
        <>
          <div className="field">
            <label className="field-label" htmlFor="ne-rname">规则编号</label>
            <input id="ne-rname" className={`input mono h-8 text-xs ${errors.name ? 'err' : ''}`} placeholder="示例：R-021" value={ruleName} onChange={e => setRuleName(e.target.value)} />
            {errors.name && <div className="field-err">{errors.name}</div>}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="ne-tpl">模板下拉</label>
            <Select
              id="ne-tpl"
              className="input h-8 text-xs"
              value={template}
              onChange={e => {
                setTemplate(e.target.value)
                if (TEMPLATES[e.target.value]) setConstruct(TEMPLATES[e.target.value])
              }}
            >
              <option value="">— 选择 CONSTRUCT 模板 —</option>
              {Object.keys(TEMPLATES).map(t => (
                <option key={t} value={t}>{t}</option>
              ))}
            </Select>
          </div>
          <div className="field mb-0">
            <label className="field-label" htmlFor="ne-construct">SPARQL CONSTRUCT（mono 编辑器）</label>
            <textarea
              id="ne-construct"
              rows={5}
              className={`input mono h-auto py-2 text-[11px] leading-5 ${errors.construct ? 'err' : ''}`}
              value={construct}
              onChange={e => setConstruct(e.target.value)}
            />
            {errors.construct && <div className="field-err">{errors.construct}</div>}
          </div>
        </>
      )}
      <p className="mt-3 text-[11px] text-label-3">创建后画布新节点入场并暂存变更单草稿；提交评审通过发布后才写入 TBox。</p>
    </Modal>
  )
}

// ---- IX-ON-02 连线（关系）编辑 ----

export function ConnectDialog({
  open,
  conn,
  classes,
  properties,
  onClose,
  onConfirm,
}: {
  open: boolean
  conn: { source: string; target: string } | null
  classes: OntoClassNode[]
  properties: OntoPropertyRow[]
  onClose: () => void
  onConfirm: (payload: { predicate: string; min: number; max: number; bidirectional: boolean }) => void
}) {
  const [predicate, setPredicate] = useState('')
  const [min, setMin] = useState(0)
  const [max, setMax] = useState(1)
  const [bidirectional, setBidirectional] = useState(false)
  const [err, setErr] = useState('')

  const sourceCls = classes.find(c => c.iri === conn?.source)
  const targetCls = classes.find(c => c.iri === conn?.target)
  /** 谓词候选：按两端类兼容性过滤（定义域命中源类或其祖先的属性） */
  const candidates = useMemo(
    () => properties.filter(p => !sourceCls || p.domain_id === sourceCls.id || p.domain_id === 'c-gridobject'),
    [properties, sourceCls],
  )

  function confirm() {
    if (!predicate) {
      setErr('谓词必选（按两端类兼容性过滤）')
      return
    }
    if (min > max) {
      setErr('基数 min 不可大于 max')
      return
    }
    setErr('')
    onConfirm({ predicate, min, max, bidirectional })
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="连线（关系）编辑"
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-p btn-sm" data-testid="connect-confirm" onClick={confirm}>确认关系</button>
        </>
      }
    >
      <div className="mb-3 flex items-center gap-2 rounded-lg bg-surface-2 px-3 py-2 text-xs">
        <b>{sourceCls ? `${sourceCls.label} ${sourceCls.name}` : conn?.source}</b>
        <span className="text-label-3">—【谓词】→</span>
        <b>{targetCls ? `${targetCls.label} ${targetCls.name}` : conn?.target}</b>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="conn-pred">谓词（按两端类兼容性过滤 + 搜索）</label>
        <Select id="conn-pred" className="input h-8 text-xs" value={predicate} onChange={e => setPredicate(e.target.value)}>
          <option value="">— 选择谓词 —</option>
          {candidates.map(p => (
            <option key={p.id} value={p.iri}>
              {p.name}（{p.label}）
            </option>
          ))}
        </Select>
      </div>
      <div className="flex gap-3">
        <div className="field flex-1">
          <label className="field-label" htmlFor="conn-min">基数 min</label>
          <input id="conn-min" type="number" min={0} className="input h-8 text-xs" value={min} onChange={e => setMin(Number(e.target.value))} />
        </div>
        <div className="field flex-1">
          <label className="field-label" htmlFor="conn-max">基数 max</label>
          <input id="conn-max" type="number" min={0} className="input h-8 text-xs" value={max} onChange={e => setMax(Number(e.target.value))} />
        </div>
      </div>
      <label className="flex cursor-pointer items-center gap-2 text-xs">
        <input type="checkbox" checked={bidirectional} onChange={e => setBidirectional(e.target.checked)} />
        双向关系（inverseOf 同义互推；开启即双向确认开关）
      </label>
      <p className="mt-2 text-[11px] text-label-3">约束附加（关联 SHACL shape）可在公理编辑器（IX-ON-08）中追加。</p>
      {err && <div className="field-err">{err}</div>}
    </Modal>
  )
}

// ---- IX-ON-06 提交评审 ----

export function SubmitReviewDialog({
  open,
  changeset,
  validation,
  onClose,
  onSubmitted,
}: {
  open: boolean
  changeset: Changeset | null
  /** 校验报告：全绿才可提交（违例阻断并列出） */
  validation: ValidateReport | null
  onClose: () => void
  onSubmitted: (changeset: Changeset) => void
}) {
  const [note, setNote] = useState('')
  const [reviewer, setReviewer] = useState(CURATORS[0])
  const [err, setErr] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const violations = validation?.results ?? []
  const allGreen = !!validation && violations.length === 0

  async function submit() {
    if (!note.trim()) {
      setErr('变更说明必填')
      return
    }
    if (!changeset) return
    setSubmitting(true)
    try {
      onSubmitted(changeset)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="提交评审"
      width={560}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn btn-p btn-sm"
            data-testid="submit-review-go"
            disabled={!allGreen || !note.trim() || submitting || !changeset}
            onClick={() => void submit()}
          >
            {submitting ? '提交中…' : '提交评审'}
          </button>
        </>
      }
    >
      {changeset && (
        <div className="mb-3 rounded-xl border border-separator px-4 py-3 text-xs">
          <div className="flex items-center gap-2">
            <b>变更单 {changeset.id}</b>
            <span className="text-label-3">{changeset.title}</span>
            <ChangeCountChips stats={changeset.stats} className="ml-auto" />
          </div>
        </div>
      )}

      {/* 校验状态卡（全绿才可提交） */}
      <div
        className="mb-3 rounded-xl border px-4 py-3 text-xs"
        style={{
          borderColor: allGreen ? 'var(--green)' : 'var(--red-soft)',
          background: allGreen ? 'var(--green-soft)' : 'var(--red-soft)',
        }}
        data-testid="submit-gate"
      >
        <div className="flex items-center gap-1.5 font-semibold">
          {allGreen ? <CheckCircle2 size={13} className="text-green" aria-hidden /> : <AlertTriangle size={13} className="text-red" aria-hidden />}
          校验状态：{allGreen ? '全绿，可提交' : `存在 ${violations.length} 条 SHACL 违例，阻断提交`}
        </div>
        {!allGreen && violations.length > 0 && (
          <ul className="mt-1.5 space-y-0.5 text-[11px]">
            {violations.slice(0, 3).map((v, i) => (
              <li key={i} className="truncate">
                ⛔ {v.focus} · {v.path} · {v.message}
              </li>
            ))}
          </ul>
        )}
        {!validation && <div className="mt-1 text-[11px]">尚未试校验——请先在工作台底部校验面板运行。</div>}
      </div>

      <div className="field">
        <label className="field-label" htmlFor="sr-note">变更说明（必填）</label>
        <textarea
          id="sr-note"
          rows={3}
          className={`input h-auto py-2 text-xs ${err ? 'err' : ''}`}
          placeholder="示例：新增停电工单行动类，故障父类对齐设备事件"
          value={note}
          onChange={e => {
            setNote(e.target.value)
            if (e.target.value.trim()) setErr('')
          }}
        />
        {err && <div className="field-err">{err}</div>}
      </div>
      <div className="field mb-0">
        <label className="field-label" htmlFor="sr-reviewer">目标评审人（curator）</label>
        <Select id="sr-reviewer" className="input h-8 text-xs" value={reviewer} onChange={e => setReviewer(e.target.value)}>
          {CURATORS.map(c => (
            <option key={c} value={c}>{c}</option>
          ))}
        </Select>
      </div>
      <p className="mt-3 text-[11px] text-label-3">提交 → 审批中心出现卡片，工作台转只读锁定态；驳回将退回提交人。</p>
    </Modal>
  )
}
