import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, ChevronRight, CircleCheck, CircleX, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import {
  createAgent,
  listAdapterSchemas,
  testConnection,
  type AdapterSchemaDef,
  type ConnectionTestResult,
} from '../api'
import { JsonSchemaForm } from './JsonSchemaForm'
import { ToolPickerPanel } from './ToolPickerModal'

/** IX-AGT-01 注册 Agent 向导（26 篇 §8.2；画板 ix-agt-01）：3 步 680px 强制顺序——
 *  ①适配器卡（nanobot/openclaw/hermes/自定义：来源徽标+能力摘要）
 *  ②接入参数：JsonSchemaForm 按适配器 Schema 渲染（endpoint/token 脱敏/超时）
 *    + 连接测试三态（进行中脉冲 / 成功绿 RTT / 失败红诊断）；
 *  ③工具注入：ToolPicker 内嵌（IX-AGT-03）+ 汇总确认。
 *  完成 → POST /agents（api/01 §5.1）→ 卡片列表新实例（状态·已停止）。 */

const STEPS = ['① 适配器选择', '② 接入参数', '③ 工具注入'] as const

type TestState = 'idle' | 'testing' | 'success' | 'fail'

export function RegisterWizard({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const [step, setStep] = useState(0)
  const [adapter, setAdapter] = useState<AdapterSchemaDef | null>(null)
  const [form, setForm] = useState<Record<string, unknown>>({})
  const [testState, setTestState] = useState<TestState>('idle')
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null)
  const [tools, setTools] = useState<string[]>([])
  const [name, setName] = useState('')

  const schemas = useQuery({ queryKey: ['agents', 'adapter-schemas'], queryFn: listAdapterSchemas, enabled: open })

  const create = useMutation({
    mutationFn: () =>
      createAgent({
        name: name || `${adapter?.name}-instance`,
        adapter: adapter!.key,
        endpoint: String(form.endpoint ?? ''),
        token: String(form.token ?? ''),
        timeout_ms: Number(form.timeout_ms ?? 30000),
        tools,
      }),
    onSuccess: agent => {
      void qc.invalidateQueries({ queryKey: ['agents'] })
      toast.success(`已注册「${agent.name}」`, { description: '新实例状态 · 已停止；启动前自动健康自检' })
      onClose()
    },
  })

  function reset() {
    setStep(0)
    setAdapter(null)
    setForm({})
    setTestState('idle')
    setTestResult(null)
    setTools([])
    setName('')
  }

  async function runTest() {
    setTestState('testing')
    const res = await testConnection({
      adapter: adapter?.key ?? 'custom',
      endpoint: String(form.endpoint ?? ''),
      token: String(form.token ?? ''),
    })
    setTestResult(res)
    setTestState(res.ok ? 'success' : 'fail')
  }

  function next() {
    if (step === 0 && !adapter) return
    if (step === 1 && testState !== 'success') return // 连接测试过后才能进入下一步
    setStep(s => Math.min(2, s + 1))
  }

  const testPulse = testState === 'testing'

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
            {step === 2 && `汇总：${adapter?.name} · ${String(form.endpoint ?? '')} · ${tools.length} 个工具`}
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
      <p className="text-xs text-label-3">三步向导 · 步骤强制顺序，不可跳过；token 仅脱敏显示</p>

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
          <button
            type="button"
            aria-pressed={adapter?.key === 'custom'}
            aria-label="自定义适配器"
            onClick={() => setAdapter({ key: 'custom', name: '自定义', vendor: '自定义', capability: '提供 endpoint 与令牌的自定义接入', schema: {} })}
            className="rounded-xl border p-3.5 text-left transition-colors"
            style={{
              borderColor: adapter?.key === 'custom' ? 'var(--accent)' : 'var(--separator)',
              background: 'var(--surface)',
            }}
          >
            <div className="flex items-center gap-2">
              <Bot size={14} aria-hidden />
              <b className="text-[13px]">自定义</b>
            </div>
            <div className="mt-1.5 text-[11px] text-label-2">手动填写 endpoint 与令牌（Schema 由后端适配器提供）</div>
          </button>
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

          {/* RJSF：按适配器 Schema 渲染（endpoint / token / timeout_ms） */}
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
              }}
            />
          </div>

          <div className="fhint -mt-1 mb-2">
            Schema：{adapter.name}-adapter {adapter.key === 'custom' ? 'v0' : 'v1.3'} · 必填 {(adapter.schema as { required?: string[] }).required?.length ?? 0} 项 · 校验走 ajv8（RJSF 引擎）
          </div>

          {/* 连接测试三态 */}
          <div className="flex items-center gap-2">
            <button type="button" className="btn btn-s btn-sm" data-testid="agt-conn-test" onClick={() => void runTest()}>
              {testPulse ? <Loader2 size={12} className="animate-spin" aria-hidden /> : <PlugTestIcon />} 连接测试
            </button>
            <span className="text-[11px] text-label-3">测试通过后才能进入下一步；token 仅脱敏显示</span>
          </div>

          <div className="mt-2 grid grid-cols-3 gap-2" aria-label="连接测试三态">
            <div className="rounded-xl border px-3 py-2.5" style={{ background: testState === 'testing' ? 'var(--accent-soft)' : 'var(--surface)', borderColor: testState === 'testing' ? 'var(--accent)' : 'var(--separator)' }}>
              <div className={`flex items-center gap-1.5 text-[11px] font-semibold ${testState === 'testing' ? 'text-accent' : 'text-label-2'}`}>
                {testPulse ? <Loader2 size={12} className="animate-spin" aria-hidden /> : null} 态 1 · 测试中
              </div>
              <div className="mt-1 text-[11px] text-label-3">RPC 握手中，限时上限 10s</div>
            </div>
            <div className="rounded-xl border px-3 py-2.5" data-testid="agt-conn-success" style={{ background: testState === 'success' ? 'var(--green-soft)' : 'var(--surface)', borderColor: testState === 'success' ? 'var(--green)' : 'var(--separator)' }}>
              <div className={`flex items-center gap-1.5 text-[11px] font-semibold ${testState === 'success' ? 'text-green' : 'text-label-2'}`}>
                <CircleCheck size={12} aria-hidden /> 态 2 · 测试通过
              </div>
              <div className="mt-1 text-[11px] text-label-3">
                {testState === 'success' && testResult ? `RTT ${testResult.rtt_ms}ms · 协议 ${testResult.protocol} · ${testResult.message}` : 'RTT 与能力清单回显'}
              </div>
            </div>
            <div className="rounded-xl border px-3 py-2.5" data-testid="agt-conn-fail" style={{ background: testState === 'fail' ? 'var(--red-soft)' : 'var(--surface)', borderColor: testState === 'fail' ? 'var(--red)' : 'var(--separator)' }}>
              <div className={`flex items-center gap-1.5 text-[11px] font-semibold ${testState === 'fail' ? 'text-red' : 'text-label-2'}`}>
                <CircleX size={12} aria-hidden /> 态 3 · 测试失败
              </div>
              <div className="mt-1 text-[11px] text-label-3">
                {testState === 'fail' && testResult ? `${testResult.code} ${testResult.message}` : 'E-5003 · 检查 endpoint 与白名单'}
              </div>
            </div>
          </div>
        </div>
      )}

      {step === 2 && adapter && (
        <div className="mt-4">
          <div className="mb-3 rounded-xl border border-separator bg-surface-2 px-3 py-2.5 text-[11px] leading-5">
            <b>汇总确认</b> · 适配器「{adapter.name}」· endpoint <span className="mono">{String(form.endpoint ?? '—')}</span>
            {form.token ? (
              <>
                {' '}· token <span className="mono">****</span>
              </>
            ) : null} · 超时 {String(form.timeout_ms ?? 30000)}ms · 注入工具 {tools.length} 个
            <div className="text-label-3">完成 → POST /agents → 卡片列表新实例（状态 · 已停止，启动时先跑健康自检）。</div>
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
