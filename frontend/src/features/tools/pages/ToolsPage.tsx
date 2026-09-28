import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, Plus, Wrench } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonCards, SkeletonRows } from '@/components/states'
import { listSkills, listTools, setToolEnabled, TOOL_SOURCE_LABEL, type SkillRow, type ToolRow } from '../api'
import { ToolDetailSheet } from '../components/ToolDetailSheet'
import { RegisterToolModal } from '../components/RegisterToolModal'
import { SkillSheet } from '../components/SkillSheet'

/** /tools 工具与技能（宿主画框 p-tools；26 篇 §9.2）：
 *  seg 两视图=工具注册表 / 技能库；工具表（名称/来源徽标/scope/状态/启停开关）
 *  + IX-TLS-01 详情抽屉 + IX-TLS-02 注册工具 + IX-TLS-03 SKILL.md 抽屉。 */

export function ToolsPage() {
  const qc = useQueryClient()
  const [view, setView] = useState<'tools' | 'skills'>('tools')
  const [detail, setDetail] = useState<ToolRow | null>(null)
  const [registerOpen, setRegisterOpen] = useState(false)
  const [skill, setSkill] = useState<SkillRow | null>(null)

  const tools = useQuery({ queryKey: ['tools'], queryFn: listTools })
  const skills = useQuery({ queryKey: ['skills', 'list'], queryFn: listSkills, enabled: view === 'skills' })

  const toggle = useMutation({
    mutationFn: (t: ToolRow) => setToolEnabled(t.id, !t.enabled),
    onSuccess: res => {
      void qc.invalidateQueries({ queryKey: ['tools'] })
      toast.success(`${res.id} 已${res.enabled ? '启用' : '停用'}`, { description: '启停写审计；运行中会话不受影响，新调用即刻生效' })
    },
  })

  const rows = useMemo(() => tools.data?.items ?? [], [tools])

  return (
    <div className="mx-auto max-w-[1080px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">工具与技能</h1>
        <span className="text-xs text-label-3">注册中心 = 平台能力出口清单 · 外部工具默认不可信（逐项审核开启）</span>
        {view === 'tools' && (
          <button type="button" className="btn btn-p btn-sm ml-auto" onClick={() => setRegisterOpen(true)} data-testid="tls-register-open">
            <Plus size={13} aria-hidden /> 注册工具
          </button>
        )}
      </div>

      <div className="seg mt-3" role="tablist" aria-label="工具与技能视图">
        <button type="button" role="tab" aria-selected={view === 'tools'} className={`seg-btn ${view === 'tools' ? 'on' : ''}`} data-testid="tls-view-tools" onClick={() => setView('tools')}>
          工具注册表 {rows.length ? rows.length : ''}
        </button>
        <button type="button" role="tab" aria-selected={view === 'skills'} className={`seg-btn ${view === 'skills' ? 'on' : ''}`} data-testid="tls-view-skills" onClick={() => setView('skills')}>
          技能库 {skills.data?.items.length ?? ''}
        </button>
      </div>

      {/* 工具注册表 */}
      {view === 'tools' && (
        <div className="card mt-4 !p-0" data-testid="tls-table">
          <table className="tbl">
            <thead>
              <tr>
                <th>工具</th>
                <th>来源</th>
                <th>提供方</th>
                <th>scope</th>
                <th>状态</th>
                <th className="text-right">启停</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(t => (
                <tr key={t.id} className="cursor-pointer" data-testid={`tool-tr-${t.name}`} onClick={() => setDetail(t)}>
                  <td>
                    <span className="mono font-semibold">{t.name}</span>
                    {t.danger && <span className="badge b-red ml-1.5">高危</span>}
                    <div className="text-[11px] text-label-3">{t.desc}</div>
                  </td>
                  <td>
                    <span className={`badge ${t.source === 'mcp' ? 'b-purple' : t.source === 'plugin' ? 'b-blue' : t.source === 'http' ? 'b-orange' : 'b-green'}`}>
                      {TOOL_SOURCE_LABEL[t.source]}
                    </span>
                  </td>
                  <td className="text-[11px] text-label-2">{t.provider}</td>
                  <td className="text-[11px]">{t.scopes.length ? t.scopes.join('、') : '—'}</td>
                  <td>
                    <span className={`badge ${t.enabled ? 'b-green' : 'b-gray'}`}>{t.enabled ? '已启用' : '已停用'}</span>
                  </td>
                  <td className="text-right">
                    <button
                      type="button"
                      role="switch"
                      aria-checked={t.enabled}
                      aria-label={`启停 ${t.name}`}
                      data-testid={`tool-switch-${t.name}`}
                      className="relative h-5 w-9 rounded-full transition-colors"
                      style={{ background: t.enabled ? 'var(--green)' : 'var(--surface-2)', border: '1px solid var(--separator)' }}
                      onClick={e => {
                        e.stopPropagation()
                        toggle.mutate(t)
                      }}
                    >
                      <span
                        className="absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white shadow transition-all"
                        style={{ left: t.enabled ? 18 : 3 }}
                      />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* S8 状态切片：加载骨架行（行数≈mock 工具量）/ 错误态（重试=refetch） */}
          {tools.isLoading && (
            <div className="!p-4">
              <SkeletonRows rows={5} />
            </div>
          )}
        </div>
      )}

      {view === 'tools' && tools.isError && (
        <div className="mt-3">
          <ErrorState
            message={tools.error instanceof Error ? tools.error.message : undefined}
            code={tools.error instanceof ApiError ? tools.error.code : undefined}
            onRetry={() => void tools.refetch()}
          />
        </div>
      )}

      {/* 技能库 */}
      {view === 'skills' && (
        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3" data-testid="tls-skills">
          {(skills.data?.items ?? []).map(s => (
            <button
              type="button"
              key={s.id}
              className="card flex flex-col !p-4 text-left transition-colors hover:border-accent"
              data-testid={`skill-card-${s.id}`}
              onClick={() => setSkill(s)}
            >
              <div className="flex items-center gap-2">
                <b className="truncate text-[13px]">{s.name}</b>
                <span className="badge b-gray ml-auto">SKILL.md · {s.version}</span>
              </div>
              <p className="mt-2 line-clamp-3 min-h-12 text-[11px] leading-4 text-label-2">{s.summary}</p>
              <div className="hairline-t mt-3 flex items-center gap-2 pt-2.5 text-[11px] text-label-3">
                <span className={`badge ${s.status === '已启用' ? 'b-green' : 'b-orange'}`}>{s.status}</span>
                <span className="ml-auto flex items-center gap-0.5 text-accent">
                  查看详情 <ArrowUpRight size={11} aria-hidden />
                </span>
              </div>
            </button>
          ))}
          {/* S8 状态切片：加载骨架卡 / 错误态（重试=refetch） */}
          {skills.isLoading && <SkeletonCards count={3} />}
          {skills.isError && (
            <div className="col-span-full">
              <ErrorState
                message={skills.error instanceof Error ? skills.error.message : undefined}
                code={skills.error instanceof ApiError ? skills.error.code : undefined}
                onRetry={() => void skills.refetch()}
              />
            </div>
          )}
        </div>
      )}

      <ToolDetailSheet tool={detail} onClose={() => setDetail(null)} />
      <RegisterToolModal open={registerOpen} onClose={() => setRegisterOpen(false)} />
      <SkillSheet skill={skill} onClose={() => setSkill(null)} />

      {view === 'tools' && (
        <div className="mt-3 flex items-center gap-2 text-[11px] text-label-3">
          <Wrench size={12} aria-hidden />
          工具目录供 Agent 挑选引用；MCP 来源工具统一经平台网关调用并逐次审计。
        </div>
      )}
    </div>
  )
}
