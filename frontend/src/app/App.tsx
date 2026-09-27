import { Suspense, lazy } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'
import { AppShell } from './AppShell'
import { RequireAuth } from './RequireAuth'
import { RouteGuard } from './RouteGuard'
import { ErrorBoundary } from './ErrorBoundary'
import { ThemeProvider } from './providers/theme-provider'
import { ROUTES } from './routes'
import { PlaceholderPage } from './PlaceholderPage'
import { LoginPage } from '@/features/auth/pages/LoginPage'

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
})

// S8 路由级代码分割：域页面懒加载（首包=框架+登录；29 篇 FE-ADR-FE3 依赖台账配套）
const DashboardPage = lazy(() => import('@/features/dashboard/pages/DashboardPage').then(m => ({ default: m.DashboardPage })))
const ChatPage = lazy(() => import('@/features/chat/pages/ChatPage').then(m => ({ default: m.ChatPage })))
const GroupChatPage = lazy(() => import('@/features/group/pages/GroupChatPage').then(m => ({ default: m.GroupChatPage })))
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

/** 已挂实页的路由（M1 起增量）；其余 ROUTES 元数据路由渲染壳内占位页（30 篇 §2 切片表）。 */
const PAGES: Record<string, React.ReactNode> = {
  '/': <DashboardPage />,
  '/chat': <ChatPage />,
  '/chat/new': <ChatPage />, // IX-EX-01 去对话深链（?entity= 携带实体上下文）
  '/chat/group/:sessionId': <GroupChatPage />, // S7 协作域：Agent 群聊（IX-GRP-01~05）
  '/chat/group': <GroupChatPage />, // S7 协作域：建群入口（无会话空态，GRP-01 触发）
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
  '/agents': <AgentListPage />, // S5 平台域：Agent 卡片列表（IX-AGT-01/04）
  '/agents/:agentId': <AgentDetailPage />, // S5 平台域：Agent 详情四 Tab（IX-AGT-02/03）
  '/marketplace': <MarketPage />, // S5 扩展中心：插件市场（IX-MKT-01~03）
  '/tools': <ToolsPage />, // S5 扩展中心：工具与技能（IX-TLS-01~03）
  '/mcp': <McpPage />, // S5 扩展中心：MCP 管理（IX-MCP-01~03）
  '/approvals': <ApprovalListPage />, // S6 治理域：审批中心（IX-APR-01~02）
  '/admin': <AdminPage />, // S6 治理域：系统管理五 Tab（IX-ADM-01~09）
  '/tasks': <TasksPage />, // S6 治理域：任务中心（IX-TSK-01~04）
  '/settings': <SettingsPage />, // S6 治理域：个人设置（IX-SET-01~04）
  '/system': <Navigate to="/admin" replace />, // 26 篇宿主路径定稿 /admin：旧路径别名重定向
}

/** 应用根：Provider 装配 + 路由（路由元数据见 routes.tsx）。
 *  层序：ErrorBoundary 兜底 → ThemeProvider（html .dark 切换，30 篇 §3-3）→ Query → Router。
 *  受护路由：RequireAuth（认证）→ AppShell → RouteGuard（meta.roles/meta.permission → 403 占位）。 */
export function App() {
  return (
    <ErrorBoundary>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
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
                {ROUTES.map(meta => (
                  <Route
                    key={meta.path}
                    path={meta.path}
                    element={
                      <RouteGuard meta={meta}>{PAGES[meta.path] ?? <PlaceholderPage meta={meta} />}</RouteGuard>
                    }
                  />
                ))}
              </Route>
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
      </Suspense>
          </BrowserRouter>
          {/* 全局 Toast（sonner；S3 起供各域操作反馈；z 层随 --z-toast 令牌） */}
          <Toaster position="top-center" richColors closeButton />
        </QueryClientProvider>
      </ThemeProvider>
    </ErrorBoundary>
  )
}
