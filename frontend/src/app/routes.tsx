/** 路由元数据（16 篇 §4.1，权威=03 篇 §4 路由表 + 08 篇 §2.2 角色矩阵）。仅作导航渲染与
 *  权限清单的事实源；元素装配见 App.tsx。页面随里程碑增量挂载。
 *  过滤纪律：菜单/路由守卫按 roles（JWT roles claim，08 篇 §2.2 角色码）粗过滤 +
 *  permission（11 篇 资源:动作 scope）细判；前端隐藏≠授权，服务端 PDP 兜底。 */

/** 角色码权威=08 篇 §2.2：admin/ontologist/curator/member/guest（+平台级 super_admin） */
export type RoleCode = 'admin' | 'ontologist' | 'curator' | 'member' | 'guest' | 'super_admin'

/** 认证用户可见角色集（guest 仅公开入口，不进壳内菜单） */
const AUTHED: RoleCode[] = ['admin', 'ontologist', 'curator', 'member', 'super_admin']

export interface RouteMeta {
  path: string
  title: string
  icon: string
  group: '工作台' | '语义资产' | '能力' | '治理'
  /** 细粒度 scope（11 篇 资源:动作）；RouteGuard 判缺 → 403 */
  permission?: string
  /** 角色粗过滤（08 §2.2 矩阵映射）；缺省 = 全部认证角色可见 */
  roles?: RoleCode[]
  hidden?: boolean
}

export const ROUTES: RouteMeta[] = [
  { path: '/', title: '工作台', icon: 'grid', group: '工作台', roles: AUTHED },
  { path: '/chat', title: '对话', icon: 'chat', group: '工作台', roles: AUTHED },
  // /chat/new?entity=（IX-EX-01 去对话深链；hidden，元素同 ChatPage）
  { path: '/chat/new', title: '新对话', icon: 'chat', group: '工作台', roles: AUTHED, hidden: true },
  { path: '/tasks', title: '任务中心', icon: 'list', group: '工作台', roles: AUTHED },
  // S7 协作域：Agent 群聊（27 篇 P14 / 26 篇 §15 GRP-01~05；hidden=深链直达，入口在群聊页 ＋。
  // /chat/group 无会话态 = 建群入口页，同元素渲染空态）
  { path: '/chat/group/:sessionId', title: 'Agent 群聊', icon: 'chat', group: '工作台', roles: AUTHED, hidden: true },
  { path: '/chat/group', title: '群聊', icon: 'spark', group: '工作台', roles: AUTHED },
  { path: '/ontology', title: '本体工作台', icon: 'cube', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'] },
  { path: '/ontology/versions', title: '版本与评审', icon: 'branch', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'] },
  // S4 本体域动态路由（hidden=侧栏/cmdk 不露，深链直达；roles 对齐 08 篇 §2.2——ontologist/admin
  // 可编辑、curator 评审，页面内写操作再按 scope can('ontology:write') 展示过滤）
  { path: '/ontology/:projectId', title: '本体工作台', icon: 'cube', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'], hidden: true },
  { path: '/ontology/:projectId/versions', title: '版本评审', icon: 'branch', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'], hidden: true },
  { path: '/kb', title: '知识库', icon: 'book', group: '语义资产', roles: AUTHED },
  // 图谱浏览（S4 IX-EX-01~04；/kb 深链 + cmdk 不露）
  { path: '/kb/explore/:kbId', title: '图谱浏览', icon: 'book', group: '语义资产', roles: AUTHED, hidden: true },
  // 抽取审核台（S3）：review 终审门禁限 curator/admin；hidden=画板侧栏不单列（/kb 深链 + cmdk 不露），路由可直达
  { path: '/kb/review', title: '抽取审核', icon: 'book', group: '语义资产', roles: ['admin', 'curator', 'super_admin'], hidden: true },
  { path: '/kb/playground', title: '检索 Playground', icon: 'flask', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'] },
  { path: '/memory', title: '记忆管理', icon: 'layers', group: '语义资产', roles: AUTHED },
  // S5 平台域动态路由（hidden=侧栏/cmdk 不露，深链直达；IX-AGT-02 详情四 Tab ?tab=info|tools|adapter|history）
  { path: '/agents/:agentId', title: 'Agent 详情', icon: 'bot', group: '能力', roles: ['admin', 'curator', 'member', 'super_admin'], hidden: true },
  { path: '/agents', title: 'Agent 管理', icon: 'bot', group: '能力', roles: ['admin', 'curator', 'member', 'super_admin'] },
  // S7 协作域：工作流编排（27 篇 P15，roles 对齐 08 篇 §2.2 矩阵——admin/ontologist 编排，
  // 运行全员由资源级 ACL 控制；hidden=编辑器深链直达，/workflows 列表入侧栏）
  { path: '/workflows', title: '工作流编排', icon: 'workflow', group: '能力', roles: ['admin', 'ontologist', 'super_admin'] },
  { path: '/workflows/:id', title: '工作流编辑器', icon: 'workflow', group: '能力', roles: ['admin', 'ontologist', 'super_admin'], hidden: true },
  // S5 扩展中心三页：26 篇 §9 宿主路径（/marketplace、/tools、/mcp；与画板 ix-07 一致）。
  // market 全员可见；tools/mcp 无 curator（08 篇 §2.2 矩阵）——安装/接入写操作页内再按 scope 过滤
  { path: '/marketplace', title: '插件市场', icon: 'puzzle', group: '能力', roles: AUTHED },
  { path: '/tools', title: '工具与技能', icon: 'wrench', group: '能力', roles: ['admin', 'ontologist', 'member', 'super_admin'] },
  { path: '/mcp', title: 'MCP 管理', icon: 'plug', group: '能力', roles: ['admin', 'ontologist', 'member', 'super_admin'] },
  // S6 治理域：26 篇 §10.2 宿主路径 /admin（Tab 深链 ?tab=users|groups|roles|models|audit，
  // 租户 Tab super_admin 可见）；roles 对齐 08 篇 §2.2（08 §2.2 矩阵「租户/用户/密钥管理」归 admin）
  { path: '/admin', title: '系统管理', icon: 'gear', group: '治理', permission: 'user:manage', roles: ['admin', 'super_admin'] },
  // /system 旧路径别名（S1 深链守卫用例 next=%2Fsystem 依赖；hidden 不入菜单，直达重定向 /admin）
  { path: '/system', title: '系统管理', icon: 'gear', group: '治理', roles: ['admin', 'super_admin'], hidden: true },
  // S6 审批中心（26 篇 §10.1 p-approve）：终审门禁限 admin/curator（super_admin 平台级豁免）
  { path: '/approvals', title: '审批中心', icon: 'shield', group: '治理', roles: ['admin', 'curator', 'super_admin'] },
  { path: '/settings', title: '个人设置', icon: 'user', group: '工作台', hidden: true, roles: AUTHED },
]
