import { useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { Check, ChevronDown, PenLine, Search, Waypoints, X } from 'lucide-react'
import type { AgenticBlock, AgenticGradeReason } from '@/api/contracts'
import { AgenticDegradedBanner } from './AgenticDegradedBanner'
import { NextActionsCard } from './NextActionsCard'

/** AgenticTracePanel（AgenticRAG优化方案.md §8 F1，v1）：检索循环时间线。
 *  顶部 decision 徽标（retrieval_skipped → 「直答（未检索）」，非错误空态）+ rounds 步进条
 *  （action/query/grade 图标，rewrite 轮带 rewrite_basis 依据）+ degraded 警示条（常显，
 *  「多轮检索未命中，以下为降级结果」，语义警示色小剂量）。
 *  风格：玻璃 Clear 档（design-system/glass.css .glass-clear）；默认折叠一行摘要，展开看时间线；
 *  framer-motion 步进入场（stagger 80ms ≈ motion.css .stagger 节奏），prefers-reduced-motion 直切；
 *  颜色/圆角/字号全走令牌（tokens.css），零硬编码色值。agentic 缺省（旧响应）→ 不渲染任何节点。
 *  2026-10-04 perf 批：本组件是全仓唯一 framer-motion 使用点，ChatStream 改 React.lazy
 *  异步挂载（degraded 横幅拆出 AgenticDegradedBanner.tsx 静态首载），framer-motion 家族
 *  （motion-dom 等 ~130KB min）随之剥离 ChatPage 首包进异步 chunk。 */

const DECISION_REASON_TEXT: Record<AgenticBlock['decision_reason'], string> = {
  deterministic_task: '确定性任务，无需检索',
  smalltalk_pattern: '识别为寒暄，跳过检索',
  default_retrieve: '默认走检索管线',
}

const GRADE_REASON_TEXT: Record<AgenticGradeReason, string> = {
  pass: '命中达标',
  hit_count_zero: '零命中',
  score_below_threshold: '得分低于阈值',
  span_missing: '证据跨度缺失',
}

/** rounds 步进条子项 variants（stagger 由父级编排；reduce-motion 时不挂载动画属性） */
const roundItem = {
  hidden: { opacity: 0, y: 8 },
  show: { opacity: 1, y: 0 },
}

export function AgenticTracePanel({ agentic, className = '' }: { agentic?: AgenticBlock | null; className?: string }) {
  const [open, setOpen] = useState(false)
  const reduceMotion = useReducedMotion()
  // 旧响应兼容红线：无 agentic 块（null/undefined）→ 面板整体不渲染，绝不报错
  if (!agentic) return null
  const skipped = agentic.decision === 'retrieval_skipped'
  const degraded = agentic.degraded === 'agentic_exhausted'

  return (
    <div data-testid="agentic-trace-panel" className={`glass-clear mt-2 rounded-xl ${className}`}>
      {/* 折叠态一行摘要：decision 徽标 + 决策依据 + 展开箭头（控件层玻璃 Clear 档） */}
      <button
        type="button"
        data-testid="agentic-trace-toggle"
        aria-expanded={open}
        title={open ? '收起检索循环时间线' : '展开检索循环时间线'}
        onClick={() => setOpen(v => !v)}
        className="flex w-full items-center gap-1.5 rounded-xl px-3 py-2 text-left"
      >
        <Waypoints size={12} className="flex-none text-accent" aria-hidden />
        {skipped ? (
          <span data-testid="agentic-decision-badge" className="badge b-blue flex-none px-[7px] py-px text-2xs">
            直答（未检索）
          </span>
        ) : (
          <span
            data-testid="agentic-decision-badge"
            className={`badge flex-none px-[7px] py-px text-2xs ${degraded ? 'b-orange' : 'b-green'}`}
          >
            检索 {agentic.rounds.length} 轮{degraded ? ' · 降级' : ''}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-2xs text-label-2">{DECISION_REASON_TEXT[agentic.decision_reason]}</span>
        <ChevronDown
          size={12}
          aria-hidden
          className={`flex-none text-label-3 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {/* degraded 警示条：不随折叠隐藏（§8.2 F2「不误导用户当权威答案」）；orange 语义色小剂量 */}
      {degraded && (
        <div className="mx-2.5 mb-1.5">
          <AgenticDegradedBanner />
        </div>
      )}

      {/* 展开时间线：高度/透明度过渡 250ms（--dur-2 档，ease 对齐 tokens --ease 曲线）；步进 stagger 80ms */}
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="agentic-timeline"
            data-testid="agentic-timeline"
            initial={reduceMotion ? false : { height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={reduceMotion ? undefined : { height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: [0.25, 0.1, 0.25, 1] }}
            className="overflow-hidden"
          >
            <div className="border-t border-separator px-3 pb-2.5 pt-2">
              {agentic.rounds.length === 0 ? (
                <div data-testid="agentic-rounds-empty" className="py-0.5 text-2xs text-label-3">
                  规则判定跳过检索，本轮无检索轮次。
                </div>
              ) : (
                <motion.ol
                  data-testid="agentic-rounds"
                  className="ml-1 space-y-2.5 border-l border-separator py-0.5 pl-3"
                  {...(reduceMotion
                    ? {}
                    : {
                        initial: 'hidden',
                        animate: 'show',
                        variants: { show: { transition: { staggerChildren: 0.08 } } },
                      })}
                >
                  {agentic.rounds.map(r => (
                    <motion.li
                      key={r.seq}
                      data-testid={`agentic-round-${r.seq}`}
                      className="relative"
                      {...(reduceMotion ? {} : { variants: roundItem })}
                    >
                      {/* 评分节点：pass=绿勾 / fail=橙叉（语义色走令牌内联，rail 定位仅几何偏移） */}
                      <span
                        aria-hidden
                        className="absolute -left-[19px] top-0 flex h-4 w-4 flex-none items-center justify-center rounded-full"
                        style={{ background: r.grade === 'pass' ? 'var(--green-soft)' : 'var(--orange-soft)', color: r.grade === 'pass' ? 'var(--green)' : 'var(--orange)' }}
                      >
                        {r.grade === 'pass' ? <Check size={10} /> : <X size={10} />}
                      </span>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className={`badge ${r.grade === 'pass' ? 'b-green' : 'b-orange'} flex-none px-[7px] py-px text-2xs`}>
                          {r.action === 'rewrite_search' ? '改写检索' : '检索'} · 第 {r.seq} 轮
                        </span>
                        <span className={`font-mono text-2xs ${r.grade === 'pass' ? 'text-label-3' : 'text-orange'}`}>
                          {GRADE_REASON_TEXT[r.grade_reason]}
                        </span>
                      </div>
                      <div data-testid={`agentic-round-query-${r.seq}`} className="mono mt-0.5 truncate text-2xs text-label" title={r.query}>
                        <Search size={9} aria-hidden className="mr-1 inline text-label-3" />
                        {r.query}
                      </div>
                      {r.action === 'rewrite_search' && r.rewrite_basis && (
                        <div data-testid={`agentic-rewrite-basis-${r.seq}`} className="mt-0.5 flex items-center gap-1 text-2xs text-teal">
                          <PenLine size={9} aria-hidden className="flex-none" />
                          <span className="mono truncate">改写依据 · {r.rewrite_basis}</span>
                        </div>
                      )}
                    </motion.li>
                  ))}
                </motion.ol>
              )}
              {/* 全程可追溯（设计宪法 5）：解释链 trace id + 决策档位 */}
              <div className="mt-2 truncate font-mono text-2xs text-label-3">
                trace {agentic.explain_trace_id} · mode {agentic.mode}
              </div>
              {/* F4 骨架：next_actions 建议卡（v1.5 待后端接入，mock 数据驱动，禁用态） */}
              <NextActionsCard className="mt-2" />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
