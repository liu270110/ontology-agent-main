import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, Bot, Plug, Puzzle, Sparkles, Wrench, type LucideIcon } from 'lucide-react'
import { toast } from 'sonner'
import { listPlugins } from '@/features/market/api'
import { listSkills, listTools } from '@/features/tools/api'
import { listServers } from '@/features/mcp/api'
import { listAgents } from '@/features/agents/api'

/** 平台能力总览（四区 IA 2026-10-01，画板 p-platform）：用户浏览与获取平台能力的统一入口——
 *  ① 四能力入口卡（插件市场/工具与技能/MCP 接入/Agent 目录，计数复用各域既有 queryKey 取数，
 *  拿不到不显示）+ ② 精选插件行（静态三张，获取/接入 → toast 走审核流演示）+
 *  ③ 四区边界说明卡（本页只做浏览与获取：运行管理在主页、治理在控制台、个性化在设置）。
 *  安装/接入/注册一律走审核流（设计宪法 3：候选非成品，人工终审才生效）。 */

interface Capability {
  key: string
  title: string
  desc: string
  icon: LucideIcon
  /** 图标底色（令牌 CSS 变量，tokens.css 亮暗成对） */
  tint: string
  color: string
  to: string
  /** 计数行（数据不可得时不渲染） */
  count?: string
}

