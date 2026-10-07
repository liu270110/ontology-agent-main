import { Suspense, lazy } from 'react'
import type { ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'
import { ApiError } from '@/api/client'
import { AppShell } from './AppShell'
import { ConsoleShell, ConsoleIndex } from './ConsoleShell'
import { PlatformShell } from './PlatformShell'
import { RequireAuth } from './RequireAuth'
import { RouteGuard } from './RouteGuard'
import { ErrorBoundary } from './ErrorBoundary'
import { NotFoundPage } from './NotFoundPage'
import { CommandMenu } from '@/components/command-menu/command-menu'
import { ThemeProvider } from './providers/theme-provider'
import { CONSOLE_ROUTES, MAIN_ROUTES, PLATFORM_ROUTES, type RouteMeta } from './routes'
import { PlaceholderPage } from './PlaceholderPage'
import { LoginPage } from '@/features/auth/pages/LoginPage'

/** P-012 retry 豁免（台账-生产化-2026-10-07）：查询失败的重试分流——
 *  ① 4xx 族不重试：404（资源缺失，重试必再 404）、信封 code=1004（路由未实装=断供态，
 *     httpStatus 可能缺失，按业务码豁免）、2xxx 权限族（重试不会改变判定结果）——
 *     重试放大故障面且拖慢错误态呈现，直接失败；
 *  ② 网络层错误（code=-1 断网/-2 超时）与 5xx 服务端错误：保留既有 retry:1（瞬时故障可能自愈）；
 *  ③ 非 ApiError（编程错误等）：维持既有 retry:1 不变。
 *  注：MutationCache 全局 onError 有意不加——各操作就地 toast，全局兜底会造成 toast 风暴（维持现状）。
 *  导出供测试锁定策略（404 不重试：mock 404 端点 + query 配置下 fetch 计数=1）。 */
export function retryQueryOnError(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError) {
    if (error.httpStatus !== undefined && error.httpStatus >= 400 && error.httpStatus < 500) return false
    if (error.code === 1004 || (error.code >= 2000 && error.code < 3000)) return false
  }
  return failureCount < 1
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: retryQueryOnError, refetchOnWindowFocus: false } },
})

// S8 路由级代码分割：域页面懒加载（首包=框架+登录；29 篇 FE-ADR-FE3 依赖台账配套）
const DashboardPage = lazy(() => import('@/features/dashboard/pages/DashboardPage').then(m => ({ default: m.DashboardPage })))
const ChatPage = lazy(() => import('@/features/chat/pages/ChatPage').then(m => ({ default: m.ChatPage })))
const GroupChatPage = lazy(() => import('@/features/group/pages/GroupChatPage').then(m => ({ default: m.GroupChatPage })))
// S9 会话轨迹回放（画框21；最小接线——用户 2026-10-01 已裁决允许 App.tsx 两行）
const TrajectoryPage = lazy(() => import('@/features/trajectory/pages/TrajectoryPage').then(m => ({ default: m.TrajectoryPage })))
const WorkflowListPage = lazy(() => import('@/features/workflow/pages/WorkflowListPage').then(m => ({ default: m.WorkflowListPage })))
const WorkflowEditorPage = lazy(() => import('@/features/workflow/pages/WorkflowEditorPage').then(m => ({ default: m.WorkflowEditorPage })))
const DocumentsPage = lazy(() => import('@/features/kb/pages/DocumentsPage').then(m => ({ default: m.DocumentsPage })))
const ReviewPage = lazy(() => import('@/features/kb/pages/ReviewPage').then(m => ({ default: m.ReviewPage })))
const PlaygroundPage = lazy(() => import('@/features/kb/pages/PlaygroundPage').then(m => ({ default: m.PlaygroundPage })))
const ProjectListPage = lazy(() => import('@/features/ontology/pages/ProjectListPage').then(m => ({ default: m.ProjectListPage })))
const WorkbenchPage = lazy(() => import('@/features/ontology/pages/WorkbenchPage').then(m => ({ default: m.WorkbenchPage })))
const VersionsPage = lazy(() => import('@/features/ontology/pages/VersionsPage').then(m => ({ default: m.VersionsPage })))
const ExplorePage = lazy(() => import('@/features/explore/pages/ExplorePage').then(m => ({ default: m.ExplorePage })))
const MemoryPage = lazy(() => import('@/features/memory/pages/MemoryPage').then(m => ({ default: m.MemoryPage })))
const AgentListPage = lazy(() => import('@/features/agents/pages/AgentListPage').then(m => ({ default: m.AgentListPage })))
const AgentDetailPage = lazy(() => import('@/features/agents/pages/AgentDetailPage').then(m => ({ default: m.AgentDetailPage })))
const MarketPage = lazy(() => import('@/features/market/pages/MarketPage').then(m => ({ default: m.MarketPage })))
const ToolsPage = lazy(() => import('@/features/tools/pages/ToolsPage').then(m => ({ default: m.ToolsPage })))
const McpPage = lazy(() => import('@/features/mcp/pages/McpPage').then(m => ({ default: m.McpPage })))
const ApprovalListPage = lazy(() => import('@/features/approvals/pages/ApprovalListPage').then(m => ({ default: m.ApprovalListPage })))
const AdminPage = lazy(() => import('@/features/admin/pages/AdminPage').then(m => ({ default: m.AdminPage })))
const TasksPage = lazy(() => import('@/features/tasks/pages/TasksPage').then(m => ({ default: m.TasksPage })))
const SettingsPage = lazy(() => import('@/features/settings/pages/SettingsPage').then(m => ({ default: m.SettingsPage })))
// 四区 IA：平台能力区总览（PlatformShell index）
const PlatformPage = lazy(() => import('@/features/platform/pages/PlatformPage').then(m => ({ default: m.PlatformPage })))

