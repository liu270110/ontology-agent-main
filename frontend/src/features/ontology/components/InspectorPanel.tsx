import { useEffect, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { Link2, RotateCcw, Save } from 'lucide-react'
import { listClasses, type OntoClassNode } from '../api'
import { useWorkbenchStore } from '../stores/workbench-store'
import { Select } from '@/components/select'

/** 右侧检查器（26 篇 §6.2 IX-ON-04；画板 ix-on-04，右栏 320px）：
 *  GB/T 48000.3 类 8 项元数据（Name/Label/Definition/SubclassOf/DisjointWith/抽象开关/
 *  同义标签/状态）+ IRI 只读；RHF 注册 + zod 校验错误就地显示；脏态「应用修改」
 *  （写入变更单草稿，不直写 TBox——宪法 3）+「还原」。
 *  迷你导航 seg：元数据为本视图；属性/公理为跳转入口——经 store leftTab 切左树对应
 *  Tab（IX-ON-03 联动；公理 Tab 即 IX-ON-08 公理编辑器）。出处/修订历史区依赖
 *  来源文档与修订记录数据，OntoClassNode 暂无该字段——数据不可得整区不渲染（不造假）。 */

const NAME_REGEX = /^[A-Z][A-Za-z0-9]*$/

const metaSchema = z.object({
  name: z.string().regex(NAME_REGEX, '需匹配 ^[A-Z][A-Za-z0-9]*$（示例：Feeder）'),
  label: z.string().trim().min(1, '中文标签必填'),
  definition: z.string().trim().min(4, '定义至少 4 个字符（GB/T 48000.3 必填）'),
  subclassOf: z.string(),
  disjointWith: z.string(),
  synonyms: z.string(),
})

type MetaForm = z.infer<typeof metaSchema>

export function InspectorPanel({ projectId, cls }: { projectId: string; cls: OntoClassNode | null }) {
  const dirty = useWorkbenchStore(s => s.dirtyCount)
  const bumpDirty = useWorkbenchStore(s => s.bumpDirty)
  const resetDirty = useWorkbenchStore(s => s.resetDirty)
  const setLeftTab = useWorkbenchStore(s => s.setLeftTab)
  const [applied, setApplied] = useState(false)
  const [fieldErr, setFieldErr] = useState<Partial<Record<keyof MetaForm, string>>>({})

  const { register, watch, reset, getValues, setValue } = useForm<MetaForm>({
    defaultValues: { name: '', label: '', definition: '', subclassOf: '', disjointWith: '', synonyms: '' },
  })
  const values = watch()

  useEffect(() => {
    if (!cls) return
    reset({
      name: cls.name,
      label: cls.label,
      definition: cls.definition ?? '',
      subclassOf: cls.parent_id ?? '',
      disjointWith: '',
      synonyms: (cls.synonyms ?? []).join(' / '),
    })
    setApplied(false)
    setFieldErr({})
  }, [cls, reset])

  const [classList, setClassList] = useState<OntoClassNode[]>([])
  useEffect(() => {
    let alive = true
    listClasses(projectId)
      .then(d => alive && setClassList(d.items))
      .catch(e => {
        // P-001 断供收敛（2026-10-07）：类清单失败（404=读模型端点未上线/网络错误）不炸检查器——
        // console.warn 留观测痕迹 + 空选项降级（父类下拉仅剩「无（顶层类）」、子类 chips 不渲染）。
        console.warn('[inspector] listClasses 加载失败，父类选项降级为空', e)
        if (alive) setClassList([])
      })
    return () => {
      alive = false
    }
  }, [projectId])

  const parent = classList.find(c => c.id === getValues('subclassOf'))

  function apply() {
    if (!cls) return
    const parsed = metaSchema.safeParse(values)
    if (!parsed.success) {
      const errs: Partial<Record<keyof MetaForm, string>> = {}
      for (const issue of parsed.error.issues) {
        const key = String(issue.path[0]) as keyof MetaForm
        if (!errs[key]) errs[key] = issue.message
      }
      setFieldErr(errs)
      return
    }
    setFieldErr({})
    setApplied(true)
    bumpDirty(1)
  }

  if (!cls) {
    return (
      <aside className="flex w-[260px] flex-none xl:w-[300px] flex-col rounded-xl border border-separator bg-surface" data-testid="onto-inspector">
        <div className="empty flex-1 !justify-center">
          <div className="t text-[13px]">未选中节点</div>
          <div className="d px-6 text-[11px]">点击画布节点或左侧类树，在此编辑 GB/T 48000.3 八项元数据。</div>
        </div>
      </aside>
    )
  }

  const err = (k: keyof MetaForm) =>
    fieldErr[k] ? <div className="field-err">zod：{fieldErr[k]}</div> : null

  // 子类 chips（只读）：从类清单按 parent_id 过滤当前类的下位类（设计稿 p-onto Inspector 子类行）
  const children = classList.filter(c => c.parent_id === cls.id)

  return (
    <aside className="scroll-thin flex w-[260px] flex-none xl:w-[300px] flex-col overflow-y-auto rounded-xl border border-separator bg-surface" data-testid="onto-inspector">
      {/* 头：选中节点 + 脏态徽标 */}
      <div className="flex-none px-4 pt-4">
        <div className="flex items-center gap-1.5">
          <b className="truncate text-sm">{cls.label} {cls.name}</b>
          {applied ? (
            <span className="badge b-green ml-auto">已应用 · 待保存</span>
          ) : (
            <span className="badge b-orange ml-auto">脏态 · 未应用</span>
          )}
        </div>
        <div className="mono mt-1 flex items-center gap-1 truncate text-[11px] text-label-3" title={cls.iri}>
          {cls.iri}
          <Link2 size={10} className="flex-none" aria-hidden />
        </div>
        {applied && (
          <div className="alert mt-2 !px-3 !py-2 text-[11px]" style={{ background: 'var(--accent-soft)', borderColor: 'transparent', color: 'var(--accent)' }}>
            本地修改已反转暂存，刷新前可回退（F-15d）；保存草稿后写入变更单（不直写 TBox）。
          </div>
        )}
      </div>

      {/* 迷你导航：元数据/属性/公理（元数据为主视图；属性/公理 = store leftTab 切左树 Tab） */}
      <div className="seg mx-4 mt-3 flex-none">
        <button type="button" className="seg-btn on flex-1">元数据</button>
        <button
          type="button"
          className="seg-btn flex-1"
          data-testid="insp-seg-properties"
          onClick={() => setLeftTab('properties')}
        >
          属性
        </button>
        <button
          type="button"
          className="seg-btn flex-1"
          data-testid="insp-seg-axioms"
          onClick={() => setLeftTab('axioms')}
        >
          公理
        </button>
      </div>

      <div className="flex-1 px-4 py-3">
        <div className="field">
          <label className="field-label" htmlFor="insp-name">Name（唯一名）</label>
          <input id="insp-name" className={`input h-8 text-xs ${fieldErr.name ? 'err' : ''}`} {...register('name')} />
          {err('name')}
        </div>
        <div className="field">
          <label className="field-label" htmlFor="insp-label">Label（中文标签）</label>
          <input id="insp-label" className={`input h-8 text-xs ${fieldErr.label ? 'err' : ''}`} {...register('label')} />
          {err('label')}
        </div>
        <div className="field">
          <label className="field-label" htmlFor="insp-def">Definition（定义）</label>
          <textarea
            id="insp-def"
            rows={3}
            className={`input h-auto py-2 text-xs ${fieldErr.definition ? 'err' : ''}`}
            {...register('definition')}
          />
          {err('definition')}
        </div>
        <div className="field">
          <label className="field-label" htmlFor="insp-parent">SubclassOf（父类）</label>
          <Select
            id="insp-parent"
            className="input h-8 text-xs"
            name="subclassOf"
            value={values.subclassOf ?? ''}
            onChange={e => setValue('subclassOf', e.target.value, { shouldDirty: true })}
          >
            <option value="">— 无（顶层类）—</option>
            {classList
              .filter(c => c.id !== cls.id)
              .map(c => (
                <option key={c.id} value={c.id}>
                  {c.label} {c.name}
                </option>
              ))}
          </Select>
          {parent && <div className="fhint">当前父类：{parent.label} {parent.name}</div>}
        </div>
        {/* 子类 chips 行（只读，随 SubclassOf 展示层级上下文；无子类不渲染） */}
        {children.length > 0 && (
          <div className="field" data-testid="insp-children">
            <span className="field-label">子类（{children.length}）</span>
            <div className="flex flex-wrap gap-1">
              {children.map(c => (
                <span key={c.id} className="badge b-gray" title={c.iri}>
                  {c.label} {c.name}
                </span>
              ))}
            </div>
          </div>
        )}
        <div className="field">
          <label className="field-label" htmlFor="insp-disjoint">DisjointWith（互斥类）</label>
          <input id="insp-disjoint" className="input h-8 text-xs" placeholder="变压器 Transformer、开关 Switch" {...register('disjointWith')} />
        </div>
        <div className="flex items-center justify-between py-1">
          <span className="field-label !mb-0">抽象类 abstract</span>
          <span className={`relative inline-flex h-[18px] w-8 flex-none rounded-full ${cls.abstract ? 'bg-accent' : 'border border-separator bg-surface-2'}`}>
            <span className="absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white shadow transition-all" style={{ left: cls.abstract ? 16 : 2 }} aria-hidden />
          </span>
        </div>
        <div className="field mt-2">
          <label className="field-label" htmlFor="insp-syn">同义标签（≤3）</label>
          <input id="insp-syn" className="input h-8 text-xs" placeholder="配电线路 / 馈电线" {...register('synonyms')} />
        </div>
        <div className="field">
          <span className="field-label">状态</span>
          <span className="badge b-green">在用</span>
        </div>

        {/* 出处 + 修订历史（IX-ON-04）：来源文档/修订记录数据不可得（OntoClassNode 无
         *  该字段），整区不渲染——不造假占位，待 api 契约补充后按数据驱动恢复 */}
      </div>

      {/* 脏态操作条：应用修改（→变更单草稿）/ 还原 */}
      <div className="hairline-t sticky bottom-0 flex flex-none items-center gap-2 bg-surface px-4 py-3">
        <button type="button" className="btn btn-p btn-sm flex-1" data-testid="inspector-apply" onClick={apply}>
          <Save size={12} aria-hidden /> 应用修改
        </button>
        <button
          type="button"
          className="btn btn-g btn-sm"
          data-testid="inspector-revert"
          onClick={() => {
            reset({
              name: cls.name, label: cls.label, definition: cls.definition ?? '',
              subclassOf: cls.parent_id ?? '', disjointWith: '', synonyms: (cls.synonyms ?? []).join(' / '),
            })
            setApplied(false)
            setFieldErr({})
            if (dirty > 0) resetDirty()
          }}
        >
          <RotateCcw size={12} aria-hidden /> 还原
        </button>
      </div>
    </aside>
  )
}
