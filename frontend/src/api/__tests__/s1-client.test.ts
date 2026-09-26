import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { api } from '@/api/client'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))
afterEach(() => {
  server.resetHandlers()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

/** S1 ④ 401 拦截：access 失效 → 单飞（single-flight）静默 POST /auth/refresh → 成功重放原请求；
 *  refresh 也失效（1003）→ 清会话（16 篇 §5.2 ④ / api/01 §5.9）。 */

function seedSession() {
  // 直接构造假会话（claims 形状与 handlers 签发的假 JWT 一致）
  localStorage.setItem(
    'oa-auth',
    JSON.stringify({
      accessToken: [
        'eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0',
        btoa(JSON.stringify({ sub: 'u-admin', tenant_id: 't-1', roles: ['admin'], scopes: ['session:chat'], typ: 'access', exp: 1 })).replace(/=+$/, ''),
        'sig',
      ].join('.'),
      refreshToken: [
        'eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0',
        btoa(JSON.stringify({ sub: 'u-admin', tenant_id: 't-1', roles: ['admin'], scopes: ['session:chat'], typ: 'refresh', exp: 9999999999 })).replace(/=+$/, ''),
        'sig',
      ].join('.'),
      expiresAt: Date.now() - 1000,
      email: 'admin@example.com',
    }),
  )
  useAuthStore.setState({
    status: 'authenticated',
    accessToken: JSON.parse(localStorage.getItem('oa-auth')!).accessToken,
    refreshToken: JSON.parse(localStorage.getItem('oa-auth')!).refreshToken,
    expiresAt: Date.now() - 1000,
    user: { id: 'u-admin', email: 'admin@example.com', displayName: 'admin', tenantId: 't-1', roles: ['admin'], scopes: ['session:chat'] },
  })
}

describe('S1 401 单飞刷新重放（api/client）', () => {
  it('首次 401 → refresh 200 → 原请求重放成功，且 store 换发新 access', async () => {
    seedSession()
    let calls = 0
    let refreshCalls = 0
    server.use(
      http.get('*/api/v1/sessions', ({ request }) => {
        calls++
        const auth = request.headers.get('Authorization') ?? ''
        if (auth.endsWith('sig')) {
          return HttpResponse.json({ code: 1001, message: '未认证', data: null }, { status: 401 })
        }
        return HttpResponse.json({
          code: 0,
          message: 'ok',
          data: { items: [{ id: 's-1', title: '重放成功', agent_id: 'nanobot', updated_at: '' }], next_cursor: null },
        })
      }),
      http.post('*/api/v1/auth/refresh', async () => {
        refreshCalls++
        const pair = await fetch('/api/v1/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: 'admin@example.com', password: 'password123' }),
        }).then(r => r.json())
        return HttpResponse.json({ code: 0, message: 'ok', data: pair.data })
      }),
    )

    const data = await api.get<{ items: { title: string }[] }>('/sessions')

    expect(data.items[0].title).toBe('重放成功')
    expect(calls).toBe(2) // 首次 401 + 刷新后重放一次
    expect(refreshCalls).toBe(1) // 单飞只刷一次
    expect(useAuthStore.getState().accessToken).not.toBeNull()
  })

  it('refresh 也失败（1003）→ 清会话（status 回 anonymous）并抛 1003', async () => {
    seedSession()
    server.use(
      http.get('*/api/v1/sessions', () => HttpResponse.json({ code: 1001, message: '未认证', data: null }, { status: 401 })),
      http.post('*/api/v1/auth/refresh', () => HttpResponse.json({ code: 1003, message: '刷新令牌无效', data: null }, { status: 401 })),
    )

    await expect(api.get('/sessions')).rejects.toMatchObject({ code: 1003 })
    // client.sessionExpired 已清 store（导航跳转在 jsdom 中静默）
    expect(useAuthStore.getState().status).toBe('anonymous')
    expect(useAuthStore.getState().refreshToken).toBeNull()
  })
})