/** 已挂实页的路由（M1 起增量）；其余路由元数据渲染壳内占位页（30 篇 §2 切片表）。
 *  双区 IA：管理页宿主路径改挂 /console/*（ConsoleShell）；四区 IA：扩展中心三页与
 *  Agent 目录挂 /platform/*（PlatformShell），旧路径由 LEGACY_REDIRECTS 保书签。 */
const PAGES: Record<string, React.ReactNode> = {
  '/': <DashboardPage />, // 主页启动台（双区 IA，2026-09-28 裁决）
  '/chat': <ChatPage />,
  '/chat/new': <ChatPage />, // IX-EX-01 去对话深链（?entity= 携带实体上下文）
  '/chat/group/:sessionId': <GroupChatPage />, // S7 协作域：Agent 群聊（IX-GRP-01~05）
  '/chat/group': <GroupChatPage />, // S7 协作域：建群入口（无会话空态，GRP-01 触发）
  '/chat/:sessionId/trajectory': <TrajectoryPage />, // S9 会话轨迹回放（画框21；消息操作组「查看轨迹」入口）
  '/workflows': <WorkflowListPage />, // S7 协作域：工作流列表（IX-GRP-06）
  '/workflows/:id': <WorkflowEditorPage />, // S7 协作域：工作流编辑器（IX-GRP-07~11）
  '/kb': <DocumentsPage />, // S3 知识域：文档管理（IX-KB-01~04）
  '/kb/review': <ReviewPage />, // S3 知识域：抽取审核台（IX-REV-01~05）
  '/kb/playground': <PlaygroundPage />, // S3 知识域：检索 Playground（IX-PG-01~03）
  '/kb/explore/:kbId': <ExplorePage />, // S4 知识域：图谱浏览（IX-EX-01~04）
  '/ontology': <ProjectListPage />, // S4 本体域：项目列表（IX-OL-01~02）
  '/ontology/:projectId': <WorkbenchPage />, // S4 本体域：工作台（IX-ON-01~08）
  '/ontology/:projectId/versions': <VersionsPage />, // S4 本体域：版本评审（IX-VR-01~05）
  '/memory': <MemoryPage />, // S5 平台域：记忆管理（IX-MEM-01~03）
  '/agents/:agentId': <AgentDetailPage />, // S5 平台域：Agent 详情四 Tab（IX-AGT-02/03；四区后仍宿主主页区）
  '/platform': <PlatformPage />, // 四区 IA：平台能力总览（IX-PLT-01，PlatformShell index）
  '/platform/market': <MarketPage />, // S5 扩展中心：插件市场（IX-MKT-01~03；四区迁入）
  '/platform/tools': <ToolsPage />, // S5 扩展中心：工具与技能（IX-TLS-01~03；四区迁入）
  '/platform/mcp': <McpPage />, // S5 扩展中心：MCP 接入（IX-MCP-01~03；四区迁入）
  '/platform/agents': <AgentListPage />, // S5 平台域：Agent 目录（IX-AGT-01/04；四区迁入）
  '/console/approvals': <ApprovalListPage />, // S6 治理域：审批中心（IX-APR-01~02）
  '/console/admin': <AdminPage />, // S6 治理域：系统管理五 Tab（IX-ADM-01~09）
  '/tasks': <TasksPage />, // S6 治理域：任务中心（IX-TSK-01~04）
  '/settings': <SettingsPage />, // S6 治理域：个人设置（IX-SET-01~04）
}

