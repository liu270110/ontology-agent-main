import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { ApiError, api } from '@/api/client'
import { disableUser, getUser, listUsers, updateUser } from '../api'

// listen/resetHandlers/close 由全局 setupFiles（src/mocks/node-setup.ts）统一管理，测试不自管
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S6 用户 CRUD · B8-WC 契约卡终对齐（2026-10-04=后端 iam 域实装 services/iam/api/users.py
 *  投影，tests/gateway/test_admin_users.py 同口径）：
 *  ① 列表三维筛选 query/status/role + department 恒 '—' + invited_via/invite_link_id 显式 null
 *     + u-07（invited）行删除（invited 真实数据恒无）
 *  ② 编辑改角色：GRANTABLE_ROLES 选择器（guest/analyst/super_admin 下掉）+ PATCH 全量替换载荷
 *     + roles=[] 前端同规拦截（后端 422/3001）
 *  ③ 停用（DELETE=200 信封 {id,status:'disabled'}）→ 行转已停用 → 启用（PATCH status 回程）
 *  ④ 409 三向：删自己（USER_SELF_DISABLE）/ 删超管+改绑超管角色（SUPER_ADMIN_PROTECTED）/
 *     guest 授予（ROLE_NOT_GRANTABLE 409）；analyst 未种子化 422
 *  ⑤ 契约边角：POST /admin/users 405、单条详情与 404 码统一（无 4041）、软删幂等 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S6 用户 CRUD（B8-WC 契约卡）', () => {
  it('① 列表三维筛选 + department 恒 — + invited 行删除 + 显式 null 序列化', async () => {
    const all = await listUsers()
    // ② u-07（invited）行删除：invited 真实数据恒无
    expect(all.data.map(u => u.id)).not.toContain('u-07')
    expect(all.data.some(u => u.status === 'invited')).toBe(false)
    // ① department 恒 '—'（占位展示位）；③ invited_via/invite_link_id 显式 null
    expect(all.data.length).toBeGreaterThan(0)
    expect(all.data.every(u => u.department === '—')).toBe(true)
    expect(all.data.every(u => u.invited_via === null && u.invite_link_id === null)).toBe(true)

    // 三维筛选（参数名=api/01 §5.8：query / status / role）
    const disabled = await listUsers({ status: 'disabled' })
    expect(disabled.data.map(u => u.id)).toEqual(['u-08'])
    const ontologists = await listUsers({ role: 'ontologist' })
    expect(ontologists.data.map(u => u.id).sort()).toEqual(['u-02', 'u-05'])
    const byQuery = await listUsers({ query: 'wang' })
    expect(byQuery.data.map(u => u.id)).toEqual(['u-02'])
    const byQueryName = await listUsers({ query: '钱进' })
    expect(byQueryName.data.map(u => u.id)).toEqual(['u-08'])
  }, 30_000)

  it('② 编辑改角色：可授予选择器收口 + PATCH 全量替换载荷 + roles=[] 拦截', async () => {
    const patches: Record<string, unknown>[] = []
    server.use(
      http.patch('*/api/v1/admin/users/u-04', async ({ request }) => {
        patches.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            id: 'u-04', username: '陈晨', email: 'chen.chen@example.com', display_name: '陈晨',
            roles: ['member', 'curator'], department: '—', status: 'active',
            last_login_at: '2026-09-26T11:47:00Z', invited_via: null, invite_link_id: null,
          },
        })
      }),
    )

    await loginAndGo('/admin?tab=users')
    expect(await screen.findByTestId('adm-user-u-04', {}, { timeout: 10_000 })).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('adm-user-edit-u-04'))
    const dialog = await screen.findByRole('dialog', { name: '编辑成员 · 陈晨' })

    // 契约卡：下拉/勾选仅 GRANTABLE_ROLES——guest（409）、analyst（未种子化 422）、super_admin 均不出现
    expect(within(dialog).getByTestId('adm-edit-role-member')).toBeInTheDocument()
    expect(within(dialog).getByTestId('adm-edit-role-curator')).toBeInTheDocument()
    expect(within(dialog).getByTestId('adm-edit-role-ontologist')).toBeInTheDocument()
    expect(within(dialog).getByTestId('adm-edit-role-admin')).toBeInTheDocument()
    expect(within(dialog).queryByTestId('adm-edit-role-guest')).not.toBeInTheDocument()
    expect(within(dialog).queryByTestId('adm-edit-role-analyst')).not.toBeInTheDocument()
    expect(within(dialog).queryByTestId('adm-edit-role-super_admin')).not.toBeInTheDocument()
    // department 展示位（无编辑字段）
    expect(within(dialog).getByTestId('adm-edit-dept')).toHaveTextContent('—')

    // roles=[] 前端同规拦截：取消唯一角色 → 保存禁用 + 提示
    fireEvent.click(within(dialog).getByTestId('adm-edit-role-member'))
    expect(within(dialog).getByTestId('adm-edit-save')).toBeDisabled()
    expect(within(dialog).getByTestId('adm-edit-roles-empty')).toHaveTextContent('至少保留一个角色')
    fireEvent.click(within(dialog).getByTestId('adm-edit-role-member'))

    // 勾选 curator → 保存 → PATCH 载荷=display_name + roles 全量替换（无 department 字段编辑）
    fireEvent.click(within(dialog).getByTestId('adm-edit-role-curator'))
    fireEvent.click(within(dialog).getByTestId('adm-edit-save'))
    await waitFor(() => expect(patches).toHaveLength(1))
    expect(patches[0]).toEqual({ display_name: '陈晨', roles: ['member', 'curator'] })
    expect(await screen.findByText(/用户已更新/)).toBeInTheDocument()
  }, 30_000)

  it('③ 停用 → 200 信封 {id,status:disabled} 行转已停用 → 启用回程', async () => {
    await loginAndGo('/admin?tab=users')
    expect(await screen.findByTestId('adm-user-u-04', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 停用（danger 弹窗输用户名确认）
    fireEvent.click(screen.getByTestId('adm-user-disable-u-04'))
    const dialog = await screen.findByRole('dialog', { name: '停用成员 · 陈晨' })
    fireEvent.change(within(dialog).getByTestId('adm-disable-name'), { target: { value: '陈晨' } })
    fireEvent.click(within(dialog).getByTestId('adm-disable-confirm'))
    expect(await screen.findByText(/已停用 陈晨（软删可追溯，会话保留）/)).toBeInTheDocument()

    // 行转已停用 + 出现启用按钮（DELETE=200 信封 {id,status:'disabled'}，api 层解析）
    const row = await screen.findByTestId('adm-user-u-04')
    expect(row).toHaveTextContent('已停用')
    fireEvent.click(screen.getByTestId('adm-user-enable-u-04'))
    expect(await screen.findByText(/已启用，该成员可重新登录/)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('adm-user-u-04')).toHaveTextContent('在职'))
    // 启用后编辑/停用按钮回归
    expect(screen.getByTestId('adm-user-disable-u-04')).toBeInTheDocument()
  }, 30_000)

  it('④ 409 三向：删自己 / 删超管+改绑超管角色 / guest 授予；analyst 422', async () => {
    await loginAndGo('/admin?tab=users')
    expect(await screen.findByTestId('adm-user-u-01', {}, { timeout: 10_000 })).toBeInTheDocument()

    // a. 禁删自己（u-01=当前登录 admin@example.com）→ 409/3409 文案按 message 透出
    fireEvent.click(screen.getByTestId('adm-user-disable-u-01'))
    const selfDialog = await screen.findByRole('dialog', { name: '停用成员 · 刘以在' })
    fireEvent.change(within(selfDialog).getByTestId('adm-disable-name'), { target: { value: '刘以在' } })
    fireEvent.click(within(selfDialog).getByTestId('adm-disable-confirm'))
    expect(await screen.findByText(/USER_SELF_DISABLE: 不可停用当前登录账号自己/)).toBeInTheDocument()
    fireEvent.click(within(selfDialog).getByText('取消'))

    // b. 删超管（u-09 super_admin）→ 409/3409 超管保护
    fireEvent.click(screen.getByTestId('adm-user-disable-u-09'))
    const saDialog = await screen.findByRole('dialog', { name: '停用成员 · 平台超管' })
    fireEvent.change(within(saDialog).getByTestId('adm-disable-name'), { target: { value: '平台超管' } })
    fireEvent.click(within(saDialog).getByTestId('adm-disable-confirm'))
    expect(await screen.findByText(/SUPER_ADMIN_PROTECTED: 平台超管账号不可停用\/删除\/改绑角色/)).toBeInTheDocument()
    fireEvent.click(within(saDialog).getByText('取消'))

    // c-e. api 级语义：guest 授予 409 / 改绑超管角色 409 / analyst 未种子化 422 / roles=[] 422
    await expect(updateUser('u-04', { roles: ['guest'] })).rejects.toMatchObject({ code: 3409 })
    await expect(updateUser('u-09', { roles: ['member'] })).rejects.toMatchObject({ code: 3409 })
    await expect(updateUser('u-04', { roles: ['analyst'] })).rejects.toMatchObject({ code: 3001, httpStatus: 422 })
    await expect(updateUser('u-04', { roles: [] })).rejects.toMatchObject({ code: 3001, httpStatus: 422 })
  }, 30_000)

  it('⑤ 契约边角：POST 405、单条详情/404 码统一、软删幂等', async () => {
    // ⑥ POST /admin/users 不存在（405）
    const postErr = await api.post('/admin/users', { emails: ['a@example.com'], role: 'member' }).catch((e: unknown) => e)
    expect(postErr).toBeInstanceOf(ApiError)
    expect((postErr as ApiError).httpStatus).toBe(405)

    // 单条详情（含 roles）+ 404 错误码统一（无 4041）
    const one = await getUser('u-02')
    expect(one.roles).toEqual(['curator', 'ontologist'])
    await expect(getUser('u-nope')).rejects.toMatchObject({ code: 404, httpStatus: 404 })
    await expect(updateUser('u-nope', { display_name: 'x' })).rejects.toMatchObject({ code: 404 })

    // DELETE 软删幂等：重复删除同响应 {id,status:'disabled'}
    const first = await disableUser('u-05')
    expect(first).toEqual({ id: 'u-05', status: 'disabled' })
    const repeat = await disableUser('u-05')
    expect(repeat).toEqual(first)
  }, 30_000)
})
