/** 路由元数据（16 篇 §4.1，权威=03 篇 §4 路由表 + 08 篇 §2.2 角色矩阵）。仅作导航渲染与
 *  权限清单的事实源；元素装配见 App.tsx。页面随里程碑增量挂载。
 *  过滤纪律：菜单/路由守卫按 roles（JWT roles claim，08 篇 §2.2 角色码）粗过滤 +
 *  permission（11 篇 资源:动作 scope）细判；前端隐藏≠授权，服务端 PDP 兜底。
 *
 *  双区 IA（2026-09-28 用户裁决）：主页区（AppShell 宿主，常用功能启动台）＋
 *  管理控制台区（ConsoleShell 宿主，治理/能力配置/观测）。管理类路由迁至 /console/*，
 *  旧路径经 App.tsx LegacyRedirect 保书签（查询串透传，/admin?tab=audit 类深链不丢）。
 *
 *  四区 IA（2026-10-01 用户裁决）：在双区之上新增平台能力区——/platform（PlatformShell 宿主，
 *  用户浏览与获取平台能力的统一入口：总览/插件市场/工具与技能/MCP 接入/Agent 目录）；
 *  扩展中心三页自 /console/* 迁出、/agents 列表迁入（旧路径 LegacyRedirect 保书签）；
 *  管理控制台收缩为治理/观测（审批中心 + 系统管理，审计=系统管理 audit Tab）。
 *  主侧边栏「独立页面」分组（STANDALONE_NAV）为跨分区入口：平台能力/管理控制台/用户设置。 */

/** 角色码权威=08 篇 §2.2：admin/ontologist/curator/member/guest（+平台级 super_admin） */
export type RoleCode = 'admin' | 'ontologist' | 'curator' | 'member' | 'guest' | 'super_admin'

/** 认证用户可见角色集（guest 仅公开入口，不进壳内菜单） */
export const AUTHED_ROLES: RoleCode[] = ['admin', 'ontologist', 'curator', 'member', 'super_admin']

const AUTHED = AUTHED_ROLES

/** 控制台窄导航分组（ConsoleShell 左栏；四区收缩后=治理/观测，能力配置三页迁 /platform） */
export type ConsoleGroup = '治理' | '观测'

export interface RouteMeta {
  path: string
  title: string
  icon: string
  group: '工作台' | '语义资产' | '能力' | '治理'
  /** 双区 IA：true=管理控制台区（ConsoleShell 宿主，不进主侧边栏）；缺省=主页区（AppShell 宿主） */
  console?: boolean
  /** 控制台窄导航分组（console=true 时消费，ConsoleShell） */
  consoleGroup?: ConsoleGroup
  /** 四区 IA：true=平台能力区（PlatformShell 宿主，不进主侧边栏；浏览/获取=用户能力，总览与市场 AUTHED） */
  platform?: boolean
  /** 细粒度 scope（11 篇 资源:动作）；RouteGuard 判缺 → 403 */
  permission?: string
  /** 角色粗过滤（08 §2.2 矩阵映射）；缺省 = 全部认证角色可见 */
  roles?: RoleCode[]
  hidden?: boolean
  /** 主侧边栏收敛（四区 IA 只留 7 常用项 + 独立页面分组）：false=不进主侧边栏（⌘K/深链仍可达） */
  sidebar?: boolean
}

