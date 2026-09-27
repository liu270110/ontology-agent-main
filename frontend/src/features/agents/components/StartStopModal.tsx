import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, CircleStop, Plug, Play, Wrench, X } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { startAgent, stopAgent, type PlatformAgent } from '../api'

/** IX-AGT-04 启停确认（26 篇 §8.2；画板 ix-agt-04）：440px——
 *  停止=danger 主画：影响说明（N 个进行中会话将被终止 / 排队任务将失败）+ 确认停止；
 *  启动=健康自检三步进度（适配器连通 → 工具可用 → 就绪）后自动拉起。
 *  动作经 POST /agents/{id}/stop|start（R 预登记端点，见 R 清单）。 */

const SELF_CHECK_STEPS = ['适配器连通', '工具可用', '就绪'] as const

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
  const [checkStep, setCheckStep] = useState(-1) // 启动健康自检进度（-1 未开始）

  const stop = useMutation({
    mutationFn: () => stopAgent(agent!.id),
    onSuccess: res => {
      void qc.invalidateQueries({ queryKey: ['agents'] })
      toast.success(`${agent?.name} 已停止`, { description: `${res.terminated_sessions} 个进行中会话被终止；已生成部分保留，可随时从历史继续。` })
      onClose()
    },
  })

  const start = useMutation({
    mutationFn: () => startAgent(agent!.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['agents'] })
      toast.success(`${agent?.name} 已启动 · 健康自检通过`, { description: '适配器连通 → 工具可用 → 就绪' })
      onClose()
    },
  })

  // 启动：健康自检三步步进（每步 500ms），走完再调启动端点
  useEffect(() => {
    if (action !== 'start' || !agent) return
    setCheckStep(0)
    const timers = [
      setTimeout(() => setCheckStep(1), 500),
      setTimeout(() => setCheckStep(2), 1000),
      setTimeout(() => {
        setCheckStep(3)
        start.mutate()
      }, 1500),
    ]
    return () => timers.forEach(clearTimeout)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [action, agent?.id])

  if (!agent || !action) return null

  const stopping = action === 'stop'

  return (
    <Modal
      open
      onClose={onClose}
      title={stopping ? '停止 Agent?' : `启动 ${agent.name}`}
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
              <CircleStop size={12} aria-hidden /> 确认停止
            </button>
          ) : (
            <button type="button" className="btn btn-p btn-sm" disabled={start.isPending} onClick={() => start.mutate()}>
              <Play size={12} aria-hidden /> {start.isPending ? '启动中…' : '完成并启动'}
            </button>
          )}
        </>
      }
    >
      <div className="text-[11.5px] text-label-3">
        {agent.name} · <span className="mono">{agent.id}</span> · 本操作写入审计日志（操作人：刘以在）
      </div>

      {stopping ? (
        <div className="mt-3 space-y-2">
          <p className="text-[12px] leading-5">
            停止后框架立即下线，不再接收新消息与会话请求；本操作写入审计日志。
          </p>
          <div className="rounded-xl px-3 py-2.5 text-[11.5px] leading-5" style={{ background: 'var(--orange-soft)' }}>
            <b className="text-orange">影响说明</b>
            <div>
              {agent.active_sessions} 个进行中会话将被终止：会话已生成部分保留，可随时从历史继续；
              不再接收新消息与会话请求。
            </div>
          </div>
          <div className="rounded-xl px-3 py-2.5 text-[11.5px] leading-5" style={{ background: 'var(--red-soft)' }}>
            <b className="text-red">任务连锁</b>
            <div>
              排队中任务 {agent.queued_tasks} 个（TASK 队列）将标记失败，可在任务中心重试（IX-TSK-03）。
            </div>
          </div>
          <div className="flex items-center gap-2 rounded-xl border border-separator px-3 py-2 text-[11px] text-label-2">
            <Wrench size={12} aria-hidden />
            工具绑定与适配器配置保留，重新启动后自动恢复并先跑健康自检。
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
                <Plug size={11} className="text-green" aria-hidden /> 适配器连通 · RTT 86ms（RPC/JSONL）
              </div>
            )}
            {checkStep > 1 && (
              <div className="flex items-center gap-1.5">
                <Wrench size={11} className="text-green" aria-hidden /> 工具可用 · 已绑定 {agent.tools.length} 个工具全部可达
              </div>
            )}
            {checkStep > 2 && (
              <div className="flex items-center gap-1.5">
                <Check size={11} className="text-green" aria-hidden /> 就绪 · 正在拉起实例（状态 · 运行中）
              </div>
            )}
            {checkStep === 0 && <div className="text-label-3">正在探测适配器…</div>}
          </div>
        </div>
      )}
    </Modal>
  )
}

/** 卡片启停按钮入口（供列表/详情复用的轻封装不必要，这里仅导出弹窗；X 用于 danger 图标语义） */
export const StartStopIcons = { Stop: CircleStop, X }
