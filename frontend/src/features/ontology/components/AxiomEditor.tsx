import { useEffect, useRef, useState } from 'react'
import { EditorState, type Extension } from '@codemirror/state'
import { EditorView, lineNumbers } from '@codemirror/view'
import { StreamLanguage } from '@codemirror/language'
import { turtle } from '@codemirror/legacy-modes/mode/turtle'
import { AlertTriangle, CheckCircle2, FilePlus2, Wand2 } from 'lucide-react'
import type { OntoAxiomRow, ValidateReport } from '../api'

/** 公理编辑器（26 篇 §6.2 IX-ON-08；画板 ix-on-08，?tab=axioms 整页态）：
 *  shape 模板库下拉 + SHACL Turtle mono 编辑器（CodeMirror 6 StreamLanguage 语法级高亮
 *  + 行号）+「试校验」即时结果条（通过绿 / 违例红并列出违规节点）+ 保存为草稿。
 *  SHACL 结构校验是后端职责（宪法 2），前端不引入 SHACL 引擎。 */

const TEMPLATES: { name: string; turtle: string }[] = [
  {
    name: '节点形状 NodeShape（基数 / 枚举 / 数据类型）',
    turtle: '# NodeShape 模板\n@prefix sh: <http://www.w3.org/ns/shacl#> .\n@prefix out: <http://example.org/outage#> .\n\nout:NewShape a sh:NodeShape ;\n  sh:targetClass out:NewClass ;\n  sh:property [\n    sh:path out:hasStatus ;\n    sh:in ( "running" "fault" ) ;\n  ] .',
  },
  {
    name: '属性约束 PropertyShape（shclass / sh:in）',
    turtle: '# PropertyShape 模板\n@prefix sh: <http://www.w3.org/ns/shacl#> .\n@prefix out: <http://example.org/outage#> .\n\nout:NewPropShape a sh:NodeShape ;\n  sh:targetClass out:Device ;\n  sh:property [\n    sh:path out:locatedIn ;\n    sh:class out:Feeder ;\n    sh:minCount 1 ;\n  ] .',
  },
  {
    name: '枚举闭合模板（状态码类，如设备状态）',
    turtle: '# 枚举闭合模板\n@prefix sh: <http://www.w3.org/ns/shacl#> .\n@prefix out: <http://example.org/outage#> .\n\nout:StatusShape a sh:NodeShape ;\n  sh:targetClass out:Device ;\n  sh:property [\n    sh:path out:hasStatus ;\n    sh:in ( "running" "fault" "maintenance" ) ;\n  ] .',
  },
]

/** 编辑器主题：透明底 + 令牌着色（暗色随 html.dark 令牌生效） */
const cmTheme: Extension = EditorView.theme({
  '&': { background: 'transparent', color: 'var(--label)', fontSize: '12px' },
  '.cm-gutters': { background: 'transparent', color: 'var(--label-3)', border: 'none' },
  '.cm-activeLine': { background: 'var(--surface-2)' },
  '.cm-activeLineGutter': { background: 'var(--surface-2)', color: 'var(--label-2)' },
  '.cm-content': { fontFamily: 'var(--mono)', caretColor: 'var(--accent)' },
  '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': { background: 'var(--accent-soft)' },
})

function useCodeMirror(value: string, onChange: (v: string) => void) {
  const host = useRef<HTMLDivElement>(null)
  const view = useRef<EditorView | null>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    if (!host.current) return
    const state = EditorState.create({
      doc: value,
      extensions: [
        lineNumbers(),
        StreamLanguage.define(turtle),
        cmTheme,
        EditorView.lineWrapping,
        EditorView.updateListener.of(u => {
          if (u.docChanged) onChangeRef.current(u.state.doc.toString())
        }),
      ],
    })
    const v = new EditorView({ state, parent: host.current })
    view.current = v
    return () => v.destroy()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 外部值（模板注入）→ 全量替换
  useEffect(() => {
    const v = view.current
    if (!v) return
    const cur = v.state.doc.toString()
    if (value !== cur) {
      v.dispatch({ changes: { from: 0, to: cur.length, insert: value } })
    }
  }, [value])

  return host
}