/** 旧路径 → 现路径（保书签；查询串透传，/admin?tab=audit 类深链不丢）。
 *  四区 IA（2026-10-01）：扩展中心三页 /console/* → /platform/*；/agents 列表 → /platform/agents。 */
const LEGACY_REDIRECTS: Array<[from: string, to: string]> = [
  ['/approvals', '/console/approvals'],
  ['/admin', '/console/admin'],
  ['/system', '/console/admin'], // 26 篇宿主路径定稿 /admin：S1 深链守卫旧别名
  ['/console/market', '/platform/market'],
  ['/console/tools', '/platform/tools'],
  ['/console/mcp', '/platform/mcp'],
  ['/agents', '/platform/agents'],
  ['/mcp', '/platform/mcp'],
  ['/marketplace', '/platform/market'],
  ['/tools', '/platform/tools'],
]

/** 旧路径重定向（查询串透传）。包 RequireAuth：匿名深链 /system 仍以原路径进 ?next=（S1 用例）。 */
function LegacyRedirect({ to }: { to: string }) {
  const search = useLocation().search
  return <Navigate to={`${to}${search}`} replace />
}

/** P-004 路由区边界（统一包裹层）：variant="route" + resetKeys=[pathname]——
 *  页面渲染崩只塌路由区（壳侧栏/导航仍在）；切路由（含同型不同参路由）即清兜底态重渲，
 *  免「一处崩溃处处兜底直至整页重载」。 */
function RouteBoundary({ children }: { children: ReactNode }) {
  const { pathname } = useLocation()
  return (
    <ErrorBoundary variant="route" resetKeys={[pathname]}>
      {children}
    </ErrorBoundary>
  )
}

/** 应用根：Provider 装配 + 路由（路由元数据见 routes.tsx）。
 *  层序：ErrorBoundary 兜底 → ThemeProvider（html .dark 切换，30 篇 §3-3）→ Query → Router。
 *  双层兜底（P-004，台账-生产化-2026-10-07）：根部 root 边界兜 providers/壳层崩溃；
 *  每条 lazy 路由 element 内 route 边界（variant="route"）兜页面渲染崩溃——错误只塌路由区，
 *  侧栏/导航仍可用（「重载本页」）；chat 域 lazyCard 手工 catch 绕过保留（降级语义更优）。
 *  受护路由：RequireAuth（认证）→ AppShell / ConsoleShell / PlatformShell → RouteGuard（meta.roles/meta.permission → 403）。
 *  四区 IA：/console、/platform 嵌套布局路由各挂独立壳；旧路径 LegacyRedirect 保书签（查询串透传）。 */