export function PlatformPage() {
  const navigate = useNavigate()

  // 计数取数：与各域页面同 queryKey/queryFn（缓存共享，进出子页不重复请求）；失败/加载中不显示
  const market = useQuery({ queryKey: ['market', 'list'], queryFn: listPlugins })
  const tools = useQuery({ queryKey: ['tools'], queryFn: listTools })
  const skills = useQuery({ queryKey: ['skills', 'list'], queryFn: listSkills })
  const mcp = useQuery({ queryKey: ['mcp', 'list'], queryFn: listServers })
  const agents = useQuery({ queryKey: ['agents', 'list'], queryFn: listAgents })

  const installed = market.data?.items.filter(p => p.installed).length
  const marketCount =
    installed != null && market.data ? `已装 ${installed} · 共 ${market.data.items.length}` : undefined
  const toolsCount =
    tools.data && skills.data ? `工具 ${tools.data.items.length} · 技能 ${skills.data.items.length}` : undefined
  const mcpCount = mcp.data ? `已接入 ${mcp.data.items.length}` : undefined
  const agentsCount = agents.data ? `实例 ${agents.data.items.length}` : undefined

  const capabilities: Capability[] = [
    {
      key: 'market', title: '插件市场', icon: Puzzle,
      desc: '官方与社区插件：接入外部系统、扩展领域动作。安装走投毒扫描 + schema 门禁。',
      tint: 'var(--indigo-soft)', color: 'var(--indigo)', to: '/platform/market', count: marketCount,
    },
    {
      key: 'tools', title: '工具与技能', icon: Wrench,
      desc: '注册工具（REST/CLI）与 SKILL.md 技能：Agent 可调用的原子能力与操作手册。',
      tint: 'var(--teal-soft)', color: 'var(--teal)', to: '/platform/tools', count: toolsCount,
    },
    {
      key: 'mcp', title: 'MCP 接入', icon: Plug,
      desc: 'Model Context Protocol 出口：discover 发现外部 Server → 逐项纳管 → 审核开启。',
      tint: 'var(--orange-soft)', color: 'var(--orange)', to: '/platform/mcp', count: mcpCount,
    },
    {
      key: 'agents', title: 'Agent 目录', icon: Bot,
      desc: '平台托管 Agent 实例目录：适配器、模型、工具授权与调试对话入口。',
      tint: 'var(--green-soft)', color: 'var(--green)', to: '/platform/agents', count: agentsCount,
    },
  ]

  return (
    <div className="mx-auto max-w-[1180px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">平台能力</h1>
        <span className="badge b-blue">浏览 · 获取 · 审核生效</span>
        <span className="text-xs text-label-3">安装 / 接入 / 注册一律走审核流（候选非成品，人工终审才生效）</span>
      </div>

      {/* ① 四能力入口卡（画板 p-platform 四宫格） */}
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {capabilities.map(c => (
          <button
            key={c.key}
            type="button"
            data-testid={`platform-cap-${c.key}`}
            onClick={() => navigate(c.to)}
            className="card glass-interactive rounded-xl border border-separator bg-surface text-left"
          >
            <div className="flex items-center gap-2.5">
              <span
                className="flex h-8 w-8 flex-none items-center justify-center rounded-lg"
                style={{ background: c.tint, color: c.color }}
              >
                <c.icon size={16} aria-hidden />
              </span>
              <b className="min-w-0 truncate text-sm">{c.title}</b>
              <ArrowRight size={14} aria-hidden className="ml-auto flex-none text-label-3" />
            </div>
            <p className="mt-2.5 text-xs leading-5 text-label-2">{c.desc}</p>
            {c.count && <div className="mt-2.5 text-2xs text-label-3" data-testid={`platform-cap-count-${c.key}`}>{c.count}</div>}
          </button>
        ))}
      </div>

      {/* ② 精选插件行（静态三张；获取/接入 → toast 演示审核流） */}
      <div className="mt-6 flex flex-wrap items-center gap-2.5">
        <b className="text-[13px]">精选插件</b>
        <span className="text-2xs text-label-3">获取流程：安装申请 → 治理审核 → 终审生效（候选非成品）</span>
        <button
          type="button"
          data-testid="platform-goto-market"
          onClick={() => navigate('/platform/market')}
          className="ml-auto text-xs text-accent hover:underline"
        >
          进入市场 →
        </button>
      </div>
      <div className="mt-2.5 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <FeaturedCard
          testKey="ledger-connector"
          name="台账写入连接器" badge="官方" badgeClass="badge b-green"
          desc="营销台账系统双向写入：工单下发/回执同步，高危动作走审批。"
          metas={['v2.1', 'scope: write']} action="获取"
        />
        <FeaturedCard
          testKey="grid-shacl"
          name="电网本体校验包" badge="社区" badgeClass="badge b-blue"
          desc="GB/T 国标条款 SHACL 形状包：术语唯一/规则人工把关两条底线内建。"
          metas={['v1.4', 'shape ×38']} action="获取"
        />
        <FeaturedCard
          testKey="scada-mcp"
          name="SCADA 网关 MCP" badge="官方" badgeClass="badge b-green"
          desc="SCADA 实时数据 MCP Server：只读遥测白名单，写操作全部走审批链。"
          metas={['v0.9', 'tools ×6']} action="接入"
        />
      </div>

      {/* ③ 四区边界说明卡（画板 p-platform 底部提示） */}
      <div className="card mt-4 flex items-start gap-3 !bg-surface-2 p-3.5" data-testid="platform-boundary">
        <Sparkles size={14} aria-hidden className="mt-0.5 flex-none text-accent" />
        <p className="text-xs leading-6 text-label-2">
          四区边界：本页只做<b>浏览与获取</b>——已装能力的<b>运行管理</b>在主页各功能页；
          <b>平台治理</b>（用户/审批/审计）在管理控制台；<b>个性化配置</b>在用户设置。
          能力获取一律走审核流，任何档位不可跳过。
        </p>
      </div>
    </div>
  )
}

/** 精选卡（静态展示；获取动作仅 toast 演示——真实安装流程在插件市场页，走安装向导 + scope 授权） */
function FeaturedCard({ testKey, name, badge, badgeClass, desc, metas, action }: {
  testKey: string
  name: string
  badge: string
  badgeClass: string
  desc: string
  metas: string[]
  action: string
}) {
  return (
    <div className="card p-3.5" data-testid={`platform-featured-${testKey}`}>
      <div className="flex items-center gap-2">
        <b className="min-w-0 truncate text-xs">{name}</b>
        <span className={`${badgeClass} flex-none text-2xs`}>{badge}</span>
      </div>
      <p className="mt-1.5 text-[11px] leading-5 text-label-2">{desc}</p>
      <div className="mt-2.5 flex items-center gap-1.5">
        {metas.map(m => (
          <span key={m} className="badge b-gray text-2xs">{m}</span>
        ))}
        <button
          type="button"
          data-testid="platform-featured-get"
          onClick={() => toast.success('获取申请已提交，走审核流（M4 前端演示）')}
          className="btn btn-s btn-sm ml-auto"
        >
          {action}
        </button>
      </div>
    </div>
  )
}