export function AxiomEditor({
  axiom,
  validation,
  validating,
  onRunValidate,
  onSaveDraft,
}: {
  axiom: OntoAxiomRow | null
  validation: ValidateReport | null
  validating: boolean
  onRunValidate: (shapeName: string) => void
  onSaveDraft: () => void
}) {
  const [shapeName, setShapeName] = useState(axiom?.name ?? 'FaultShape')
  const [code, setCode] = useState(axiom?.turtle ?? TEMPLATES[0].turtle)
  const [draftSaved, setDraftSaved] = useState(false)
  const host = useCodeMirror(code, setCode)

  useEffect(() => {
    if (axiom) {
      setShapeName(axiom.name)
      setCode(axiom.turtle)
      setDraftSaved(false)
    }
  }, [axiom])

  const violations = validation?.results ?? []
  const conforms = validation?.conforms ?? null

  return (
    <div className="flex min-h-0 flex-1 gap-3" data-testid="axiom-editor">
      {/* 编辑区 */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-separator bg-surface">
        <div className="hairline-b flex flex-none items-center gap-2 px-4 py-2.5">
          <span className="mono text-[12px] font-semibold">{shapeName}.ttl</span>
          {conforms === null ? (
            <span className="badge b-gray">未校验</span>
          ) : conforms ? (
            <span className="badge b-green">Turtle 语法通过 · 0 违例</span>
          ) : (
            <span className="badge b-orange">试校验 {violations.length} 违例</span>
          )}
          <span className="ml-auto flex gap-2">
            <select
              aria-label="shape 模板下拉"
              className="input h-7 w-56 text-[11.5px]"
              value=""
              onChange={e => {
                const t = TEMPLATES.find(x => x.name === e.target.value)
                if (t) {
                  setCode(t.turtle)
                  setShapeName(/out:(\w+)/.exec(t.turtle)?.[1] ?? shapeName)
                  setDraftSaved(false)
                }
              }}
            >
              <option value="">模板：插入 shape 模板…</option>
              {TEMPLATES.map(t => (
                <option key={t.name} value={t.name}>{t.name}</option>
              ))}
            </select>
            <button type="button" className="btn btn-s btn-sm" data-testid="axiom-validate" disabled={validating} onClick={() => onRunValidate(shapeName)}>
              <Wand2 size={12} className={validating ? 'animate-spin' : ''} aria-hidden /> 试校验
            </button>
            <button
              type="button"
              className="btn btn-p btn-sm"
              data-testid="axiom-save-draft"
              onClick={() => {
                onSaveDraft()
                setDraftSaved(true)
              }}
            >
              <FilePlus2 size={12} aria-hidden /> {draftSaved ? '已存草稿' : '保存为草稿'}
            </button>
          </span>
        </div>
        <div ref={host} className="scroll-thin min-h-0 flex-1 overflow-auto px-3 py-2" />
        <div className="hairline-t flex flex-none items-center gap-3 px-4 py-1.5 text-[10.5px] text-label-3">
          <span className="mono">行 {code.split('\n').length} · 列 5</span>
          <span>UTF-8</span>
          <span>SHACL 1.1</span>
        </div>
      </div>

      {/* 右栏：试校验结果 + 模板库 + 不直写 TBox 提示 */}
      <div className="flex w-[300px] flex-none flex-col gap-3 overflow-y-auto">
        <div
          className="rounded-xl border px-4 py-3"
          style={{
            borderColor: conforms === false ? 'var(--red)' : 'var(--separator)',
          }}
          data-testid="axiom-validate-result"
        >
          <div className="flex items-center gap-1.5 text-[13px] font-bold">
            试校验结果
            {conforms === null && <span className="badge b-gray ml-auto">未运行</span>}
            {conforms === true && <span className="badge b-green ml-auto">通过</span>}
            {conforms === false && <span className="badge b-red ml-auto">未通过 · {violations.length} 违例</span>}
          </div>
          <p className="mono mt-1 text-[10px] text-label-3">POST /ontologies/(id)/validate · conforms={conforms === null ? '—' : String(conforms)}</p>
          {conforms === false && (
            <ul className="mt-2 space-y-2">
              {violations.map((v, i) => (
                <li key={i} className="rounded-lg bg-surface-2 px-2.5 py-2 text-[11px] leading-4">
                  <div className="flex items-center gap-1.5">
                    <AlertTriangle size={12} className="flex-none text-red" aria-hidden />
                    <b className="mono">{v.focus.split('·')[1]?.trim() ?? v.focus}</b>
                    <span className="text-label-3">{v.path}</span>
                  </div>
                  <div className="mt-0.5 text-label-2">
                    {v.message}（{v.constraint}）
                  </div>
                </li>
              ))}
            </ul>
          )}
          {conforms === true && (
            <div className="mt-2 flex items-center gap-1.5 text-[11.5px] text-green">
              <CheckCircle2 size={13} aria-hidden /> 全部约束满足，可保存为草稿。
            </div>
          )}
          <p className="mt-2 text-[10.5px] text-label-3">点「定位」回画布闪烁对应节点；修复后跑到绿。</p>
        </div>

        <div className="rounded-xl border border-separator bg-surface px-4 py-3">
          <div className="flex items-center gap-2 text-[13px] font-bold">
            shape 模板库 <span className="badge b-purple">SHACL</span>
          </div>
          <ul className="mt-2 space-y-1.5">
            {TEMPLATES.map(t => (
              <li key={t.name}>
                <button
                  type="button"
                  className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[11.5px] hover:bg-surface-2"
                  onClick={() => setCode(t.turtle)}
                >
                  <FilePlus2 size={12} className="flex-none text-label-3" aria-hidden />
                  <span className="truncate">{t.name}</span>
                  <span className="ml-auto flex-none text-label-3">＋</span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="alert rounded-xl" style={{ background: 'var(--accent-soft)', borderColor: 'transparent', color: 'var(--accent)' }}>
          <div className="text-[11.5px] leading-5">
            <b>编辑不直写 TBox</b>
            <br />
            保存为草稿写入变更单；发布仍走 submit → approve → publish 五动词，与设计宪法「候选非成品」一致。
          </div>
        </div>
      </div>
    </div>
  )
}