export function App() {
  return (
    <ErrorBoundary>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
          {/* v7 future flags：消除控制台 future-flag 警告，提前对齐 v7 行为（startTransition 包装状态更新 + splat 相对解析） */}
          <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <Suspense fallback={null}>
      <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route
                element={
                  <RequireAuth>
                    <AppShell />
                  </RequireAuth>
                }
              >
                {MAIN_ROUTES.map(meta => (
                  <Route
                    key={meta.path}
                    path={meta.path}
                    element={
                      <RouteGuard meta={meta}>
                        {/* P-004 路由区边界（RouteBoundary）：崩只塌路由区，切路由即复位兜底态 */}
                        <RouteBoundary>{PAGES[meta.path] ?? <PlaceholderPage meta={meta} />}</RouteBoundary>
                      </RouteGuard>
                    }
                  />
                ))}
              </Route>
              {/* 管理控制台区：独立布局独立导航（双区 IA，2026-09-28 裁决；四区收缩=治理/观测） */}
              <Route path="/console" element={<RequireAuth><ConsoleShell /></RequireAuth>}>
                <Route
                  index
                  element={
                    <RouteGuard meta={CONSOLE_INDEX_META}>
                      <ConsoleIndex />
                    </RouteGuard>
                  }
                />
                {CONSOLE_ROUTES.map(meta => (
                  <Route
                    key={meta.path}
                    path={meta.path.replace('/console/', '')}
                    element={
                      <RouteGuard meta={meta}>
                        {/* P-004 路由区边界（RouteBoundary）：崩只塌路由区，切路由即复位兜底态 */}
                        <RouteBoundary>{PAGES[meta.path] ?? <PlaceholderPage meta={meta} />}</RouteBoundary>
                      </RouteGuard>
                    }
                  />
                ))}
              </Route>
              {/* 平台能力区：独立布局独立导航（四区 IA，2026-10-01 裁决；index=平台能力总览） */}
              <Route path="/platform" element={<RequireAuth><PlatformShell /></RequireAuth>}>
                <Route
                  index
                  element={
                    <RouteGuard meta={PLATFORM_INDEX_META}>
                      <RouteBoundary>
                        <PlatformPage />
                      </RouteBoundary>
                    </RouteGuard>
                  }
                />
                {PLATFORM_ROUTES.map(meta => (
                  <Route
                    key={meta.path}
                    path={meta.path.replace('/platform/', '')}
                    element={
                      <RouteGuard meta={meta}>
                        {/* P-004 路由区边界（RouteBoundary）：崩只塌路由区，切路由即复位兜底态 */}
                        <RouteBoundary>{PAGES[meta.path] ?? <PlaceholderPage meta={meta} />}</RouteBoundary>
                      </RouteGuard>
                    }
                  />
                ))}
              </Route>
              {LEGACY_REDIRECTS.map(([from, to]) => (
                <Route
                  key={from}
                  path={from}
                  element={
                    <RequireAuth>
                      <LegacyRedirect to={to} />
                    </RequireAuth>
                  }
                />
              ))}
              {/* 404 全局状态页（39 号对账 G-S1 / 画板 p-status）：通配分支不再静默重定向——
                  独立 404 页展示用户输入路径（保留诊断价值）；包 RequireAuth 与全站口径一致
                  （匿名先登录，回跳仍落本页） */}
              <Route path="*" element={<RequireAuth><NotFoundPage /></RequireAuth>} />
            </Routes>
      </Suspense>
          {/* ⌘K 命令面板上移全局挂载（随 404 裸页引入：NotFoundPage「搜索内容」动作依赖；
              AppShell 内重复挂载已撤——双实例 ⌘K 监听会互相抵消。未登录时路由清单为空，仅空面板） */}
          <CommandMenu />
          </BrowserRouter>
          {/* 全局 Toast（sonner；S3 起供各域操作反馈；z 层随 --z-toast 令牌）。
              v1.1 玻璃主题对齐（36 §B1 收尾）：classNames 映射 elements.css .toast
              （glass-clear 档 + sh-float，令牌单源禁新 hex）；richColors 撤下——实底类型色
              会盖掉玻璃配方，图标语义色（success=--green / error=--red 等 33 §3）改由
              elements.css 的 ol[data-sonner-toaster] 作用域规则绑定令牌（裸类压不过 sonner
              默认 0,2,0 选择器；勿用 <Toaster id> 充当作用域——sonner 按 id 分发派 toast）。 */}
          <Toaster
            position="top-center"
            closeButton
            toastOptions={{ classNames: { toast: 'toast', actionButton: 'btn btn-s btn-sm' } }}
          />
        </QueryClientProvider>
      </ThemeProvider>
    </ErrorBoundary>
  )
}

/** /console 首页守卫元数据（无角色/scope 限制；分区卡片按当前用户可见性过滤） */
const CONSOLE_INDEX_META: RouteMeta = { path: '/console', title: '管理控制台', icon: 'gear', group: '治理' }

/** /platform 总览守卫元数据（四区 IA：浏览与获取=用户能力，AUTHED 全员；子页各自 roles 见 routes.tsx） */
const PLATFORM_INDEX_META: RouteMeta = { path: '/platform', title: '平台能力', icon: 'puzzle', group: '能力' }