export const ROUTES: RouteMeta[] = [
  // ---- 主页区（AppShell 宿主）----
  // 主页启动台：主侧边栏不再单列（Logo 点击回主页），⌘K/深链可达
  { path: '/', title: '主页', icon: 'grid', group: '工作台', hidden: true, roles: AUTHED },
  { path: '/chat', title: '对话', icon: 'chat', group: '工作台', roles: AUTHED },
  // /chat/new?entity=（IX-EX-01 去对话深链；hidden，元素同 ChatPage）
  { path: '/chat/new', title: '新对话', icon: 'chat', group: '工作台', roles: AUTHED, hidden: true },
  // S7 协作域：Agent 群聊（27 篇 P14 / 26 篇 §15 GRP-01~05；hidden=深链直达，入口在群聊页 ＋。
  // /chat/group 无会话态 = 建群入口页，同元素渲染空态）
  { path: '/chat/group/:sessionId', title: 'Agent 群聊', icon: 'chat', group: '工作台', roles: AUTHED, hidden: true },
  { path: '/chat/group', title: '群聊', icon: 'spark', group: '工作台', roles: AUTHED },
  // S9 会话轨迹回放（画框21 / 08 篇只追加事件流；hidden=消息操作组「查看轨迹」深链直达，
  // RouteGuard 沿 AUTHED，会话主人可见性由服务端 PDP 兜底）
  { path: '/chat/:sessionId/trajectory', title: '轨迹回放', icon: 'list', group: '工作台', roles: AUTHED, hidden: true },
  // S7 协作域：工作流编排（27 篇 P15；hidden=编辑器深链直达，列表入主侧边栏）
  { path: '/workflows', title: '工作流', icon: 'workflow', group: '工作台', roles: ['admin', 'ontologist', 'super_admin'] },
  { path: '/workflows/:id', title: '工作流编辑器', icon: 'workflow', group: '工作台', roles: ['admin', 'ontologist', 'super_admin'], hidden: true },
  { path: '/tasks', title: '任务中心', icon: 'list', group: '工作台', roles: AUTHED },
  { path: '/ontology', title: '本体工作台', icon: 'cube', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'] },
  // 版本与评审 / 检索 Playground：主侧边栏收敛不单列（sidebar:false，⌘K/页内入口/深链可达）
  { path: '/ontology/versions', title: '版本与评审', icon: 'branch', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'], sidebar: false },
  // S4 本体域动态路由（hidden=侧栏/cmdk 不露，深链直达；roles 对齐 08 篇 §2.2——ontologist/admin
  // 可编辑、curator 评审，页面内写操作再按 scope can('ontology:write') 展示过滤）
  { path: '/ontology/:projectId', title: '本体工作台', icon: 'cube', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'], hidden: true },
  { path: '/ontology/:projectId/versions', title: '版本评审', icon: 'branch', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'], hidden: true },
  { path: '/kb', title: '知识库', icon: 'book', group: '语义资产', roles: AUTHED },
  // 图谱浏览（S4 IX-EX-01~04；/kb 深链 + cmdk 不露）
  { path: '/kb/explore/:kbId', title: '图谱浏览', icon: 'book', group: '语义资产', roles: AUTHED, hidden: true },
  // 抽取审核台（S3）：review 终审门禁限 curator/admin；hidden=画板侧栏不单列（/kb 深链 + cmdk 不露），路由可直达
  { path: '/kb/review', title: '抽取审核', icon: 'book', group: '语义资产', roles: ['admin', 'curator', 'super_admin'], hidden: true },
  { path: '/kb/playground', title: '检索 Playground', icon: 'flask', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'], sidebar: false },
  { path: '/memory', title: '记忆管理', icon: 'layers', group: '语义资产', roles: AUTHED },
  // S5 平台域动态路由（hidden=侧栏/cmdk 不露，深链直达；IX-AGT-02 详情四 Tab ?tab=info|tools|adapter|history）
  // 四区 IA：详情仍宿主主页区（自 /platform/agents 列表跳入），列表路由迁 /platform/agents
  { path: '/agents/:agentId', title: 'Agent 详情', icon: 'bot', group: '能力', roles: ['admin', 'curator', 'member', 'super_admin'], hidden: true },
  { path: '/settings', title: '个人设置', icon: 'user', group: '工作台', hidden: true, roles: AUTHED },

  // ---- 平台能力区（platform=true，PlatformShell 宿主；四区 IA 2026-10-01 用户裁决：
  // 扩展中心三页自 /console/* 迁出 + /agents 列表迁入；旧路径由 App.tsx LegacyRedirect 保书签）----
  // 权限口径：浏览与获取是用户能力——总览/市场 AUTHED 全员；tools/mcp 沿用原口径（08 §2.2 无 curator，
  // 安装/接入写操作页内再按 scope 过滤）；agents 沿用原口径（无 ontologist）
  { path: '/platform/market', title: '插件市场', icon: 'puzzle', group: '能力', platform: true, roles: AUTHED },
  { path: '/platform/tools', title: '工具与技能', icon: 'wrench', group: '能力', platform: true, roles: ['admin', 'ontologist', 'member', 'super_admin'] },
  { path: '/platform/mcp', title: 'MCP 接入', icon: 'plug', group: '能力', platform: true, roles: ['admin', 'ontologist', 'member', 'super_admin'] },
  { path: '/platform/agents', title: 'Agent 目录', icon: 'bot', group: '能力', platform: true, roles: ['admin', 'curator', 'member', 'super_admin'] },

  // ---- 管理控制台区（console=true，ConsoleShell 宿主；旧路径 /approvals /admin /system 由
  // App.tsx LegacyRedirect 映射，查询串透传。四区收缩：能力配置三页已迁 /platform/*）----
  // S6 审批中心（26 篇 §10.1 p-approve）：终审门禁限 admin/curator（super_admin 平台级豁免）
  { path: '/console/approvals', title: '审批中心', icon: 'shield', group: '治理', console: true, consoleGroup: '治理', roles: ['admin', 'curator', 'super_admin'] },
  // S6 系统管理（26 篇 §10.2 宿主路径 /admin→/console/admin；Tab 深链 ?tab=users|groups|roles|
  // models|audit，租户 Tab super_admin 可见）；审计日志控制台入口 = /console/admin?tab=audit
  { path: '/console/admin', title: '系统管理', icon: 'gear', group: '治理', console: true, consoleGroup: '治理', permission: 'user:manage', roles: ['admin', 'super_admin'] },
]

/** 主页区路由（AppShell 侧栏/⌘K 事实源） */
export const MAIN_ROUTES = ROUTES.filter(r => !r.console && !r.platform)
/** 管理控制台区路由（ConsoleShell 宿主 + 控制台窄导航/首页卡片事实源） */
export const CONSOLE_ROUTES = ROUTES.filter(r => r.console)
/** 平台能力区路由（PlatformShell 宿主；/platform 总览为壳 index，不入此表） */
export const PLATFORM_ROUTES = ROUTES.filter(r => r.platform)

/** 主侧边栏「独立页面」分组（四区 IA 2026-10-01）：跨分区入口，非 AppShell 宿主路由
 *  （/platform=PlatformShell、/console=ConsoleShell、/settings=AppShell hidden 深链）。 */
export interface StandaloneNavItem {
  title: string
  to: string
  icon: string
  desc: string
}
export const STANDALONE_NAV: StandaloneNavItem[] = [
  { title: '平台能力', to: '/platform', icon: 'puzzle', desc: '插件市场 · 工具技能 · MCP · Agent 目录' },
  { title: '管理控制台', to: '/console', icon: 'shield', desc: '审批中心 · 系统管理 · 审计观测' },
  { title: '用户设置', to: '/settings', icon: 'user', desc: '个人资料与偏好' },
]
