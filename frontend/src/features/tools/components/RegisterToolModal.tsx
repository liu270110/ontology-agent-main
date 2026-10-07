import { useEffect, useRef, useState } from 'react'
import { EditorState, type Extension } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { json } from '@codemirror/lang-json'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { PauseCircle, Wrench } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { registerTool, TOOL_CHANNEL_LABEL, type ToolSourceChannel } from '../api'
import { Select } from '@/components/select'

/** IX-TLS-02 注册工具弹窗（26 篇 §9.2；画板 ix-tls-02）：560px——S1 注册契约
 *  （POST /tools {name, action_iri, source_channel, semantic_annotation, version,
 *  health_hint?, evidence_uri?}；无语义标注 4601 拒绝、重名 4602→409）：
 *  名称 + 行动类 IRI（本体对账键）+ 来源通道 L0~L3（能力来源纯元数据）+
 *  语义标注 JSON 编辑器（CodeMirror 6 lang-json + 令牌主题）+ 版本/健康提示/证据 URI。
 *  诚实态：mock 时代的「试运行」（POST /tools/dry-run）后端未实装（14 §3 未登记）——
 *  按钮置灰 +「后端未实装」，不造假调用；端点登记后恢复。 */

const cmTheme: Extension = EditorView.theme({
  '&': { background: 'var(--surface)', color: 'var(--label)', fontSize: '11.5px', border: '1px solid var(--separator)', borderRadius: '10px' },
  '.cm-content': { fontFamily: 'var(--mono)', caretColor: 'var(--accent)', padding: '8px' },
  '.cm-focused': { outline: 'none', borderColor: 'var(--accent)' },
  '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': { background: 'var(--accent-soft)' },
  '.cm-line': { padding: '0 4px' },
})

function JsonEditor({ value, onChange, label }: { value: string; onChange: (v: string) => void; label: string }) {
  const host = useRef<HTMLDivElement>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    if (!host.current) return
    const state = EditorState.create({
      doc: value,
      extensions: [
        json(),
        cmTheme,
        EditorView.updateListener.of(u => {
          if (u.docChanged) onChangeRef.current(u.state.doc.toString())
        }),
      ],
    })
    const view = new EditorView({ state, parent: host.current })
    return () => view.destroy()
    // 仅挂载时创建；受控值经 updateListener 单向流出
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="field mb-3">
      <span className="field-label">{label}</span>
      <div ref={host} data-testid={`json-editor-${label}`} />
    </div>
  )
}

/** 语义标注默认样例（对齐 kernel ExtensionMeta 口径的最小形状） */
const ANNOTATION_DEFAULT = `{
  "label": "停电工单查询",
  "onto_id": "ont_grid_std",
  "read_only": true
}`

