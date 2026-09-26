import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
})

/** 已挂实页的路由（M1 起增量）；其余 ROUTES 元数据路由渲染壳内占位页（30 篇 §2 切片表）。 */
const PAGES: Record<string, React.ReactNode> = {
  '/': <DashboardPage />,
  '/chat': <ChatPage />,
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
        </QueryClientProvider>
      </ThemeProvider>
    </ErrorBoundary>
  )
}
