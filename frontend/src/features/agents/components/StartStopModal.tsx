import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, CircleStop, Plug, Play, Wrench, X } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { healthCheckAgent, startAgent, stopAgent, type PlatformAgent } from '../api'

/** IX-AGT-04 启停确认（26 篇 §8.2；画板 ix-agt-04）：440px——
 *  停止=danger 主画：影响说明（live 语义：disabled=拒绝新会话绑定，存量会话与运行中
 *  Run 跑完不中断，terminated_sessions 恒 0——不级联改 sessions）+ 确认停止；
 *  启动=健康自检三步（真实链路：POST /health-check 适配器连通 → 白名单回显 → 就绪）
 *  通过后 POST /enable 拉起；自检失败则停留错误态不启用。
 *  动作经 POST /agents/{id}/enable|disable（live 定稿动词，返回 {id,status,
 *  terminated_sessions}）。 */

const SELF_CHECK_STEPS = ['适配器连通', '工具白名单', '就绪'] as const

export function StartStopModal({
  agent,
  action,
  onClose,
}: {
  agent: PlatformAgent | null
  action: 'start' | 'stop' | null
  onClose: () => void
}) {
  const qc = useQueryClient()
  // 启动自检进度（-1 未开始；0/1/2 步序；3=全部通过）；-2=自检失败
  const [checkStep, setCheckStep] = useState(-1)
  const [checkError, setCheckError] = useState<string | null>(null)

  const stop = useMutation({
    mutationFn: () => stopAgent(agent!.id),
    onSuccess: res => {
      void qc.invalidateQueries({ queryKey: ['agents'] })
      toast.success(`${agent?.name} 已停用`, {
        description: `status=${res.status}：新会话拒绝绑定；存量会话与运行中 Run 不中断（terminated_sessions=${res.terminated_sessions ?? 0}）。`,
      })
      onClose()
    },
    onError: e => {
      toast.error('停用失败', { description: e instanceof Error ? e.message : undefined })
    },
  })

  const start = useMutation({
    mutationFn: () => startAgent(agent!.id),
    onSuccess: res => {
      void qc.invalidateQueries({ queryKey: ['agents'] })
      toast.success(`${agent?.name} 已启用`, { description: `status=${res.status}；新会话恢复放行。` })
      onClose()
    },
    onError: e => {
      toast.error('启用失败', { description: e instanceof Error ? e.message : undefined })
    },
  })

  // 启动自检真实链路：①health-check（进程内适配器即通过）→ ②白名单回显 → ③enable
  useEffect(() => {
    if (action !== 'start' || !agent) return
    let cancelled = false
    setCheckStep(0)
    setCheckError(null)
    ;(async () => {
      try {
        await healthCheckAgent(agent.id) // 自检不过=异常上抛，停留错误态不启用
        if (cancelled) return
        setCheckStep(1)
        setCheckStep(2)
        setCheckStep(3)
        start.mutate()
      } catch (e) {
        if (cancelled) return
        setCheckError(e instanceof Error ? e.message : '健康自检失败')
        setCheckStep(-2)
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [action, agent?.id])

  if (!agent || !action) return null

  const stopping = action === 'stop'

  return (
    <Modal
      open
      onClose={onClose}
      title={stopping ? '停用 Agent?' : `启用 ${agent.name}`}
      width={440}
      danger={stopping}
      footer={
        <>
          <span className="mr-auto text-[11px] text-label-3">Esc 或取消不断运行</span>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose} data-testid="agt-toggle-cancel">
            取消
          </button>
          {stopping ? (
            <button
              type="button"
              className="btn btn-d btn-sm"
              data-testid="agt-stop-confirm"
              disabled={stop.isPending}
              onClick={() => stop.mutate()}
            >
              <CircleStop size={12} aria-hidden /> 确认停用
            </button>
          ) : (
            <button type="button" className="btn btn-p btn-sm" disabled={start.isPending} onClick={() => start.mutate()}>
              <Play size={12} aria-hidden /> {start.isPending ? '启用中…' : '完成并启用'}
            </button>
          )}
        </>
      }
    >
      <div className="text-[11px] text-label-3">
        {agent.name} · <span className="mono">{agent.id}</span> · 本操作写入审计日志
      </div>

      {stopping ? (
        <div className="mt-3 space-y-2">
          <p className="text-xs leading-5">
            停用后该 Agent 拒绝新会话绑定；本操作写入审计日志（操作人随审计通道留痕）。
          </p>
          <div className="rounded-xl px-3 py-2.5 text-[11px] leading-5" style={{ background: 'var(--orange-soft)' }}>
            <b className="text-orange">影响说明（live 语义）</b>
            <div>
              disabled=拒绝<b>新会话</b>绑定；存量会话与运行中 Run 跑完不中断（04 §10 裁决），
              不级联改写会话行（terminated_sessions 恒 0）。
            </div>
          </div>
          <div className="rounded-xl px-3 py-2.5 text-[11px] leading-5" style={{ background: 'var(--red-soft)' }}>
            <b className="text-red">任务连锁</b>
            <div>排队中任务仍会按原配置执行完毕；新提交的会话请求将被拒绝（409 拒绑）。</div>
          </div>
          <div className="flex items-center gap-2 rounded-xl border border-separator px-3 py-2 text-[11px] text-label-2">
            <Wrench size={12} aria-hidden />
            工具白名单（config.tool_whitelist）与配置保留，重新启用后新会话恢复放行。
          </div>
        </div>
      ) : (
        <div className="mt-3" aria-label="启动健康自检三步进度" data-testid="agt-start-check">
          <div className="steps">
            {SELF_CHECK_STEPS.map((s, i) => (
              <div key={s} className={`step ${i < checkStep ? 'done' : i === checkStep ? 'cur' : ''}`}>
                <span className="s-dot">{i < checkStep ? <Check size={9} aria-hidden /> : ''}</span>
                <span className="s-name">
                  {i + 1} {s}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-3 space-y-1 text-[11px] text-label-2" data-testid="agt-start-check-detail">
            {checkStep > 0 && (
              <div className="flex items-center gap-1.5">
                <Plug size={11} className="text-green" aria-hidden /> 适配器连通 · health-check 通过（进程内适配器=inprocess）
              </div>
            )}
            {checkStep > 1 && (
              <div className="flex items-center gap-1.5">
                <Wrench size={11} className="text-green" aria-hidden /> 工具白名单 · config.tool_whitelist {(agent.config?.tool_whitelist as string[] | undefined)?.length ?? 0} 项
              </div>
            )}
            {checkStep > 2 && (
              <div className="flex items-center gap-1.5">
                <Check size={11} className="text-green" aria-hidden /> 就绪 · 正在启用实例（status → enabled）
              </div>
            )}
            {checkStep === 0 && <div className="text-label-3">正在探活适配器…</div>}
            {checkStep === -2 && (
              <div className="rounded-xl px-3 py-2" style={{ background: 'var(--red-soft)' }} data-testid="agt-start-check-error" role="alert">
                <b className="text-red">健康自检未通过</b> <span className="ml-1">{checkError}</span>
                <span className="mt-0.5 block text-label-3">实例未启用；排除故障后重新打开「启用」。</span>
              </div>
            )}
          </div>
        </div>
      )}
    </Modal>
  )
}

/** 卡片启停按钮入口（供列表/详情复用的轻封装不必要，这里仅导出弹窗；X 用于 danger 图标语义） */
export const StartStopIcons = { Stop: CircleStop, X }
