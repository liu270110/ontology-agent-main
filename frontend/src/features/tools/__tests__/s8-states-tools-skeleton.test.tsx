import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管，
// 本文件不重复 listen（重复会抛 Invariant Violation）；用例内仍以 server.use(...) 注入可控数据。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S8 状态切片 · /platform/tools 工具与技能（s8-states）：MSW 注入延迟（不动 src/mocks/handlers.ts）。
 *  ① 工具注册表延迟 → 骨架行先出现 → 数据到达后骨架消失；
 *  ② 技能库延迟 → 骨架卡先出现 → 数据到达后骨架消失。
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   首屏骨架断言依赖空缓存；①② 查询键不同互不污染。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 工具注册表 · 加载骨架', () => {
  it('延迟 → 骨架行先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/tools', async () => {
        await delay(800)
        // S1 live 投影形状（裸 {data,meta}；ToolOut 逐字段）
        return HttpResponse.json({
          data: [
            {
              id: '0b1e3a10-0000-4000-8000-00000000s801', tenant_id: 't-demo', name: 'skeleton.tool',
              action_iri: 'ont_core#CL-900', source_channel: 'L0',
              semantic_annotation: { label: '骨架验证', description: '骨架验证工具' },
              version: '1.0.0', status: 'listed', health_hint: null, evidence_uri: null,
            },
          ],
          meta: { page: 1, page_size: 20, total: 1 },
        })
      }),
    )
    await loginAndGo('/platform/tools')

    expect(await screen.findByTestId('skeleton-rows', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId('tool-tr-skeleton.tool', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()
  }, 30_000)
})

describe('S8 状态切片 · 技能库 · 加载骨架', () => {
  it('切到技能库 → 延迟 → 骨架卡先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/skills', async () => {
        await delay(800)
        // S2 live 投影形状（裸 {data,meta}；SkillOut 逐字段）
        return HttpResponse.json({
          data: [
            {
              id: '3f2a7c10-0000-4000-8000-00000000s801', name: '骨架验证技能',
              description: '骨架屏验证用技能', source_uri: 'services/skills/skeleton/SKILL.md',
              version: '1.0.0', status: 'listed', body_bytes: 100, origin: 'repo',
              required_secrets: [], missing_secrets: [], unprovisioned: false,
              created_at: null, updated_at: null,
            },
          ],
          meta: { page: 1, page_size: 20, total: 1 },
        })
      }),
    )
    await loginAndGo('/platform/tools')

    fireEvent.click(await screen.findByTestId('tls-view-skills', {}, { timeout: 10_000 }))

    expect(await screen.findByTestId('skeleton-cards', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId('skill-card-3f2a7c10-0000-4000-8000-00000000s801', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-cards')).not.toBeInTheDocument()
  }, 30_000)
})
