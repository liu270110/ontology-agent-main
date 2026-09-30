import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ArrowRight, Check } from 'lucide-react'
import { getPreferences, putPreferences } from '@/features/settings/api'

/** 新手引导 · 三步上手（宿主 p-dashboard 新手引导卡；S-AD 切片）：
 *  三步 checklist——①完成首次对话 ②创建本体项目 ③导入知识库文档；完成态判定用
 *  工作台既有 live 数据（最近会话非空 / 本体项目非空 / 知识文档非空，复用 useDashboardQueries
 *  缓存不重复拉，见 DashboardPage）。全完成 → PUT /me/preferences 写 onboarding_done=true
 *  （写一次，ref 防抖）→ 卡片隐藏；已 done（GET 读回 true）→ 不渲染。
 *  样式 = .card 玻璃 + 三行 checklist（完成项绿勾，未完成空心圈 + 跳转链接）。 */

interface Step {
  key: string
  title: string
  doneText: string
  todoText: string
  to: string
  linkText: string
  done: boolean
}

export function OnboardingCard({
  chatDone,
  projectDone,
  docsDone,
  ready,
}: {
  /** ①最近会话非空（sessionsQ 数据就绪后判定） */
  chatDone: boolean
  /** ②本体项目非空（ontoQ） */
  projectDone: boolean
  /** ③知识文档非空（docsQ；live 挂起 → 保持未完成） */
  docsDone: boolean
  /** 三路判定数据是否已就绪（未就绪不渲染，防闪烁误判） */
  ready: boolean
}) {
  const qc = useQueryClient()
  // 引导完成标记：GET /me/preferences 读回（已 done → 不渲染）
  const prefsQ = useQuery({ queryKey: ['dashboard', 'onboarding-prefs'], queryFn: getPreferences })
  const [writtenDone, setWrittenDone] = useState(false)
  const writingRef = useRef(false)

  const prefsDone = prefsQ.data?.onboarding_done === true
  const allDone = chatDone && projectDone && docsDone

  useEffect(() => {
    // 偏好未读回前不写（防已 done 用户重复 PUT）
    if (writtenDone || writingRef.current || prefsDone || prefsQ.isPending || !ready || !allDone) return
    writingRef.current = true
    void putPreferences({ onboarding_done: true })
      .then(() => setWrittenDone(true))
      .then(() => qc.invalidateQueries({ queryKey: ['dashboard', 'onboarding-prefs'] }))
      .catch(() => { /* 写失败保持可见，下次进入重试 */ })
      .finally(() => { writingRef.current = false })
  }, [allDone, prefsDone, prefsQ.isPending, ready, writtenDone, qc])

  // 已 done（GET 读回或本会话刚写入）→ 不渲染
  if (prefsDone || writtenDone) return null
  // 偏好/判定数据未就绪 → 先不渲染（避免把加载中误判为未完成、或已 done 用户闪见卡片）
  if (!ready || prefsQ.isPending) return null

  const steps: Step[] = [
    {
      key: 'chat', title: '完成首次对话', doneText: '已完成 · 最近会话已就绪', todoText: '带证据溯源的第一次提问',
      to: '/chat', linkText: '去对话', done: chatDone,
    },
    {
      key: 'ontology', title: '创建本体项目', doneText: '已完成 · 项目已在库', todoText: '从本体工作台新建并建模',
      to: '/ontology', linkText: '去新建', done: projectDone,
    },
    {
      key: 'docs', title: '导入知识库文档', doneText: '已完成 · 文档已在库', todoText: '上传 PDF / Word / Excel 触发抽取',
      to: '/kb', linkText: '去上传', done: docsDone,
    },
  ]
  const doneCount = steps.filter(s => s.done).length

  return (
    <section className="card glass-interactive rounded-xl border border-separator bg-surface" data-testid="onboarding-card" aria-label="新手引导">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-bold">新手引导 · 三步上手</h3>
        <span className={`badge ${doneCount === steps.length ? 'b-green' : 'b-gray'}`} data-testid="onboarding-progress">
          {doneCount}/{steps.length} 完成
        </span>
        <span className="ml-auto text-[11px] text-label-3">完成即隐藏（/me/preferences）</span>
      </div>
      <div className="mt-2">
        {steps.map(s => (
          <div
            key={s.key}
            className="flex flex-wrap items-center gap-2.5 border-t border-separator px-0.5 py-2.5 first:border-t-0"
            data-testid={`onboarding-step-${s.key}`}
          >
            {s.done ? (
              <span className="flex h-[18px] w-[18px] flex-none items-center justify-center rounded-full" style={{ background: 'var(--green-soft)', color: 'var(--green)' }} aria-hidden>
                <Check size={11} strokeWidth={3} />
              </span>
            ) : (
              <span className="h-[18px] w-[18px] flex-none rounded-full border-[1.5px] border-separator" aria-hidden />
            )}
            <b className="text-[13px]">{s.title}</b>
            <span className="text-[11.5px] text-label-3">{s.done ? s.doneText : s.todoText}</span>
            {!s.done && (
              <Link to={s.to} className="ml-auto flex items-center gap-0.5 text-[11px] text-accent hover:underline" data-testid={`onboarding-link-${s.key}`}>
                {s.linkText}
                <ArrowRight size={11} aria-hidden />
              </Link>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}