export function RegisterToolModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [actionIri, setActionIri] = useState('')
  const [channel, setChannel] = useState<ToolSourceChannel>('L0')
  const [annotation, setAnnotation] = useState(ANNOTATION_DEFAULT)
  const [version, setVersion] = useState('1.0.0')
  const [healthHint, setHealthHint] = useState('')
  const [evidenceUri, setEvidenceUri] = useState('')
  const [annotationErr, setAnnotationErr] = useState('')

  const register = useMutation({
    mutationFn: () =>
      registerTool({
        name,
        action_iri: actionIri,
        source_channel: channel,
        semantic_annotation: JSON.parse(annotation),
        version,
        health_hint: healthHint.trim() || undefined,
        evidence_uri: evidenceUri.trim() || undefined,
      }),
    onSuccess: res => {
      void qc.invalidateQueries({ queryKey: ['tools'] })
      toast.success(`工具 ${res.data.name} 已注册`, {
        description: 'v1 直通 listed 进入集市；可被 ToolPicker 引用（S1 注册契约）',
      })
      onClose()
    },
    onError: e => {
      toast.error('注册失败', { description: e instanceof Error ? e.message : undefined })
    },
  })

  function parseAnnotation(): Record<string, unknown> | null {
    setAnnotationErr('')
    try {
      const parsed = JSON.parse(annotation)
      if (parsed == null || typeof parsed !== 'object' || Array.isArray(parsed) || Object.keys(parsed).length === 0) {
        setAnnotationErr('语义标注必须是非空 JSON 对象（无语义标注不上架，4601）')
        return null
      }
      return parsed
    } catch (e) {
      setAnnotationErr(`语义标注 JSON 解析失败：${(e as Error).message}`)
      return null
    }
  }

  function reset() {
    setName('')
    setActionIri('')
    setChannel('L0')
    setAnnotation(ANNOTATION_DEFAULT)
    setVersion('1.0.0')
    setHealthHint('')
    setEvidenceUri('')
    setAnnotationErr('')
  }

  return (
    <Modal
      open={open}
      onClose={() => {
        onClose()
        reset()
      }}
      title="注册工具"
      width={560}
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
          <button
            type="button"
            className="btn btn-p btn-sm"
            data-testid="tls-register-go"
            disabled={!name.trim() || !actionIri.trim() || register.isPending || !!annotationErr}
            onClick={() => {
              if (parseAnnotation()) register.mutate()
            }}
          >
            <Wrench size={12} aria-hidden /> 注册工具
          </button>
        </>
      }
    >
      <p className="text-[11px] text-label-3">
        按 S1 注册契约登记集市条目：行动类 IRI 为本体对账键，无语义标注不上架（4601）；
        动作写审计（trace_id）。
      </p>

      <div className="mt-3 grid grid-cols-2 gap-3">
        <div className="field mb-2">
          <label className="field-label" htmlFor="tls-tool-name">
            工具名
          </label>
          <input
            id="tls-tool-name"
            className="input mono"
            data-testid="tls-tool-name"
            placeholder="grid.load.query"
            value={name}
            onChange={e => setName(e.target.value)}
          />
        </div>
        <div className="field mb-2">
          <label className="field-label" htmlFor="tls-tool-channel">
            来源通道（L0~L3 元数据标注）
          </label>
          <Select id="tls-tool-channel" className="input" value={channel} onChange={e => setChannel(e.target.value as ToolSourceChannel)}>
            {(Object.keys(TOOL_CHANNEL_LABEL) as ToolSourceChannel[]).map(c => (
              <option key={c} value={c}>
                {TOOL_CHANNEL_LABEL[c]}
              </option>
            ))}
          </Select>
        </div>
      </div>
      <div className="field mb-2">
        <label className="field-label" htmlFor="tls-tool-iri">
          行动类 IRI（本体对账键）
        </label>
        <input
          id="tls-tool-iri"
          className="input mono"
          data-testid="tls-tool-iri"
          placeholder="ont_grid_std#CL-021"
          value={actionIri}
          onChange={e => setActionIri(e.target.value)}
        />
      </div>

      <JsonEditor label="语义标注（JSON）" value={annotation} onChange={setAnnotation} />
      {annotationErr && <div className="field-err -mt-2 mb-2">{annotationErr}</div>}

      <div className="grid grid-cols-2 gap-3">
        <div className="field mb-2">
          <label className="field-label" htmlFor="tls-tool-version">
            版本
          </label>
          <input id="tls-tool-version" className="input mono" value={version} onChange={e => setVersion(e.target.value)} />
        </div>
        <div className="field mb-2">
          <label className="field-label" htmlFor="tls-tool-health">
            健康提示（可选）
          </label>
          <input id="tls-tool-health" className="input" placeholder="如：依赖 95598 网关可用性" value={healthHint} onChange={e => setHealthHint(e.target.value)} />
        </div>
      </div>
      <div className="field mb-2">
        <label className="field-label" htmlFor="tls-tool-evidence">
          证据 URI（可选）
        </label>
        <input id="tls-tool-evidence" className="input mono" placeholder="https://wiki.example.com/grid-api" value={evidenceUri} onChange={e => setEvidenceUri(e.target.value)} />
      </div>

      {/* 试运行面板：后端未实装诚实态（S1 无 dry-run 端点，14 §3 未登记；勿造假） */}
      <div className="rounded-xl border border-separator p-3" style={{ background: 'var(--surface)' }} data-testid="tls-dryrun">
        <div className="flex items-center gap-2">
          <b className="text-xs">试运行</b>
          <span className="badge b-orange" data-testid="tls-dryrun-unimplemented">
            <PauseCircle size={10} className="mr-1 inline" aria-hidden /> 后端未实装
          </span>
          <button type="button" className="btn btn-s btn-sm ml-auto" data-testid="tls-dryrun-go" disabled title="POST /tools/dry-run 未登记（api/01 §5.6），端点交付后开放">
            试运行
          </button>
        </div>
        <div className="mt-1.5 text-[11px] text-label-3">
          示例参数 → 实际调用 → 结果：POST /tools/dry-run 未登记（14 §3 未列该端点），交付前置灰不造假。
        </div>
      </div>
    </Modal>
  )
}
