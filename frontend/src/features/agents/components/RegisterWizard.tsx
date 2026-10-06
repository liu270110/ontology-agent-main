import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, ChevronRight, CircleCheck, CircleX, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { Modal } from '@/components/modal'
import {
  bindAgentTools,
  createAgent,
  listAdapterSchemas,
  testConnection,
  type AdapterSchemaDef,
  type ConnectionTestResult,
} from '../api'
import { JsonSchemaForm } from './JsonSchemaForm'
import { ToolPickerPanel } from './ToolPickerModal'

/** IX-AGT-01 注册 Agent 向导（26 篇 §8.2；画板 ix-agt-01）：3 步 680px 强制顺序——
 *  ①适配器卡（live 键集=builtin/claude，GET /agents/adapter-schemas 下发；custom 键不在
 *    后端 ^(builtin|claude)$ 词表，卡片随 live 收敛移除——不提供必失败的入口）
 *  ②接入参数：JsonSchemaForm 按适配器 config schema 渲染（model/temperature/
 *    tool_whitelist/num_ctx，=领域 _CONFIG_KEYS）+ 连接测试（POST /agents/connection-test
 *    live 契约：provider/base_url/api_key?/model——一次最小补全，失败结构化 200 ok=false；
 *    HTTP 级失败=门禁 403/校验 422，catch ApiError 透出）
 *  ③工具注入：ToolPicker 内嵌（IX-AGT-03）+ 汇总确认。
 *  完成 → POST /agents {name,agent_tool,system_prompt?,config?} → 201 →
 *  勾选工具经 PUT /agents/{id}/tools 覆盖式写白名单 → 卡片列表新实例（status=enabled）。 */

const STEPS = ['① 适配器选择', '② 接入参数', '③ 工具注入'] as const

type TestState = 'idle' | 'testing' | 'success' | 'fail'

/** 连接测试目标（live ConnectionTestIn：provider 当前仅登记=唯一 OpenAI 兼容通道） */
const PROVIDER = 'openai_compatible'

