import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Sparkles, Wrench } from 'lucide-react'
import { toast } from 'sonner'
import { Sheet } from '@/components/sheet'
import { getSkill, type SkillRow } from '../api'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** IX-TLS-03 技能 SKILL.md 查看抽屉（26 篇 §9.2；画板 ix-tls-03）：520px——
 *  SKILL.md 渲染（frontmatter 元数据卡 + 正文 Markdown）+ 版本/依赖
 *  +「在 Agent 中启用」（→ IX-AGT-03 ToolPicker 预选该技能，/agents/:id?tab=tools&pick=）。
 *  数据 GET /skills/{id}（R 预登记，见 R 清单）。 */

export function SkillSheet({ skill, onClose }: { skill: SkillRow | null; onClose: () => void }) {
  const navigate = useNavigate()
  const { data } = useQuery({
    queryKey: ['skills', 'detail', skill?.id],
    queryFn: () => getSkill(skill!.id),
    enabled: !!skill,
  })

  if (!skill) return null
  const d = data

  function enableInAgent() {
    toast('在 Agent 中启用该技能', {
      description: '跳转 ToolPicker 并预选该技能；分发动作留审计。演示目标：agt-nanobot-01（可在列表换选）',
    })
    navigate(`/agents/agt-nanobot-01?tab=tools&pick=${skill!.id}`)
    onClose()
  }

  return (
    <Sheet open onClose={onClose} title={skill.name} width={520}>
      <div className="px-5 py-4">
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 flex-none items-center justify-center rounded-xl" style={{ background: 'var(--orange-soft)' }}>
            <Sparkles size={16} className="text-orange" aria-hidden />
          </span>
          <div>
            <div className="flex flex-wrap items-center gap-1.5">
              <b className="text-[14.5px]">{skill.name}</b>
              <span className="badge b-gray">SKILL.md · {skill.version}</span>
              {skill.status === '未分发' && <span className="badge b-orange">未分发</span>}
            </div>
            <div className="mt-0.5 text-[11px] text-label-3">{skill.summary}</div>
            {skill.status === '已启用' && (
              <span className="badge b-purple mt-1">程序记忆 · 与记忆服务打通</span>
            )}
          </div>
        </div>

        {/* frontmatter 元数据卡 */}
        <div className="mt-4 rounded-xl border border-separator p-3.5" style={{ background: 'var(--surface)' }} data-testid="tls-frontmatter">
          <div className="flex items-center gap-2">
            <span className="badge b-gray">frontmatter</span>
            <span className="text-[10.5px] text-label-3">YAML 元数据</span>
          </div>
          <div className="mt-2 divide-y" style={{ borderColor: 'var(--separator)' }}>
            {(d?.frontmatter ?? []).map(f => (
              <div key={f.key} className="flex items-center gap-2 py-1.5 text-[11.5px]">
                <span className="mono badge b-blue">{f.key}</span>
                <span className="mono text-label-2">{f.value}</span>
              </div>
            ))}
          </div>
        </div>

        {/* 正文 Markdown */}
        <div className="markdown mt-4 text-[12px] leading-6" data-testid="tls-skill-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{d?.body ?? ''}</ReactMarkdown>
        </div>

        {/* 版本与依赖 */}
        <div className="mt-4 flex flex-wrap items-center gap-1.5 text-[11px]">
          <span className="text-label-3">版本 {d?.version ?? skill.version} · 依赖 {d?.depends_tools.length ?? 0} 个工具：</span>
          {(d?.depends_tools ?? []).map(t => (
            <span key={t} className="badge b-gray">{t}</span>
          ))}
          {(d?.depends_tools ?? []).length === 0 && <span className="badge b-gray">无工具依赖</span>}
        </div>

        <button type="button" className="btn btn-p mt-4 w-full" data-testid="tls-skill-enable" onClick={enableInAgent}>
          <Wrench size={13} aria-hidden /> 在 Agent 中启用
        </button>
        <div className="fhint">
          跳转 IX-AGT-03 ToolPicker（/agents/:id?tab=tools）并预选该技能；分发动作留审计。api/01 §5.6 skills 读取（R 预登记）。
        </div>
      </div>
    </Sheet>
  )
}
