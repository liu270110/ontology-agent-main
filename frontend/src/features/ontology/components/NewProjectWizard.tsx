import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { z } from 'zod'
import { Modal } from '@/components/modal'
import { toast } from 'sonner'
import { ChevronRight } from 'lucide-react'
import { createProject, TIER_DESC, type OntoTier } from '../api'
import { NAMESPACE_IRI_REGEX } from './shared'

/** 新建本体项目向导（26 篇 §6.1 IX-OL-01；画板 ix-ol-01）：3 步强制顺序不可跳过——
 *  ①基本信息（zod 校验：名称必填 / 命名空间必须为绝对 IRI 且以 # 或 / 结尾）
 *  ②方案三卡（轻量/标准/重型，容量与推理说明对齐 ontology 02）
 *  ③确认摘要 + 初始化模板（空项目 / 导入 Turtle——选导入则在落地后唤起 IX-OL-02）。
 *  完成即 draft 本体与首个变更单；POST /ontologies（api/01 §5.3）。 */

const step1Schema = z.object({
  name: z.string().trim().min(2, '名称至少 2 个字符'),
  namespace: z
    .string()
    .trim()
    .regex(/^https?:\/\//, '必须为绝对 IRI（http:// 或 https:// 前缀）')
    .regex(NAMESPACE_IRI_REGEX, '必须以「#」或「/」结尾（示例：http://example.org/outage#）'),
  description: z.string().trim().max(120, '描述不超过 120 字'),
})

const TIERS: { key: OntoTier; name: string; tag: string; recommended?: boolean }[] = [
  { key: 'light', name: '轻量图谱', tag: 'Light' },
  { key: 'standard', name: '标准图谱', tag: 'Standard' },
  { key: 'heavy', name: '重型本体', tag: 'Heavy', recommended: true },
]

const STEPS = ['基本信息', '选择方案', '确认初始化'] as const

export function NewProjectWizard({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [step, setStep] = useState(0)
  const [name, setName] = useState('')
  const [namespace, setNamespace] = useState('')
  const [description, setDescription] = useState('')
  const [tier, setTier] = useState<OntoTier>('heavy')
  const [initTemplate, setInitTemplate] = useState<'empty' | 'turtle'>('empty')
  const [errors, setErrors] = useState<Record<string, string>>({})

  const create = useMutation({
    mutationFn: () => createProject({ name, namespace, tier, description }),
    onSuccess: project => {
      void qc.invalidateQueries({ queryKey: ['ontology', 'projects'] })
      toast.success(`已创建「${project.name}」draft 本体与首个变更单`)
      onClose()
      navigate(`/ontology/${project.id}`)
    },
  })

  function validateStep1(): boolean {
    const parsed = step1Schema.safeParse({ name, namespace, description })
    if (parsed.success) {
      setErrors({})
      return true
    }
    const errs: Record<string, string> = {}
    for (const issue of parsed.error.issues) {
      const key = String(issue.path[0])
      if (!errs[key]) errs[key] = issue.message
    }
    setErrors(errs)
    return false
  }

  function next() {
    if (step === 0 && !validateStep1()) return
    setStep(s => Math.min(2, s + 1))
  }

  function reset() {
    setStep(0)
    setName('')
    setNamespace('')
    setDescription('')
    setTier('heavy')
    setInitTemplate('empty')
    setErrors({})
  }

  return (
    <Modal
      open={open}
      onClose={() => {
        onClose()
        reset()
      }}
      title="新建本体项目"
      width={680}
      footer={
        <>
          <button
            type="button"
            className="btn btn-g btn-sm"
            onClick={() => {
              onClose()
              reset()
            }}
          >
            取消
          </button>
          {step > 0 && (
            <button type="button" className="btn btn-g btn-sm" onClick={() => setStep(s => s - 1)}>
              上一步
            </button>
          )}
          {step < 2 ? (
            <button type="button" className="btn btn-p btn-sm" onClick={next}>
              下一步：{STEPS[step + 1]} <ChevronRight size={12} aria-hidden />
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-p btn-sm"
              data-testid="wizard-create"
              disabled={create.isPending}
              onClick={() => void create.mutate()}
            >
              {create.isPending ? '创建中…' : '创建并进入工作台'}
            </button>
          )}
        </>
      }
    >
      <p className="text-xs text-label-3">三步向导 · 步骤强制顺序，不可跳过</p>

      {/* 步骤条（design-system .steps 同源） */}
      <div className="steps mt-3" aria-label="新建向导步骤条">
        {STEPS.map((s, i) => (
          <div key={s} className={`step ${i < step ? 'done' : i === step ? 'cur' : ''}`}>
            <span className="s-dot">{i < step ? '✓' : ''}</span>
            <span className="s-name">
              {i + 1} {s}
            </span>
          </div>
        ))}
      </div>

      {step === 0 && (
        <div className="mt-4">
          <div className="field">
            <label className="field-label" htmlFor="wiz-name">
              名称（唯一）
            </label>
            <input
              id="wiz-name"
              className={`input ${errors.name ? 'err' : ''}`}
              placeholder="示例：配网停电分析本体"
              value={name}
              onChange={e => setName(e.target.value)}
            />
            {errors.name && <div className="field-err">zod：{errors.name}</div>}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="wiz-ns">
              命名空间 IRI
            </label>
            <input
              id="wiz-ns"
              className={`input mono ${errors.namespace ? 'err' : ''}`}
              placeholder="http://example.org/outage#"
              value={namespace}
              onChange={e => setNamespace(e.target.value)}
            />
            {errors.namespace && <div className="field-err">zod：{errors.namespace}</div>}
          </div>
          <div className="field mb-0">
            <label className="field-label" htmlFor="wiz-desc">
              业务描述（可选）
            </label>
            <textarea
              id="wiz-desc"
              className="input h-auto py-2"
              rows={2}
              placeholder="示例：配网故障停电研判与检修工单闭环"
              value={description}
              onChange={e => setDescription(e.target.value)}
            />
          </div>
        </div>
      )}

      {step === 1 && (
        <div className="mt-4 grid grid-cols-3 gap-3">
          {TIERS.map(t => {
            const desc = TIER_DESC[t.key]
            const selected = tier === t.key
            return (
              <button
                type="button"
                key={t.key}
                aria-pressed={selected}
                aria-label={`${t.name}方案`}
                onClick={() => setTier(t.key)}
                className="rounded-xl border p-3.5 text-left transition-colors"
                style={{
                  borderColor: selected ? 'var(--accent)' : 'var(--separator)',
                  boxShadow: selected ? 'var(--glow-accent)' : 'none',
                  background: 'var(--surface)',
                }}
              >
                <div className="flex items-center gap-1.5">
                  <b className="text-[13px]">{t.name}</b>
                  <span className="badge b-gray">{t.tag}</span>
                  {t.recommended && <span className="badge b-purple ml-auto">推荐</span>}
                </div>
                <div className="mt-1.5 text-lg font-bold">{desc.cap}</div>
                <ul className="mt-1.5 space-y-1 text-[11px] leading-4 text-label-2">
                  <li>· {desc.scope}</li>
                  <li>· {desc.infer}</li>
                </ul>
              </button>
            )
          })}
        </div>
      )}

      {step === 2 && (
        <div className="mt-4 space-y-3">
          <div className="rounded-xl border border-separator bg-surface-2 px-4 py-3 text-xs leading-6">
            <div>
              <b>① 基本信息（已填写）：</b>名称「{name}」· 命名空间 <span className="mono">{namespace}</span>
              {description ? ` · 描述「${description}」` : ''}
            </div>
            <div>
              <b>② 方案：</b>{TIERS.find(t => t.key === tier)?.name}（{TIER_DESC[tier].cap} · {TIER_DESC[tier].infer}）
            </div>
            <div className="text-label-3">
              创建即 draft 本体与首个变更单；后续「提交评审 → approve → publish」五动词闭环（设计宪法 3：候选非成品）。
            </div>
          </div>
          <fieldset>
            <legend className="field-label">初始化模板</legend>
            <label className="flex cursor-pointer items-center gap-2 py-1 text-xs">
              <input type="radio" name="wiz-init" checked={initTemplate === 'empty'} onChange={() => setInitTemplate('empty')} />
              空项目（进入工作台后逐步建模）
            </label>
            <label className="flex cursor-pointer items-center gap-2 py-1 text-xs">
              <input type="radio" name="wiz-init" checked={initTemplate === 'turtle'} onChange={() => setInitTemplate('turtle')} />
              导入 Turtle（选择后落地工作台并唤起导入弹窗）
            </label>
          </fieldset>
        </div>
      )}
    </Modal>
  )
}
