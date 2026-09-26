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
  { path: '/tasks', title: '任务中心', icon: 'list', group: '工作台', roles: AUTHED },
  { path: '/ontology', title: '本体工作台', icon: 'cube', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'] },
  { path: '/ontology/versions', title: '版本与评审', icon: 'branch', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'] },
  { path: '/kb', title: '知识库', icon: 'book', group: '语义资产', roles: AUTHED },
  { path: '/kb/playground', title: '检索 Playground', icon: 'flask', group: '语义资产', roles: ['admin', 'ontologist', 'curator', 'super_admin'] },
  { path: '/memory', title: '记忆管理', icon: 'layers', group: '语义资产', roles: AUTHED },
  { path: '/agents', title: 'Agent 管理', icon: 'bot', group: '能力', roles: ['admin', 'curator', 'member', 'super_admin'] },
  { path: '/workflows', title: '工作流编排', icon: 'branch', group: '能力', roles: ['admin', 'member', 'super_admin'] },
  { path: '/extensions/market', title: '插件市场', icon: 'puzzle', group: '能力', roles: AUTHED },
  { path: '/extensions/tools', title: '工具与技能', icon: 'wrench', group: '能力', roles: ['admin', 'ontologist', 'member', 'super_admin'] },
  { path: '/extensions/mcp', title: 'MCP 管理', icon: 'plug', group: '能力', roles: ['admin', 'ontologist', 'member', 'super_admin'] },
  { path: '/system', title: '系统管理', icon: 'gear', group: '治理', permission: 'user:manage', roles: ['admin', 'super_admin'] },
  { path: '/settings', title: '个人设置', icon: 'user', group: '工作台', hidden: true, roles: AUTHED },
]
