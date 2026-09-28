import { useState } from 'react'
import { AtSign, Check, ChevronDown, Crown, Layers, RefreshCw, Shield } from 'lucide-react'
import { FloatingCard } from '@/components/popover'
import { ApiError } from '@/api/client'
import { ROUTING_LABEL, patchRouting, type RoutingMode } from '../api'

/** IX-GRP-02 发言路由切换 RoutingModePicker（Popover）：四模式卡 + 预算提示 +
 *  宪法 2 注记；选择随会话记忆（PATCH /sessions/{id} routing，§5.2 预登记）。 */

const MODE_META: { mode: RoutingMode; icon: typeof AtSign; cls: string; badge: string; desc: React.ReactNode; mono: string }[] = [
  {
    mode: 'mention', icon: AtSign, cls: 'var(--teal)', mono: 'mention', badge: '确定性 · 零 Token',
    desc: <>@谁谁答、可多点名；未点名成员不注入本轮上下文、不消耗预算（提示「本轮 N/M 成员参与」）。</>,
  },
  {
    mode: 'round_robin', icon: RefreshCw, cls: 'var(--indigo)', mono: 'round_robin', badge: '确定性',
    desc: <>按成员序依次应答，一轮一人；适合巡检式逐个过堂。</>,
  },
  {
    mode: 'all', icon: Layers, cls: 'var(--orange)', mono: 'all', badge: '确定性 · 并行',
    desc: <>并行全答进 ResponseGroup（GRP-04）由用户选优；预算 ×N，<b className="text-orange">&gt;3 成员发送前二次确认</b>。</>,
  },
  {
    mode: 'orchestrator', icon: Crown, cls: 'var(--purple)', mono: 'orchestrator', badge: '唯一 LLM 路由',
    desc: <>协调者成员做低频语义判断选择应答者，路由决定（who / why）写审计，消息流呈现系统行（GRP-03）。</>,
  },
]

export function RoutingModePicker({
  sessionId,
  routing,
  onChange,
}: {
  sessionId: string
  routing: RoutingMode
  onChange: (r: RoutingMode) => void
}) {
  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState<DOMRect | null>(null)
  const [saving, setSaving] = useState<RoutingMode | null>(null)
  const [errMsg, setErrMsg] = useState<string | null>(null)
  const isOrchestrator = routing === 'orchestrator'

  async function pick(mode: RoutingMode) {
    if (mode === routing) {
      setOpen(false)
      return
    }
    setSaving(mode)
    setErrMsg(null)
    try {
      await patchRouting(sessionId, mode)
      onChange(mode)
      setOpen(false)
    } catch (e) {
      setErrMsg(e instanceof ApiError ? e.message : '路由切换失败')
    } finally {
      setSaving(null)
    }
  }

  return (
    <>
      <button
        type="button"
        data-testid="grp-routing-open"
        className="flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold"
        style={{
          color: isOrchestrator ? 'var(--accent)' : 'var(--label-2)',
          background: isOrchestrator ? 'var(--accent-soft)' : 'var(--surface-2)',
          border: isOrchestrator ? 'none' : '1px solid var(--separator)',
        }}
        onClick={e => {
          setAnchor(e.currentTarget.getBoundingClientRect())
          setOpen(v => !v)
        }}
      >
        <Crown size={12} style={{ color: 'var(--purple)' }} aria-hidden />
        _routing: {ROUTING_LABEL[routing]}
        <ChevronDown size={11} aria-hidden />
      </button>
      <FloatingCard open={open} anchor={anchor} onClose={() => setOpen(false)} width={352}>
        <div className="mb-2 flex items-center gap-2">
          <b className="text-[13px]">发言路由</b>
          <span className="badge b-purple">当前 · {ROUTING_LABEL[routing]}</span>
          <span className="ml-auto text-[11px] text-label-3">随会话记忆</span>
        </div>
        {MODE_META.map(m => {
          const Icon = m.icon
          const active = m.mode === routing
          return (
            <button
              key={m.mode}
              type="button"
              data-testid={`grp-mode-${m.mode}`}
              disabled={saving !== null}
              className="mb-1.5 block w-full rounded-xl p-2.5 text-left disabled:opacity-60"
              style={{
                border: active ? '1.5px solid var(--accent)' : '1px solid var(--separator)',
                background: active ? 'var(--accent-soft)' : 'transparent',
              }}
              onClick={() => void pick(m.mode)}
            >
              <div className="flex items-center gap-2">
                <span className="flex h-[27px] w-[27px] flex-none items-center justify-center rounded-lg" style={{ background: `color-mix(in srgb, ${m.cls} 14%, transparent)`, color: m.cls }}>
                  <Icon size={14} aria-hidden />
                </span>
                <b className="text-xs">{ROUTING_LABEL[m.mode]}</b>
                <span className="mono text-2xs text-label-3">{m.mono}</span>
                <span className={`badge ${m.mode === 'orchestrator' ? 'b-purple' : 'b-green'} ml-auto`}>{m.badge}</span>
                {active && <Check size={13} style={{ color: 'var(--accent)' }} aria-hidden />}
              </div>
              <div className="mt-1.5 text-[11px] leading-relaxed text-label-2">{m.desc}</div>
            </button>
          )
        })}
        {errMsg && <div className="field-err">{errMsg}</div>}
        <div className="mt-2 flex items-start gap-1.5 border-t border-separator pt-2 text-[11px] leading-relaxed text-label-3">
          <Shield size={12} className="mt-0.5 flex-none" style={{ color: 'var(--green)' }} aria-hidden />
          <span>仅协调者模式消耗一次路由判定，其余三种确定性路由零额外消耗；切换即写入会话记忆并即时生效。</span>
        </div>
      </FloatingCard>
    </>
  )
}
