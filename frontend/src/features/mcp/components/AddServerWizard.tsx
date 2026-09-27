import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronRight, CircleCheck, Loader2, Plug } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { discoverServer, registerServer, type DiscoveredTool, type McpTransport } from '../api'

/** IX-MCP-01 接入向导（26 篇 §9.3；画板 ix-mcp-01）：3 步 680px——
 *  ①连接配置：名称 + 传输分段（Streamable HTTP / stdio）+ URL/命令 + 鉴权（Bearer/Basic）
 *  ②连接测试与发现：测试按钮（进行中脉冲/成功列出工具清单/失败错误诊断）+ 工具清单勾选
 *    纳管（默认全选）+ 写入类「需审批」徽标 + 默认不可信警示；
 *  ③确认摘要（Server 信息 + 纳管工具数）→ POST /mcp/servers → 列表新行（健康·未知 → 首次探活）。 */

const STEPS = ['① 连接配置', '② 连接测试与发现', '③ 确认摘要'] as const

type TestOk = { ok: true; latency_ms: number; protocol: string; server_version: string; tools: DiscoveredTool[] }
type TestedState = TestOk | { ok: false } | null

export function AddServerWizard({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const [step, setStep] = useState(0)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [transport, setTransport] = useState<McpTransport>('streamable http')
  const [url, setUrl] = useState('')
  const [command, setCommand] = useState('')
  const [auth, setAuth] = useState('Bearer Token')
  const [token, setToken] = useState('')
  const [testing, setTesting] = useState(false)
  const [tested, setTested] = useState<TestedState>(null)
  const [adopted, setAdopted] = useState<Set<string>>(new Set())

  const create = useMutation({
    mutationFn: () =>
      registerServer({
        name,
        desc,
        transport,
        url: transport === 'streamable http' ? url : undefined,
        command: transport === 'stdio' ? command : undefined,
        auth,
        token,
        adopt_tool_ids: [...adopted],
      }),
    onSuccess: server => {
      void qc.invalidateQueries({ queryKey: ['mcp'] })
      toast.success(`已接入 ${server.name}`, {
        description: `纳管 ${server.adopted_count} 个工具（默认「未启用」，写入类需逐项审核开启）；健康 · 未知 → 首次探活`,
      })
      onClose()
    },
  })

  async function runTest() {
    setTesting(true)
    setTested(null)
    const res = await discoverServer({ name, transport, url, command, auth, token })
    setTesting(false)
    setTested(res)
    if (res.ok) setAdopted(new Set(res.tools.map(t => t.tool_id))) // 全选默认开
  }

  function reset() {
    setStep(0)
    setName('')
    setDesc('')
    setTransport('streamable http')
    setUrl('')
    setCommand('')
    setAuth('Bearer Token')
    setToken('')
    setTesting(false)
    setTested(null)
    setAdopted(new Set())
  }

  const canNextStep0 = name.trim() && (transport === 'streamable http' ? /^https?:\/\//.test(url) : !!command.trim())
  const allAdopted = tested?.ok ? adopted.size === tested.tools.length : false

  return (
    <Modal
      open={open}
      onClose={() => {
        onClose()
        reset()
      }}
      title="接入 MCP Server"
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
              上一步
            </button>
          )}
          <span className="mx-auto text-[11px] text-label-3">
            {step === 1 && '写入类可先取消勾选缩小授权面；第③步生成纳管摘要 → 完成后 /mcp 列表新增一行'}
            {step === 0 && '连接测试成功才可下一步'}
            {step === 2 && `纳管 ${adopted.size} 个工具`}
          </span>
          {step < 2 ? (
            <button
              type="button"
              className="btn btn-p btn-sm whitespace-nowrap"
              data-testid="mcp-wiz-next"
              disabled={(step === 0 && !canNextStep0) || (step === 1 && !tested?.ok)}
              onClick={() => setStep(s => s + 1)}
            >
              下一步 · {STEPS[step + 1].slice(2)} <ChevronRight size={12} aria-hidden />
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-p btn-sm whitespace-nowrap"
              data-testid="mcp-wiz-create"
              disabled={create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? '注册中…' : `完成接入（纳管 ${adopted.size} 个工具）`}
            </button>
          )}
        </>
      }
    >
      <p className="text-xs text-label-3">外部 Server 默认不可信：能力经发现后逐项纳管，调用统一经 L7 MCP 网关</p>

      <div className="steps mt-3" aria-label="接入向导步骤条">
        {STEPS.map((s, i) => (
          <div key={s} className={`step ${i < step ? 'done' : i === step ? 'cur' : ''}`}>
            <span className="s-dot">{i < step ? '✓' : ''}</span>
            <span className="s-name">{s}</span>
          </div>
        ))}
      </div>

      {step === 0 && (
        <div className="mt-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="field mb-2">
              <label className="field-label" htmlFor="mcp-name">
                名称
              </label>
              <input
                id="mcp-name"
                className="input mono"
                data-testid="mcp-name"
                placeholder="crm-prod"
                value={name}
                onChange={e => setName(e.target.value)}
              />
            </div>
            <div className="field mb-2">
              <label className="field-label" htmlFor="mcp-desc">
                描述（可选）
              </label>
              <input id="mcp-desc" className="input" placeholder="客服工单系统" value={desc} onChange={e => setDesc(e.target.value)} />
            </div>
          </div>
          <div className="field mb-2">
            <span className="field-label">传输方式</span>
            <div className="seg" role="radiogroup" aria-label="传输方式">
              <button
                type="button"
                role="radio"
                aria-checked={transport === 'streamable http'}
                className={`seg-btn ${transport === 'streamable http' ? 'on' : ''}`}
                data-testid="mcp-transport-http"
                onClick={() => setTransport('streamable http')}
              >
                Streamable HTTP
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={transport === 'stdio'}
                className={`seg-btn ${transport === 'stdio' ? 'on' : ''}`}
                data-testid="mcp-transport-stdio"
                onClick={() => setTransport('stdio')}
              >
                stdio
              </button>
            </div>
          </div>
          {transport === 'streamable http' ? (
            <div className="field mb-2">
              <label className="field-label" htmlFor="mcp-url">
                URL
              </label>
              <input
                id="mcp-url"
                className="input mono"
                data-testid="mcp-url"
                placeholder="https://crm-prod.example.com/mcp"
                value={url}
                onChange={e => setUrl(e.target.value)}
              />
            </div>
          ) : (
            <div className="field mb-2">
              <label className="field-label" htmlFor="mcp-command">
                启动命令
              </label>
              <input
                id="mcp-command"
                className="input mono"
                data-testid="mcp-command"
                placeholder="npx @modelcontextprotocol/server-github"
                value={command}
                onChange={e => setCommand(e.target.value)}
              />
            </div>
          )}
          <div className="grid grid-cols-2 gap-3">
            <div className="field mb-0">
              <label className="field-label" htmlFor="mcp-auth">
                鉴权
              </label>
              <select id="mcp-auth" className="input" value={auth} onChange={e => setAuth(e.target.value)}>
                <option>Bearer Token</option>
                <option>Basic</option>
                <option>无鉴权（内网白名单）</option>
              </select>
            </div>
            {auth !== '无鉴权（内网白名单）' && (
              <div className="field mb-0">
                <label className="field-label" htmlFor="mcp-token">
                  凭据（仅脱敏回显）
                </label>
                <input
                  id="mcp-token"
                  className="input mono"
                  type="password"
                  data-testid="mcp-token"
                  placeholder="sk-..."
                  value={token}
                  onChange={e => setToken(e.target.value)}
                />
              </div>
            )}
          </div>
        </div>
      )}

      {step === 1 && (
        <div className="mt-4">
          {/* 连接配置回显 */}
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 rounded-xl border border-separator bg-surface-2 px-4 py-3 text-[11px]">
            <div><span className="text-label-3">名称　</span><b>{name || '—'}</b></div>
            <div><span className="text-label-3">传输　</span>{transport}</div>
            <div><span className="text-label-3">URL　　</span><span className="mono">{transport === 'streamable http' ? url || '—' : command || '—'}</span></div>
            <div><span className="text-label-3">鉴权　</span>{auth}{token ? `（${'*'.repeat(8)}）` : ''}</div>
          </div>

          {/* 连接测试 */}
          <div className="mt-3 flex items-center gap-2">
            <button type="button" className="btn btn-s btn-sm" data-testid="mcp-discover-go" onClick={() => void runTest()}>
              {testing ? <Loader2 size={12} className="animate-spin" aria-hidden /> : <Plug size={12} aria-hidden />}
              {tested?.ok ? '重新测试' : '连接测试与发现'}
            </button>
            <span className="text-[11px] text-label-3">测试成功才可下一步；失败时在此给出诊断建议（网络 / 鉴权 / 协议不匹配）</span>
          </div>

          {testing && (
            <div className="mt-2 rounded-xl px-3 py-2.5 text-[11px]" style={{ background: 'var(--accent-soft)' }}>
              <Loader2 size={12} className="mr-1 inline animate-spin" aria-hidden /> tools/list 握手中，限时上限 10s…
            </div>
          )}

          {tested && !testing && !tested.ok && (
            <div className="mt-2 rounded-xl px-3 py-2.5 text-[11px]" style={{ background: 'var(--red-soft)' }} data-testid="mcp-discover-fail">
              <b className="text-red">测试失败</b> · E-5003 未收到 tools/list 响应 · 建议：检查网络连通、凭据与协议版本匹配
            </div>
          )}

          {tested?.ok && (
            <div className="mt-2 rounded-xl px-3.5 py-3" style={{ background: 'var(--green-soft)' }} data-testid="mcp-discover-ok">
              <div className="flex items-center gap-1.5 text-xs font-semibold text-green">
                <CircleCheck size={13} aria-hidden /> 连接测试成功
                <button type="button" className="btn btn-g btn-sm ml-auto" onClick={() => void runTest()}>
                  重新测试
                </button>
              </div>
              <div className="mt-0.5 text-[11px] text-label-2">
                延迟 {tested.latency_ms}ms · 协议 {tested.protocol} · {tested.server_version} · 发现工具 {tested.tools.length} 项
              </div>
            </div>
          )}

          {/* 工具清单勾选纳管（全选默认开） */}
          {tested?.ok && (
            <div className="mt-3 rounded-xl border border-separator px-3 py-2.5" style={{ background: 'var(--surface)' }}>
              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  aria-label="全选纳管工具"
                  data-testid="mcp-adopt-all"
                  checked={allAdopted}
                  onChange={e => setAdopted(e.target.checked ? new Set(tested.tools.map(t => t.tool_id)) : new Set())}
                />
                <b className="text-xs">全选</b>
                <span className="badge b-blue">已选 {adopted.size} / {tested.tools.length}</span>
                <span className="mono ml-auto text-2xs text-label-3">tools/list · {tested.latency_ms}ms 返回</span>
              </div>
              <div className="mt-1.5 divide-y" style={{ borderColor: 'var(--separator)' }}>
                {tested.tools.map(t => (
                  <div key={t.tool_id} className="flex items-center gap-2 py-1.5 text-[11px]">
                    <input
                      type="checkbox"
                      aria-label={`纳管 ${t.name}`}
                      data-testid={`mcp-adopt-${t.name}`}
                      checked={adopted.has(t.tool_id)}
                      onChange={e =>
                        setAdopted(prev => {
                          const next = new Set(prev)
                          if (e.target.checked) next.add(t.tool_id)
                          else next.delete(t.tool_id)
                          return next
                        })
                      }
                    />
                    <span className="mono font-semibold">{t.name}</span>
                    <span className="truncate text-label-3">{t.desc}</span>
                    {t.write ? <span className="badge b-orange ml-auto">写入 · 需审批</span> : <span className="badge b-gray ml-auto">只读</span>}
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="mt-3 rounded-xl px-3.5 py-2.5" style={{ background: 'var(--orange-soft)' }}>
            <b className="text-[11px] text-orange">外部工具默认不可信</b>
            <p className="mt-0.5 text-[11px] leading-4 text-label-2">
              纳管后以「未启用」进注册中心，写入类需逐项审核开启（POST /mcp/tools/&#123;tool_id&#125;/enable）；annotations 仅作提示，不作授权依据。
            </p>
          </div>
        </div>
      )}

      {step === 2 && tested?.ok && (
        <div className="mt-4 rounded-xl border border-separator bg-surface-2 px-4 py-3 text-xs leading-6">
          <div><b>Server：</b>{name}（{transport}）· {transport === 'streamable http' ? url : command}</div>
          <div><b>鉴权：</b>{auth}{token ? ' · 凭据已脱敏存管' : ''}</div>
          <div><b>协议 / 版本：</b>{tested.protocol} · {tested.server_version} · 发现延迟 {tested.latency_ms}ms</div>
          <div><b>纳管工具：</b>{adopted.size} / {tested.tools.length} 项（默认未启用；写入类需逐项审批开启）</div>
          <div className="text-label-3">完成 → POST /mcp/servers → 列表新增一行（健康 · 未知 → 首次探活）。</div>
        </div>
      )}
      {step === 2 && (
        <div className="mt-2 flex items-center gap-1.5 text-[11px] text-label-3">
          <Check size={11} aria-hidden /> 注册动作写入审计（trace_id 落 admin 审计日志）
        </div>
      )}
    </Modal>
  )
}
