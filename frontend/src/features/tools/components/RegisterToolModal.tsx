import { useEffect, useRef, useState } from 'react'
import { EditorState, type Extension } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { json } from '@codemirror/lang-json'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Play, Wrench, XCircle } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { dryRunTool, registerTool } from '../api'

/** IX-TLS-02 注册工具弹窗（26 篇 §9.2；画板 ix-tls-02）：560px——
 *  HTTP 工具封装（endpoint + 鉴权方式）+ 输入/输出 Schema JSON 编辑器
 *  （CodeMirror 6 lang-json + 令牌主题）+ 试运行面板（示例参数 → 实际调用 → 结果）。
 *  注册后进入工具注册中心（GET /tools 可见，可被 ToolPicker 勾选）。 */

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

const INPUT_SCHEMA_DEFAULT = `{
  "type": "object",
  "required": ["tg_id"],
  "properties": {
    "tg_id": { "type": "string", "description": "台区编号" },
    "window": { "type": "string", "enum": ["1h", "24h", "7d"] }
  }
}`
const OUTPUT_SCHEMA_DEFAULT = `{
  "type": "object",
  "properties": {
    "tg_id": { "type": "string" },
    "p_max_kw": { "type": "number" },
    "samples": { "type": "integer" }
  }
}`

export function RegisterToolModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [endpoint, setEndpoint] = useState('')
  const [auth, setAuth] = useState('Bearer Token')
  const [inputSchema, setInputSchema] = useState(INPUT_SCHEMA_DEFAULT)
  const [outputSchema, setOutputSchema] = useState(OUTPUT_SCHEMA_DEFAULT)
  const [example, setExample] = useState('{"tg_id":"TQ-0417","window":"24h"}')
  const [run, setRun] = useState<{ status: number; elapsed_ms: number; result: string } | 'error' | null>(null)
  const [schemaErr, setSchemaErr] = useState('')

  const register = useMutation({
    mutationFn: () =>
      registerTool({
        name,
        desc,
        endpoint,
        auth,
        input_schema: JSON.parse(inputSchema),
        output_schema: JSON.parse(outputSchema),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['tools'] })
      toast.success(`工具 ${name} 已注册`, { description: '进入工具注册中心，可被 ToolPicker 勾选注入 Agent' })
      onClose()
    },
  })

  function tryRun() {
    setRun(null)
    setSchemaErr('')
    try {
      JSON.parse(inputSchema)
      JSON.parse(outputSchema)
    } catch (e) {
      setSchemaErr(`Schema JSON 解析失败：${(e as Error).message}`)
      return
    }
    try {
      const input = JSON.parse(example)
      void dryRunTool({ name, input_example: input }).then(res => {
        setRun({ status: res.status, elapsed_ms: res.elapsed_ms, result: JSON.stringify(res.result) })
      })
    } catch {
      setSchemaErr('示例参数不是合法 JSON')
    }
  }

  function reset() {
    setName('')
    setDesc('')
    setEndpoint('')
    setAuth('Bearer Token')
    setInputSchema(INPUT_SCHEMA_DEFAULT)
    setOutputSchema(OUTPUT_SCHEMA_DEFAULT)
    setRun(null)
    setSchemaErr('')
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
            disabled={!name.trim() || !endpoint.trim() || register.isPending || !!schemaErr}
            onClick={() => register.mutate()}
          >
            <Wrench size={12} aria-hidden /> 注册工具
          </button>
        </>
      }
    >
      <p className="text-[11px] text-label-3">
        将业务 HTTP 接口封装为受治理工具：调用经平台网关，带审计与 trace_id（设计宪法 5）。
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
          <label className="field-label" htmlFor="tls-tool-type">
            类型
          </label>
          <select id="tls-tool-type" className="input" defaultValue="HTTP 工具封装">
            <option>HTTP 工具封装</option>
          </select>
        </div>
      </div>
      <div className="field mb-2">
        <label className="field-label" htmlFor="tls-tool-desc">
          描述
        </label>
        <input
          id="tls-tool-desc"
          className="input"
          placeholder="查询台区实时负荷与 24h 历史曲线，用于停电范围研判"
          value={desc}
          onChange={e => setDesc(e.target.value)}
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="field mb-2">
          <label className="field-label" htmlFor="tls-tool-endpoint">
            Endpoint
          </label>
          <input
            id="tls-tool-endpoint"
            className="input mono"
            data-testid="tls-tool-endpoint"
            placeholder="https://grid-api.example.com/v1/load"
            value={endpoint}
            onChange={e => setEndpoint(e.target.value)}
          />
        </div>
        <div className="field mb-2">
          <label className="field-label" htmlFor="tls-tool-auth">
            鉴权方式
          </label>
          <select id="tls-tool-auth" className="input" value={auth} onChange={e => setAuth(e.target.value)}>
            <option>Bearer Token</option>
            <option>Basic</option>
            <option>内网白名单（无鉴权头）</option>
          </select>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <JsonEditor label="输入 Schema（JSON）" value={inputSchema} onChange={setInputSchema} />
        <JsonEditor label="输出 Schema（JSON）" value={outputSchema} onChange={setOutputSchema} />
      </div>
      {schemaErr && <div className="field-err -mt-2 mb-2">{schemaErr}</div>}

      {/* 试运行面板 */}
      <div className="rounded-xl border border-separator p-3" style={{ background: 'var(--surface)' }} data-testid="tls-dryrun">
        <div className="flex items-center gap-2">
          <b className="text-xs">试运行</b>
          <span className="text-[11px] text-label-3">示例参数 → 实际调用 → 结果</span>
          <button type="button" className="btn btn-s btn-sm ml-auto" data-testid="tls-dryrun-go" onClick={tryRun}>
            <Play size={11} aria-hidden /> 试运行
          </button>
        </div>
        <div className="mt-2 flex items-center gap-2">
          <input
            className="input !h-7 flex-1 font-mono text-[11px]"
            aria-label="示例参数（JSON）"
            data-testid="tls-dryrun-example"
            value={example}
            onChange={e => setExample(e.target.value)}
          />
          {run && run !== 'error' && (
            <span className="badge b-green">
              {run.status} · {run.elapsed_ms}ms
            </span>
          )}
          {run === 'error' && (
            <span className="badge b-red">
              <XCircle size={10} aria-hidden /> 失败
            </span>
          )}
        </div>
        {run && run !== 'error' && (
          <pre
            className="mono mt-2 overflow-x-auto rounded-lg px-2.5 py-2 text-[11px] leading-4"
            style={{ background: 'var(--surface-2)' }}
            data-testid="tls-dryrun-result"
          >
            {run.result}
          </pre>
        )}
      </div>
    </Modal>
  )
}
