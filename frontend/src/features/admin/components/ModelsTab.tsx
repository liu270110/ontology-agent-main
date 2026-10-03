import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Plus, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/states'
import { Modal } from '@/components/modal'
import { Select } from '@/components/select'
import {
  createModelChannel, deleteModelChannel, getModelImpact, listModels, testModelChannel,
  type ConnectivityResult, type ModelChannel,
} from '../api'

/** 模型渠道 Tab（26 篇 §10.2 p-admin models）：渠道表 + IX-ADM-05 接入渠道
 *  （提供商三卡 + 密钥脱敏 + 连通性测试区三态，成功才可保存）+ IX-ADM-06 删除渠道
 *  （danger：级联影响 + 迁移建议 + 输名确认）。
 *  表单选型记录：未复用 JsonSchemaForm/RJSF——该表单为「提供商卡 + 测试门控」自定义形态，
 *  非schema 驱动 payload 表单，采用受控组件自建（与 agents 域向导同款做法）。 */

const PROVIDERS = [
  { key: 'deepseek', label: 'DeepSeek 云', desc: 'OpenAI 兼容 · 推理性价比高', hint: 'sk-ds-…' },
  { key: 'qwen', label: 'Qwen 云', desc: '百炼 / DashScope · 套餐模式', hint: 'sk-qw-…' },
  { key: 'ollama', label: 'Ollama 本地', desc: '内网离线 · 数据不出域', hint: '本地无需密钥' },
] as const

