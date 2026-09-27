import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { ChevronRight, Download } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { installPlugin, type MarketPlugin } from '../api'

/** IX-MKT-02 安装向导（26 篇 §9.1；画板 ix-mkt-02）：3 步 640px 强制步骤——
 *  ①版本选择（最新推荐 + 历史可回退）②scope 逐项确认（高危 danger 底 + 「我已了解」
 *  单独勾选，未勾禁下一步——强制步骤不可跳过）③安装确认（生成安装任务 → 任务中心）。
 *  提交 POST /plugins/{id}/install（§6.7：202 + install_id → /tasks?job=）。 */

const STEPS = ['① 选择版本', '② scope 确认', '③ 安装确认'] as const

export function InstallWizard({
  plugin,
  onClose,
}: {
  plugin: MarketPlugin | null
  onClose: () => void
}) {
  const navigate = useNavigate()
  const [step, setStep] = useState(0)
  const [version, setVersion] = useState('')
  const [grants, setGrants] = useState<Set<string>>(new Set())
  const [ack, setAck] = useState(false) // 「我已了解」单独勾选（高危强制步骤）

  // 打开向导：非高危 scope 默认授权（逐项可取消收窄授权面）；高危项默认不授，
  // 须单独勾选「我已了解」 acknowledgment 后方可进入下一步（强制步骤不可跳过）
  useEffect(() => {
    if (plugin) {
      setVersion(plugin.versions.find(v => v.latest)?.version ?? '')
      setGrants(new Set(plugin.scopes.filter(s => !s.danger).map(s => s.scope)))
      setAck(false)
      setStep(0)
    }
  }, [plugin])

  const install = useMutation({
    mutationFn: () => installPlugin(plugin!.id, { version, scope_grants: [...grants] }),
    onSuccess: res => {
      toast.success(`安装任务已创建（${res.install_id}）`, {
        description: '验签 → 解包 → scope 登记 → 工具注入 → 完成（任务中心流水线跟踪）',
      })
      onClose()
      navigate(`/tasks?job=${res.install_id}`)
    },
  })

  if (!plugin) return null
  const latest = plugin.versions.find(v => v.latest) ?? plugin.versions[0]
  const chosen = version || latest?.version || ''
  const dangerScopes = plugin.scopes.filter(s => s.danger)
  const canNext = dangerScopes.length === 0 || ack

  function toggleGrant(scope: string, checked: boolean) {
    setGrants(prev => {
      const next = new Set(prev)
      if (checked) next.add(scope)
      else next.delete(scope)
      return next
    })
  }

  function reset() {
    setStep(0)
    setVersion('')
    setGrants(new Set())
    setAck(false)
  }

  return (
    <Modal
      open={!!plugin}
      onClose={() => {
        onClose()
        reset()
      }}
      title={`安装插件 · ${plugin.name}`}
      width={640}
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
          <span className="mx-auto text-[11px] text-label-3">
            {step === 1 && !canNext && '高危项未确认，下一步保持禁用——scope 确认为强制步骤，任何治理档不可跳过。'}
            {step === 1 && canNext && '第③步提交 POST /plugins/{id}/install · 202 生成安装任务'}
            {step === 2 && `scope_grants：[...grants 略] · 版本 ${chosen}`}
          </span>
          {step < 2 ? (
            <button
              type="button"
              className="btn btn-p btn-sm"
              data-testid="mkt-install-next"
              disabled={step === 1 && !canNext}
              onClick={() => setStep(s => s + 1)}
            >
              下一步 · {STEPS[step + 1].slice(2)} <ChevronRight size={12} aria-hidden />
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-p btn-sm"
              data-testid="mkt-install-submit"
              disabled={install.isPending}
              onClick={() => install.mutate()}
            >
              <Download size={12} aria-hidden /> {install.isPending ? '提交中…' : '确认安装'}
            </button>
          )}
        </>
      }
    >
      <div className="text-[11.5px] text-label-3">
        <span className="mono">{plugin.slug}</span> · 开发者 {plugin.developer}
      </div>

      {/* 步骤条 */}
      <div className="steps mt-3" aria-label="安装向导步骤条">
        {STEPS.map((s, i) => (
          <div key={s} className={`step ${i < step ? 'done' : i === step ? 'cur' : ''}`}>
            <span className="s-dot">{i < step ? '✓' : ''}</span>
            <span className="s-name">{s}</span>
          </div>
        ))}
      </div>

      {step === 0 && (
        <div className="mt-4 space-y-2">
          {plugin.versions.map(v => (
            <label
              key={v.version}
              className="flex cursor-pointer items-start gap-2.5 rounded-xl border px-3 py-2.5 transition-colors"
              style={{
                borderColor: chosen === v.version ? 'var(--accent)' : 'var(--separator)',
                background: chosen === v.version ? 'var(--accent-soft)' : 'var(--surface)',
              }}
            >
              <input
                type="radio"
                name="mkt-version"
                className="mt-1"
                checked={chosen === v.version}
                onChange={() => setVersion(v.version)}
              />
              <span>
                <span className="text-[12.5px] font-semibold">
                  {v.version} {v.latest && <span className="badge b-green">最新 · 推荐</span>}
                </span>
                <span className="ml-2 text-[10.5px] text-label-3">{v.released_at} 发布</span>
                <div className="mt-0.5 text-[11px] text-label-2">{v.note}</div>
              </span>
            </label>
          ))}
          <div className="fhint">历史版本可回退选择；安装走任务中心流水线（验签 → 解包 → scope 登记 → 工具注入）。</div>
        </div>
      )}

      {step === 1 && (
        <div className="mt-4">
          {step >= 1 && (
            <div className="mb-2 flex items-center gap-2">
              <span className="badge b-green">第①步已完成</span>
              <span className="text-[11px] text-label-3">已选 {chosen}（最新 · 推荐）· 历史版本可回退选择</span>
            </div>
          )}
          <b className="text-[12.5px]">逐项确认权限 scope（{plugin.scopes.length} 项，逐项授权后能提交安装）</b>
          <div className="mt-2 space-y-2">
            {plugin.scopes.map(s => (
              <div
                key={s.scope}
                className="rounded-xl border px-3 py-2.5"
                style={
                  s.danger
                    ? { background: 'var(--red-soft)', borderColor: 'var(--red)' }
                    : { borderColor: 'var(--separator)', background: 'var(--surface)' }
                }
                data-testid={`mkt-scope-${s.scope}`}
              >
                <div className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    aria-label={`授权 ${s.scope}`}
                    data-testid={`mkt-grant-${s.scope}`}
                    checked={grants.has(s.scope)}
                    onChange={e => toggleGrant(s.scope, e.target.checked)}
                  />
                  <span className="mono text-[12px] font-semibold">{s.scope}</span>
                  <span className={`badge ${s.access === '只读' ? 'b-gray' : s.danger ? 'b-red' : 'b-orange'}`}>{s.access}</span>
                  <span className="truncate text-[11px] text-label-2">{s.desc}</span>
                </div>
                {s.danger && (
                  <label className="mt-1.5 flex cursor-pointer items-center gap-2 text-[11.5px] text-red">
                    <input
                      type="checkbox"
                      aria-label={`我已了解 ${s.scope} 的高危影响`}
                      data-testid="mkt-danger-ack"
                      checked={ack}
                      onChange={e => setAck(e.target.checked)}
                    />
                    我已了解：该 scope 将允许插件发起外部系统调用，每次调用经 L7 网关审计并带 trace_id
                  </label>
                )}
              </div>
            ))}
          </div>
          {!canNext && <div className="field-err">高危项未确认，下一步保持禁用（强制步骤不可跳过）。</div>}
        </div>
      )}

      {step === 2 && (
        <div className="mt-4 space-y-2">
          <div className="rounded-xl border border-separator bg-surface-2 px-4 py-3 text-[12px] leading-6">
            <div><b>插件：</b>{plugin.name}（{plugin.slug}）· 版本 {chosen}</div>
            <div><b>scope 授权：</b>{[...grants].join('、') || '（未授予任何 scope）'}</div>
            <div className="text-label-3">
              第③步提交 POST /plugins/{plugin.id}/install · 202 生成安装任务 in_01X →
              任务中心流水线：验签 → 解包 → scope 登记 → 工具注入 → 完成（SSE 推进）。
              安装成功后插件 tools 进注册中心（GET /tools 可见）。
            </div>
          </div>
        </div>
      )}
    </Modal>
  )
}
