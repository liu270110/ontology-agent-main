import { useEffect, useState } from 'react'
import { ApiError } from '@/api/client'
import { listSessionFeedback, submitSessionFeedback, type SessionFeedbackDto } from '../api'

/** 助手消息尾反馈组件（飞轮采集环前端 W9，docs/Agent/19 §5：用户信号第一落点）。
 *
 *  - run 完成后（store RUN_FINISHED 归约绑定 m.runId）挂消息卡尾部；三态轻按钮
 *    「有帮助👍 / 部分解决 / 没解决👎」→ 点击展开可选纠错文本框（≤120 字）→ 提交
 *    POST /sessions/{sid}/feedback（契约 202）；
 *  - 已反馈：挂载时 GET 本人历史回显当前状态（按 run_id 匹配）；再选其他态提交=幂等更新；
 *  - 归属外会话（GET/POST 404）静默不渲染（fail-soft——反馈为增强面，不阻塞消息流）；
 *    其余网络错误同样静默降级。
 */

export type FeedbackOutcome = SessionFeedbackDto['outcome']

const OPTIONS: { value: FeedbackOutcome; label: string }[] = [
  { value: 'completed', label: '有帮助 👍' },
  { value: 'partial', label: '部分解决' },
  { value: 'failed', label: '没解决 👎' },
]

const CORRECTION_MAX = 120 // 契约常量（后端 SessionFeedbackIn.correction_text max_length 同源）

export function MessageFeedback({ sessionId, runId }: { sessionId: string; runId: string }) {
  const [outcome, setOutcome] = useState<FeedbackOutcome | null>(null)
  const [submitted, setSubmitted] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [correction, setCorrection] = useState('')
  const [busy, setBusy] = useState(false)
  const [hidden, setHidden] = useState(false)

  // 挂载回显：GET 本人反馈历史按 run_id 匹配（404=归属外会话 → 不渲染）
  useEffect(() => {
    let alive = true
    listSessionFeedback(sessionId)
      .then(res => {
        if (!alive) return
        const mine = res.data.find(f => f.run_id === runId)
        if (mine) {
          setOutcome(mine.outcome)
          setCorrection(mine.correction_text ?? '')
          setSubmitted(true)
        }
      })
      .catch((e: unknown) => {
        if (!alive) return
        if (e instanceof ApiError && e.httpStatus === 404) setHidden(true)
      })
    return () => {
      alive = false
    }
  }, [sessionId, runId])

  if (hidden) return null

  async function submit() {
    if (!outcome || busy) return
    setBusy(true)
    try {
      await submitSessionFeedback(sessionId, {
        run_id: runId,
        outcome,
        correction_text: correction.trim() ? correction.trim() : null,
      })
      setSubmitted(true)
      setExpanded(false)
    } catch (e: unknown) {
      if (e instanceof ApiError && e.httpStatus === 404) setHidden(true)
      // 其余失败保留展开态与输入，用户可重试（不 toast——反馈为增强面）
    } finally {
      setBusy(false)
    }
  }

  return (
    <div data-testid={`msg-feedback-${runId}`} className="mt-1.5 flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-1">
        {OPTIONS.map(o => (
          <button
            key={o.value}
            type="button"
            data-testid={`fb-btn-${o.value}`}
            aria-pressed={outcome === o.value}
            onClick={() => {
              setOutcome(o.value)
              setExpanded(true)
            }}
            className={`flex h-6 items-center gap-1 rounded-full border px-2 text-[11px] transition-colors ${
              outcome === o.value
                ? 'border-accent bg-accent-soft text-accent'
                : 'border-separator text-label-3 hover:border-accent hover:text-accent'
            }`}
          >
            {o.label}
          </button>
        ))}
        {submitted && outcome && (
          <span data-testid="fb-status" className="ml-1 text-2xs text-label-3">
            已反馈（{OPTIONS.find(o => o.value === outcome)?.label}）· 点击其他选项可修改
          </span>
        )}
      </div>
      {expanded && (
        <div data-testid="fb-editor" className="flex flex-col gap-1.5 rounded-lg border border-separator bg-surface-2 p-2">
          <textarea
            data-testid="fb-correction"
            aria-label="纠错说明（可选）"
            placeholder="补充纠错说明（可选，帮助我们把任务改进为评测场景）"
            maxLength={CORRECTION_MAX}
            value={correction}
            onChange={e => setCorrection(e.target.value.slice(0, CORRECTION_MAX))}
            className="min-h-[52px] resize-y rounded-md border border-separator bg-surface px-2 py-1.5 text-xs text-label outline-none focus:border-accent"
          />
          <div className="flex items-center justify-between">
            <span className="text-2xs text-label-3">
              {correction.length}/{CORRECTION_MAX}
            </span>
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                data-testid="fb-cancel"
                onClick={() => setExpanded(false)}
                className="rounded-md px-2 py-1 text-[11px] text-label-3 hover:bg-surface hover:text-label"
              >
                取消
              </button>
              <button
                type="button"
                data-testid="fb-submit"
                disabled={!outcome || busy}
                onClick={() => void submit()}
                className="btn btn-g btn-sm disabled:opacity-40"
              >
                {busy ? '提交中…' : '提交反馈'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