export function ModelsTab() {
  const [addOpen, setAddOpen] = useState(false)
  const [deleting, setDeleting] = useState<ModelChannel | null>(null)
  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ['admin', 'models'], queryFn: listModels })
  // fe3 信封收口：listModels 改 api.list 归一（{data,meta}），读 .data
  const channels = useMemo(() => data?.data ?? [], [data])

  return (
    <div>
      <div className="flex items-center gap-2">
        <span className="text-xs text-label-2">路由顺序：本地默认 → 云溢出按优先级 → 降级备用；密钥只写哈希与长度</span>
        <button type="button" className="btn btn-p btn-sm ml-auto" data-testid="adm-model-open" onClick={() => setAddOpen(true)}>
          <Plus size={13} aria-hidden /> 接入渠道
        </button>
      </div>

      <div className="card mt-3 overflow-x-auto !p-0">
        <table className="w-full min-w-[760px] text-xs">
          <thead>
            <tr className="hairline-b text-left text-[11px] text-label-3">
              <th className="px-4 py-2.5 font-semibold">渠道</th>
              <th className="px-4 py-2.5 font-semibold">提供商</th>
              <th className="px-4 py-2.5 font-semibold">模型</th>
              <th className="px-4 py-2.5 font-semibold">优先级</th>
              <th className="px-4 py-2.5 font-semibold">预算上限</th>
              <th className="px-4 py-2.5 font-semibold">近 30 天用量</th>
              <th className="px-4 py-2.5 font-semibold text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {/* 状态完备：成功空列表 → 空态行（与加载/错误态互斥门控） */}
            {!isLoading && !isError && channels.length === 0 && (
              <tr>
                <td colSpan={7}>
                  <EmptyState compact title="还没有模型渠道" desc="接入第一个渠道后，Agent 插槽即可绑定模型。" />
                </td>
              </tr>
            )}
            {channels.map(c => (
              <tr key={c.id} className="hairline-b" data-testid={`adm-model-${c.id}`}>
                <td className="px-4 py-2.5"><b>{c.name}</b><span className="mono block text-2xs text-label-3">{c.api_key_masked}</span></td>
                <td className="px-4 py-2.5">{c.provider_label}</td>
                <td className="px-4 py-2.5">{c.models.map(m => <span key={m} className="mono badge b-gray mr-1">{m}</span>)}</td>
                <td className="px-4 py-2.5">P{c.priority}</td>
                <td className="px-4 py-2.5">{c.budget_daily ? `¥${c.budget_daily} / 日` : '不限'}</td>
                <td className="px-4 py-2.5 text-label-2">{c.usage_30d}</td>
                <td className="px-4 py-2.5 text-right">
                  <button type="button" className="btn btn-d btn-sm" data-testid={`adm-model-del-${c.id}`} onClick={() => setDeleting(c)}>
                    <Trash2 size={12} aria-hidden /> 删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {/* S8 状态切片：加载骨架行 / 错误态在表格外（重试=refetch） */}
        {isLoading && (
          <div className="px-4 py-3">
            <SkeletonRows rows={5} rowHeight={32} />
          </div>
        )}
      </div>

      {isError && (
        <div className="mt-3">
          <ErrorState
            message={error instanceof Error ? error.message : undefined}
            code={error instanceof ApiError ? error.code : undefined}
            onRetry={() => void refetch()}
          />
        </div>
      )}

      {addOpen && <AddChannelModal onClose={() => setAddOpen(false)} />}
      {deleting && <DeleteChannelModal channel={deleting} onClose={() => setDeleting(null)} />}
    </div>
  )
}

type TestState =
  | { phase: 'idle' }
  | { phase: 'testing' }
  | { phase: 'ok'; result: ConnectivityResult }
  | { phase: 'fail'; message: string }

/** IX-ADM-05 接入渠道（600px）：提供商三卡 + 模型 id / 密钥（脱敏）/ 优先级 / 预算 +
 *  连通性测试区三态（idle 提示 → testing 脉冲 → 成功列模型与配额 / 失败诊断建议）；
 *  测试成功才解锁保存。 */
function AddChannelModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [provider, setProvider] = useState<'deepseek' | 'qwen' | 'ollama'>('deepseek')
  const [modelId, setModelId] = useState('deepseek-chat')
  const [apiKey, setApiKey] = useState('')
  const [priority, setPriority] = useState(4)
  const [budget, setBudget] = useState<number | ''>(200)
  const [test, setTest] = useState<TestState>({ phase: 'idle' })

  const testMutation = useMutation({
    mutationFn: () => testModelChannel({ provider, model_id: modelId, api_key: apiKey || undefined }),
    onSuccess: result => setTest({ phase: 'ok', result }),
    onError: e => setTest({ phase: 'fail', message: e.message }),
  })

  const createMutation = useMutation({
    mutationFn: () =>
      createModelChannel({
        provider, model_id: modelId, api_key: apiKey || undefined,
        priority, budget_daily: budget === '' ? null : budget,
      }),
    onSuccess: c => {
      toast.success(`渠道「${c.name}」已接入（密钥仅存哈希与长度）`)
      void qc.invalidateQueries({ queryKey: ['admin', 'models'] })
      onClose()
    },
    onError: e => toast.error(e.message),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title="接入模型渠道（LiteLLM 网关）"
      width={600}
      footer={
        <>
          <span className="mr-auto text-[11px] text-label-3">连通性测试成功后才可保存</span>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn btn-p"
            data-testid="adm-model-save"
            disabled={test.phase !== 'ok' || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            <Check size={13} aria-hidden /> 保存渠道
          </button>
        </>
      }
    >
      <div className="field">
        <span className="field-label">提供商</span>
        <div className="grid grid-cols-3 gap-2">
          {PROVIDERS.map(p => (
            <button
              key={p.key}
              type="button"
              data-testid={`adm-model-provider-${p.key}`}
              aria-pressed={provider === p.key}
              onClick={() => { setProvider(p.key); setTest({ phase: 'idle' }) }}
              className={`rounded-xl border p-3 text-left transition-colors ${provider === p.key ? 'border-accent bg-accent-soft' : 'border-separator hover:border-label-3'}`}
            >
              <b className="text-xs">{p.label}</b>
              <div className="mt-1 text-[11px] leading-4 text-label-2">{p.desc}</div>
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="field">
          <label className="field-label" htmlFor="adm-model-id">模型 id</label>
          <input
            id="adm-model-id"
            data-testid="adm-model-id"
            className="input"
            value={modelId}
            onChange={e => { setModelId(e.target.value); setTest({ phase: 'idle' }) }}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="adm-model-key">API Key（脱敏）</label>
          <input
            id="adm-model-key"
            data-testid="adm-model-key"
            className="input mono"
            type="password"
            disabled={provider === 'ollama'}
            placeholder={provider === 'ollama' ? '本地无需密钥' : PROVIDERS.find(p => p.key === provider)?.hint}
            value={apiKey}
            onChange={e => { setApiKey(e.target.value); setTest({ phase: 'idle' }) }}
          />
          {apiKey && <div className="fhint">已保存 · 只写哈希与长度，不落明文</div>}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="field">
          <label className="field-label" htmlFor="adm-model-priority">优先级</label>
          <Select id="adm-model-priority" className="input" value={priority} onChange={e => setPriority(Number(e.target.value))}>
            <option value={1}>P1 · 本地默认</option>
            <option value={2}>P2</option>
            <option value={3}>P3</option>
            <option value={4}>P4 · 云溢出</option>
          </Select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="adm-model-budget">预算上限（¥ / 日）</label>
          <input
            id="adm-model-budget"
            className="input"
            type="number"
            min={0}
            placeholder="不限"
            value={budget}
            onChange={e => setBudget(e.target.value === '' ? '' : Number(e.target.value))}
          />
          <div className="fhint">超出自动降级回本地渠道并告警（通知中心）。</div>
        </div>
      </div>

      {/* 连通性测试区：三态 */}
      <div className="rounded-xl border border-separator p-3" data-testid="adm-model-test">
        {test.phase === 'idle' && (
          <div className="flex items-center gap-2 text-xs text-label-2">
            <span className="h-2 w-2 rounded-full bg-[var(--label-3)]" aria-hidden />
            未测试：填写后点击「测试连通性」，成功才可保存。
            <button type="button" className="btn btn-s btn-sm ml-auto" data-testid="adm-model-test-btn" onClick={() => { setTest({ phase: 'testing' }); testMutation.mutate() }}>
              测试连通性
            </button>
          </div>
        )}
        {test.phase === 'testing' && (
          <div className="flex items-center gap-2 text-xs text-accent" data-testid="adm-model-test-running">
            <span className="h-2 w-2 animate-pulse rounded-full bg-accent" aria-hidden />
            正在测试连通性…
          </div>
        )}
        {test.phase === 'ok' && (
          <div data-testid="adm-model-test-ok">
            <div className="flex items-center gap-2 text-xs font-semibold" style={{ color: 'var(--green)' }}>
              <span className="h-2 w-2 rounded-full" style={{ background: 'var(--green)' }} aria-hidden />
              连通性测试 · 成功（{test.result.latency_ms}ms）
              <button type="button" className="btn btn-g btn-sm ml-auto" onClick={() => { setTest({ phase: 'testing' }); testMutation.mutate() }}>重测</button>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {test.result.models.map(m => (
                <span key={m.id} className="mono badge b-green">{m.id} · {m.ctx} ctx</span>
              ))}
            </div>
            <div className="mono mt-2 text-[11px] text-label-2">
              配额：RPM {test.result.quota.rpm.toLocaleString()} · TPM {test.result.quota.tpm.toLocaleString()} · 本月已用 ¥{test.result.quota.used_yuan} / ¥{test.result.quota.budget_yuan}
            </div>
          </div>
        )}
        {test.phase === 'fail' && (
          <div className="al-err alert" data-testid="adm-model-test-fail">
            <div>
              <b>连通性测试失败</b>
              {test.message}
              <div className="mt-1 text-[11px]">诊断建议：密钥无效 / 网络不通 / 模型名不存在。失败时保存按钮保持禁用。</div>
            </div>
          </div>
        )}
      </div>
    </Modal>
  )
}

/** IX-ADM-06 渠道删除确认（danger）：级联影响（Agent 插槽 + 近 30 天用量）+
 *  迁移建议下拉 + 输入渠道名确认。 */
function DeleteChannelModal({ channel, onClose }: { channel: ModelChannel; onClose: () => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [migrateTo, setMigrateTo] = useState('')
  const { data: impact } = useQuery({ queryKey: ['admin', 'models', channel.id, 'impact'], queryFn: () => getModelImpact(channel.id) })

  const mutation = useMutation({
    mutationFn: () => deleteModelChannel(channel.id),
    onSuccess: () => {
      toast.success(`渠道「${channel.name}」已删除${migrateTo ? '，引用插槽已迁移' : ''}`)
      void qc.invalidateQueries({ queryKey: ['admin', 'models'] })
      onClose()
    },
    onError: e => toast.error(e.message),
  })

  return (
    <Modal
      open
      danger
      onClose={onClose}
      title={`删除渠道 · ${channel.name}`}
      width={440}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn btn-d"
            data-testid="adm-model-del-confirm"
            disabled={name !== channel.name || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            确认删除
          </button>
        </>
      }
    >
      <div className="al-err alert">
        <div>
          <b>级联影响</b>
          引用该渠道的 Agent 插槽：
          {(impact?.agents ?? []).length === 0 && <div className="text-[11px]">（无引用插槽）</div>}
          {(impact?.agents ?? []).map(a => <div key={a} className="mono text-[11px]">{a}</div>)}
          <div className="mt-1 text-[11px]">
            近 30 天：{impact?.sessions_30d ?? '—'} 会话 · {impact?.tokens_30d ?? '—'} tokens · {impact?.cost_30d ?? '—'}
          </div>
        </div>
      </div>
      <div className="field mt-3">
        <label className="field-label" htmlFor="adm-model-migrate">迁移建议（引用插槽的默认渠道切到）</label>
        <Select id="adm-model-migrate" data-testid="adm-model-migrate" className="input" value={migrateTo} onChange={e => setMigrateTo(e.target.value)}>
          <option value="">不迁移（插槽置为待配置）</option>
          {(impact?.migrate_to ?? []).map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
        </Select>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="adm-model-del-name">输入渠道名 <b className="text-red">{channel.name}</b> 以确认（不可逆，审计留痕）</label>
        <input id="adm-model-del-name" data-testid="adm-model-del-name" className="input" value={name} onChange={e => setName(e.target.value)} />
      </div>
    </Modal>
  )
}
