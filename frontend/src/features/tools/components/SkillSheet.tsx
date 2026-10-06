import { useNavigate } from 'react-router-dom'
import { Sparkles, Wrench } from 'lucide-react'
import { toast } from 'sonner'
import { Sheet } from '@/components/sheet'
import { TOOL_STATUS_LABEL, type SkillRow } from '../api'

/** IX-TLS-03 技能详情抽屉（26 篇 §9.2；画板 ix-tls-03）：520px——S2 SkillOut 元数据详情：
 *  描述/来源 URI/版本/上架状态/来源 origin（repo 扫描 | external 登记）/body 长度
 *  + K5 密钥透出（required/missing_secrets、unprovisioned 警示）+ 时间戳。
 *  诚实态（S2 契约收敛，2026-10-05）：mock 时代的 frontmatter 卡与 SKILL.md 正文渲染无
 *  数据源（S2 详情「不回 body 正文」，body 只回长度 body_bytes），面板移除不造假。
 *  「在 Agent 中启用」→ IX-AGT-03 ToolPicker（/agents/:id?tab=tools）。 */

export function SkillSheet({ skill, onClose }: { skill: SkillRow | null; onClose: () => void }) {
  const navigate = useNavigate()

  if (!skill) return null

  function enableInAgent() {
    toast('在 Agent 中启用该技能', {
      description: '到目标 Agent 详情 → 工具配置 → ToolPicker 勾选该技能（分发动作留审计）',
    })
    onClose()
    navigate('/agents')
  }

  return (
    <Sheet open onClose={onClose} title={skill.name} width={520}>
      <div className="px-5 py-4">
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 flex-none items-center justify-center rounded-xl" style={{ background: 'var(--orange-soft)' }}>
            <Sparkles size={16} className="text-orange" aria-hidden />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <b className="text-sm">{skill.name}</b>
              <span className="badge b-gray">SKILL.md · {skill.version}</span>
              <span className={`badge ${skill.status === 'listed' ? 'b-green' : 'b-gray'}`}>{TOOL_STATUS_LABEL[skill.status]}</span>
            </div>
            <div className="mt-0.5 text-[11px] text-label-3">{skill.description}</div>
          </div>
        </div>

        {/* 元数据卡（S2 SkillOut 逐字段） */}
        <div className="mt-4 rounded-xl border border-separator p-3.5" style={{ background: 'var(--surface)' }} data-testid="tls-skill-meta">
          <div className="flex items-center gap-2">
            <span className="badge b-gray">元数据</span>
            <span className="text-[11px] text-label-3">S2 SkillOut · body 只回长度不回正文</span>
          </div>
          <div className="mt-2 divide-y" style={{ borderColor: 'var(--separator)' }}>
            <div className="flex items-center gap-2 py-1.5 text-[11px]">
              <span className="w-[92px] flex-none text-label-3">来源 URI</span>
              <span className="mono min-w-0 truncate" title={skill.source_uri}>{skill.source_uri}</span>
            </div>
            <div className="flex items-center gap-2 py-1.5 text-[11px]">
              <span className="w-[92px] flex-none text-label-3">来源 origin</span>
              <span className="badge b-blue">{skill.origin === 'repo' ? '本仓资产扫描（repo）' : '外部登记（external）'}</span>
            </div>
            <div className="flex items-center gap-2 py-1.5 text-[11px]">
              <span className="w-[92px] flex-none text-label-3">正文长度</span>
              <span className="mono">{skill.body_bytes.toLocaleString()} bytes（正文不下发）</span>
            </div>
            <div className="flex items-center gap-2 py-1.5 text-[11px]">
              <span className="w-[92px] flex-none text-label-3">登记 / 更新</span>
              <span className="mono">{skill.created_at?.slice(0, 10) ?? '—'} / {skill.updated_at?.slice(0, 10) ?? '—'}</span>
            </div>
          </div>
        </div>

        {/* K5 密钥门透出（docs/Agent/13 §10：required/missing + unprovisioned 可感知不阻断） */}
        <div className="mt-3 rounded-xl border border-separator p-3.5" style={{ background: 'var(--surface)' }} data-testid="tls-skill-secrets">
          <div className="flex items-center gap-2">
            <span className="badge b-gray">密钥供给</span>
            <span className="text-[11px] text-label-3">required_secrets / missing_secrets（K5 门 1/2 透出）</span>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {skill.required_secrets.map(k => (
              <span key={k} className={`badge ${skill.missing_secrets.includes(k) ? 'b-red' : 'b-green'} mono`}>
                {k}
                {skill.missing_secrets.includes(k) ? ' · 缺失' : ' · 已供给'}
              </span>
            ))}
            {skill.required_secrets.length === 0 && <span className="text-[11px] text-label-3">无密钥声明。</span>}
          </div>
          {skill.unprovisioned && (
            <div className="mt-2 rounded-lg px-2.5 py-1.5 text-[11px]" style={{ background: 'var(--orange-soft)' }}>
              <b className="text-orange">unprovisioned</b> · 缺失 {skill.missing_secrets.length} 项：不阻断 listed，消费方可感知（调用前需配齐）。
            </div>
          )}
        </div>

        <div className="mt-3 text-[11px] text-label-3">
          SKILL.md 正文与 frontmatter 不随详情下发（S2 契约：body 只回长度）；正文查看待正文通道端点登记后恢复。
        </div>

        <button type="button" className="btn btn-p mt-4 w-full" data-testid="tls-skill-enable" onClick={enableInAgent}>
          <Wrench size={13} aria-hidden /> 在 Agent 中启用
        </button>
        <div className="fhint">
          跳转 Agent 目录选实例后在工具配置中经 ToolPicker 勾选；分发动作留审计。S2 skills 四端点（14 §3）。
        </div>
      </div>
    </Sheet>
  )
}
