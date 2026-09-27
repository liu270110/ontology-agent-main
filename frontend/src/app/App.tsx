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
import { DashboardPage } from '@/features/dashboard/pages/DashboardPage'
import { ChatPage } from '@/features/chat/pages/ChatPage'
import { GroupChatPage } from '@/features/group/pages/GroupChatPage'
import { WorkflowListPage } from '@/features/workflow/pages/WorkflowListPage'
import { WorkflowEditorPage } from '@/features/workflow/pages/WorkflowEditorPage'
import { DocumentsPage } from '@/features/kb/pages/DocumentsPage'
import { ReviewPage } from '@/features/kb/pages/ReviewPage'
import { PlaygroundPage } from '@/features/kb/pages/PlaygroundPage'
import { ProjectListPage } from '@/features/ontology/pages/ProjectListPage'
import { WorkbenchPage } from '@/features/ontology/pages/WorkbenchPage'
import { VersionsPage } from '@/features/ontology/pages/VersionsPage'
import { ExplorePage } from '@/features/explore/pages/ExplorePage'
import { MemoryPage } from '@/features/memory/pages/MemoryPage'
import { AgentListPage } from '@/features/agents/pages/AgentListPage'
import { AgentDetailPage } from '@/features/agents/pages/AgentDetailPage'
import { MarketPage } from '@/features/market/pages/MarketPage'
import { ToolsPage } from '@/features/tools/pages/ToolsPage'
import { McpPage } from '@/features/mcp/pages/McpPage'
import { ApprovalListPage } from '@/features/approvals/pages/ApprovalListPage'
import { AdminPage } from '@/features/admin/pages/AdminPage'
import { TasksPage } from '@/features/tasks/pages/TasksPage'
import { SettingsPage } from '@/features/settings/pages/SettingsPage'

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
})

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
          </BrowserRouter>
          {/* 全局 Toast（sonner；S3 起供各域操作反馈；z 层随 --z-toast 令牌） */}
          <Toaster position="top-center" richColors closeButton />
        </QueryClientProvider>
      </ThemeProvider>
    </ErrorBoundary>
  )
}