export function RegisterWizard({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const [step, setStep] = useState(0)
  const [adapter, setAdapter] = useState<AdapterSchemaDef | null>(null)
  const [form, setForm] = useState<Record<string, unknown>>({})
  const [testState, setTestState] = useState<TestState>('idle')
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null)
  const [testErrMsg, setTestErrMsg] = useState<string | null>(null)
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [tools, setTools] = useState<string[]>([])
  const [name, setName] = useState('')

  const schemas = useQuery({ queryKey: ['agents', 'adapter-schemas'], queryFn: listAdapterSchemas, enabled: open })

  const create = useMutation({
    mutationFn: async () => {
      // live 契约：POST /agents {name, agent_tool, system_prompt?, config?}（config 键集
      // =适配器 schema 字段）→ 201 AgentOut；勾选工具再覆盖式写白名单
      const agent = await createAgent({
        name: name || `${adapter?.key}-instance`,
        agent_tool: adapter!.key as 'builtin' | 'claude',
        config: form,
      })
      if (tools.length > 0) await bindAgentTools(agent.id, tools)
      return agent
    },
    onSuccess: agent => {
      void qc.invalidateQueries({ queryKey: ['agents'] })
      toast.success(`已注册「${agent.name}」`, {
        description: `状态 · 已启用${tools.length ? `；白名单 ${tools.length} 个工具经 PUT /tools 覆盖式写入` : ''}`,
      })
      onClose()
    },
    onError: e => {
      toast.error('Agent 注册失败', { description: e instanceof Error ? e.message : undefined })
    },
  })

  function reset() {
    setStep(0)
    setAdapter(null)
    setForm({})
    setTestState('idle')
    setTestResult(null)
    setTestErrMsg(null)
    setBaseUrl('')
    setApiKey('')
    setTools([])
    setName('')
  }

  async function runTest() {
    setTestState('testing')
    setTestErrMsg(null)
    try {
      const res = await testConnection({
        provider: PROVIDER,
        base_url: baseUrl,
        api_key: apiKey.trim() || undefined, // 缺省=本地渠道占位 EMPTY（后端口径）
        model: String(form.model ?? ''),
      })
      setTestResult(res)
      setTestState(res.ok ? 'success' : 'fail')
    } catch (e) {
      // HTTP 级失败（403 门禁 / 422 缺 model 等）：结构化错误文案透出，不上 500 假设
      setTestResult(null)
      setTestErrMsg(e instanceof ApiError ? `${e.code} ${e.message}` : e instanceof Error ? e.message : '连接测试失败')
      setTestState('fail')
    }
  }

  function next() {
    if (step === 0 && !adapter) return
    if (step === 1 && testState !== 'success') return // 连接测试过后才能进入下一步
    setStep(s => Math.min(2, s + 1))
  }

  const testPulse = testState === 'testing'
  const canTest = !!baseUrl.trim() && !!String(form.model ?? '').trim()

  return (
    <Modal
      open={open}
      onClose={() => {
        onClose()
        reset()
      }}
      title="注册 Agent"
      width={680}
      footer={
        <>
          <button
            type="button"
            className="btn btn-g btn-sm whitespace-nowrap"
            onClick={() => {
              onClose()
              reset()
            }}
          >
            取消
          </button>
          {step > 0 && (
            <button type="button" className="btn btn-g btn-sm whitespace-nowrap" onClick={() => setStep(s => s - 1)}>
              上一步 · {STEPS[step - 1]}
            </button>
          )}
          <span className="mx-auto text-[11px] text-label-3">
            {step === 0 && '选择适配器后进入接入参数'}
            {step === 1 && '下一步进入 ToolPicker 嵌入与汇总确认'}
            {step === 2 && `汇总：${adapter?.name} · ${String(form.model ?? '')} · ${tools.length} 个工具`}
          </span>
          {step < 2 ? (
            <button
              type="button"
              className="btn btn-p btn-sm whitespace-nowrap"
              data-testid="agt-wiz-next"
              disabled={(step === 0 && !adapter) || (step === 1 && testState !== 'success')}
              onClick={next}
            >
              下一步 · {STEPS[step + 1].slice(2)} <ChevronRight size={12} aria-hidden />
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-p btn-sm whitespace-nowrap"
              data-testid="agt-wiz-create"
              disabled={create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? '创建中…' : '完成注册'}
            </button>
          )}
        </>
      }
    >
      <p className="text-xs text-label-3">三步向导 · 步骤强制顺序，不可跳过；agent_tool 仅 builtin / claude（后端词表）</p>

      {/* 步骤条 */}
      <div className="steps mt-3" aria-label="注册向导步骤条">
        {STEPS.map((s, i) => (
          <div key={s} className={`step ${i < step ? 'done' : i === step ? 'cur' : ''}`}>
            <span className="s-dot">{i < step ? '✓' : ''}</span>
            <span className="s-name">{s}</span>
          </div>
        ))}
      </div>

      {step === 0 && (
        <div className="mt-4 grid grid-cols-2 gap-3">
          {(schemas.data?.items ?? []).map(a => {
            const selected = adapter?.key === a.key
            return (
              <button
                type="button"
                key={a.key}
                aria-pressed={selected}
                aria-label={`${a.name} 适配器`}
                data-testid={`agt-adapter-${a.key}`}
                onClick={() => setAdapter(a)}
                className="rounded-xl border p-3.5 text-left transition-colors"
                style={{
                  borderColor: selected ? 'var(--accent)' : 'var(--separator)',
                  boxShadow: selected ? 'var(--glow-accent)' : 'none',
                  background: 'var(--surface)',
                }}
              >
                <div className="flex items-center gap-2">
                  <Bot size={14} aria-hidden />
                  <b className="text-[13px]">{a.name}</b>
                  {selected && <span className="badge b-blue">已选</span>}
                  <span className="badge b-gray ml-auto">{a.vendor.split('·')[0]?.trim()}</span>
                </div>
                <div className="mt-1.5 text-[11px] leading-4 text-label-2">{a.capability}</div>
              </button>
            )
          })}
          {schemas.isLoading && <div className="col-span-2 text-[11px] text-label-3">适配器清单加载中…</div>}
        </div>
      )}

      {step === 1 && adapter && (
        <div className="mt-4">
          {/* 已选适配器条 */}
          <div className="flex items-center gap-2 rounded-xl border border-separator bg-surface-2 px-3 py-2">
            <Bot size={14} aria-hidden />
            <b className="text-xs">{adapter.name}</b>
            <span className="badge b-blue">已选</span>
            <span className="truncate text-[11px] text-label-3">{adapter.vendor} · {adapter.capability}</span>
          </div>

          {/* RJSF：按适配器 config schema 渲染（model / temperature / tool_whitelist / num_ctx） */}
          <div className="mt-3">
            <div className="field">
              <label className="field-label" htmlFor="agt-inst-name">
                实例名称
              </label>
              <input
                id="agt-inst-name"
                className="input"
                data-testid="agt-inst-name"
                placeholder={`示例：${adapter.name} 实例`}
                value={name}
                onChange={e => setName(e.target.value)}
              />
            </div>
            <JsonSchemaForm
              schema={adapter.schema}
              formData={form}
              onChange={data => {
                setForm(data)
                setTestState('idle')
                setTestResult(null)
                setTestErrMsg(null)
              }}
            />
          </div>

          <div className="fhint -mt-1 mb-2">
            config schema：{(adapter.schema as { properties?: Record<string, unknown> }).properties
              ? Object.keys((adapter.schema as { properties: Record<string, unknown> }).properties).join(' / ')
              : '—'}（领域 _CONFIG_KEYS 同键；校验走 ajv8）
          </div>

          {/* 连接测试（live 契约：provider/base_url/api_key?/model——先测后注册） */}
          <div className="grid grid-cols-2 gap-3">
            <div className="field mb-2">
              <label className="field-label" htmlFor="agt-conn-base-url">
                连接测试 · 基座端点 base_url
              </label>
              <input
                id="agt-conn-base-url"
                className="input mono"
                data-testid="agt-conn-base-url"
                placeholder="https://llm.example.com/v1"
                value={baseUrl}
                onChange={e => {
                  setBaseUrl(e.target.value)
                  setTestState('idle')
                  setTestResult(null)
                }}
              />
            </div>
            <div className="field mb-2">
              <label className="field-label" htmlFor="agt-conn-api-key">
                API Key（可选 · 缺省占位 EMPTY）
              </label>
              <input
                id="agt-conn-api-key"
                className="input mono"
                type="password"
                data-testid="agt-conn-api-key"
                placeholder="sk-..."
                value={apiKey}
                onChange={e => setApiKey(e.target.value)}
              />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="btn btn-s btn-sm"
              data-testid="agt-conn-test"
              disabled={!canTest || testPulse}
              title={canTest ? undefined : '需填写 base_url，且 config 中已填 model（后端 422 校验）'}
              onClick={() => void runTest()}
            >
              {testPulse ? <Loader2 size={12} className="animate-spin" aria-hidden /> : <PlugTestIcon />} 连接测试
            </button>
            <span className="text-[11px] text-label-3">
              一次最小补全探测（provider=openai_compatible · model 取 config.model）；测试通过后才能进入下一步
            </span>
          </div>

          <div className="mt-2 grid grid-cols-3 gap-2" aria-label="连接测试三态">
            <div className="rounded-xl border px-3 py-2.5" style={{ background: testState === 'testing' ? 'var(--accent-soft)' : 'var(--surface)', borderColor: testState === 'testing' ? 'var(--accent)' : 'var(--separator)' }}>
              <div className={`flex items-center gap-1.5 text-[11px] font-semibold ${testState === 'testing' ? 'text-accent' : 'text-label-2'}`}>
                {testPulse ? <Loader2 size={12} className="animate-spin" aria-hidden /> : null} 态 1 · 测试中
              </div>
              <div className="mt-1 text-[11px] text-label-3">最小补全握手中，限时上限 10s</div>
            </div>
            <div className="rounded-xl border px-3 py-2.5" data-testid="agt-conn-success" style={{ background: testState === 'success' ? 'var(--green-soft)' : 'var(--surface)', borderColor: testState === 'success' ? 'var(--green)' : 'var(--separator)' }}>
              <div className={`flex items-center gap-1.5 text-[11px] font-semibold ${testState === 'success' ? 'text-green' : 'text-label-2'}`}>
                <CircleCheck size={12} aria-hidden /> 态 2 · 测试通过
              </div>
              <div className="mt-1 text-[11px] text-label-3">
                {testState === 'success' && testResult ? `延迟 ${testResult.latency_ms}ms · model ${testResult.model}` : '延迟与 model 回显'}
              </div>
            </div>
            <div className="rounded-xl border px-3 py-2.5" data-testid="agt-conn-fail" style={{ background: testState === 'fail' ? 'var(--red-soft)' : 'var(--surface)', borderColor: testState === 'fail' ? 'var(--red)' : 'var(--separator)' }}>
              <div className={`flex items-center gap-1.5 text-[11px] font-semibold ${testState === 'fail' ? 'text-red' : 'text-label-2'}`}>
                <CircleX size={12} aria-hidden /> 态 3 · 测试失败
              </div>
              <div className="mt-1 text-[11px] text-label-3">
                {testState === 'fail' && (testResult?.error || testErrMsg)
                  ? (testResult?.error ?? testErrMsg)
                  : '检查 base_url / 凭据 / model 可用性'}
              </div>
            </div>
          </div>
        </div>
      )}

      {step === 2 && adapter && (
        <div className="mt-4">
          <div className="mb-3 rounded-xl border border-separator bg-surface-2 px-3 py-2.5 text-[11px] leading-5">
            <b>汇总确认</b> · 适配器「{adapter.name}」· model <span className="mono">{String(form.model ?? '—')}</span>
            {' '}· temperature <span className="mono">{String(form.temperature ?? '—')}</span> · 注入工具 {tools.length} 个
            <div className="text-label-3">
              完成 → POST /agents（agent_tool={adapter.key}）→ 勾选工具经 PUT /agents/&#123;id&#125;/tools 覆盖式写白名单 →
              列表新实例（状态 · 已启用）。
            </div>
          </div>
          <ToolPickerPanel selected={tools} onChange={setTools} />
        </div>
      )}
    </Modal>
  )
}

function PlugTestIcon() {
  return <Bot size={12} aria-hidden />
}
