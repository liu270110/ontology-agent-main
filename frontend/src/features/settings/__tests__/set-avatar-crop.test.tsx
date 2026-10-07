import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

// MSW 启停由全局 setupFiles（src/mocks/node-setup.ts）负责，用例不再重复 listen/close。
beforeEach(() => {
  // jsdom 无图片加载器：FakeImage 赋 src 后微任务触发 onload（naturalWidth/Height 供 cover 计算）
  vi.stubGlobal('Image', class FakeImage {
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    naturalWidth = 320
    naturalHeight = 240
    private _src = ''
    get src() { return this._src }
    set src(v: string) {
      this._src = v
      queueMicrotask(() => this.onload?.())
    }
  })
  toDataUrlSpy = vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue(STUB_AVATAR)
  // jsdom 无 2D 实现：拦下 getContext 返回 null（免 not-implemented 噪音；组件侧空实现兜底）
  getContextSpy = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  toDataUrlSpy.mockRestore()
  getContextSpy.mockRestore()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

// jsdom 无 canvas 实现：toDataURL 桩为固定 dataURL（断言「确认回调收到 dataURL」层面）
const STUB_AVATAR = 'data:image/png;base64,STUBAVATAR'
let toDataUrlSpy: MockInstance
let getContextSpy: MockInstance

/** B5-T 设置域 polish · 头像圆形裁剪（IX-SET-01）：选文件 → 裁剪态（canvas 圆形遮罩+缩放）
 *  → 确认 → dataURL 上屏并落 localStorage `oa-avatar-<email>`；非图片/超 5MB toast 拒绝。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
  await screen.findByTestId('set-profile-name', {}, { timeout: 10_000 })
}

describe('设置 · 资料 Tab · 头像裁剪', () => {
  it('选图片文件 → 裁剪态出现 → 确认 → 头像 img src 更新并持久化', async () => {
    await loginAndGo('/settings')

    const png = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'a.png', { type: 'image/png' })
    fireEvent.change(screen.getByLabelText('上传头像'), { target: { files: [png] } })

    // 裁剪态：Modal（圆形 canvas + 缩放滑杆）出现
    expect(await screen.findByRole('dialog', { name: '裁剪头像' })).toBeInTheDocument()
    expect(screen.getByTestId('set-avatar-canvas')).toBeInTheDocument()
    const zoom = screen.getByTestId('set-avatar-zoom') as HTMLInputElement
    expect(zoom.min).toBe('0.5')
    expect(zoom.max).toBe('2')

    fireEvent.click(screen.getByTestId('set-avatar-confirm'))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '裁剪头像' })).not.toBeInTheDocument())

    // 既有展示路径：头像 img src 变为裁剪产物 dataURL
    const img = screen.getByAltText('头像预览')
    expect(img).toHaveAttribute('src', STUB_AVATAR)
    // localStorage 持久化（key = oa-avatar-<auth-store 邮箱>）
    expect(localStorage.getItem('oa-avatar-admin@example.com')).toBe(STUB_AVATAR)
  }, 30_000)

  it('超 5MB 文件 → toast 拒绝且不进裁剪态', async () => {
    await loginAndGo('/settings')

    const big = new File([new ArrayBuffer(5 * 1024 * 1024 + 1)], 'big.png', { type: 'image/png' })
    fireEvent.change(screen.getByLabelText('上传头像'), { target: { files: [big] } })

    expect(await screen.findByText('图片不能超过 5MB，请压缩后重试')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '裁剪头像' })).not.toBeInTheDocument()
  }, 30_000)

  it('非图片文件 → toast 拒绝且不进裁剪态', async () => {
    await loginAndGo('/settings')

    const txt = new File(['hello'], 'note.txt', { type: 'text/plain' })
    fireEvent.change(screen.getByLabelText('上传头像'), { target: { files: [txt] } })

    expect(await screen.findByText('仅支持图片文件（JPG/PNG）')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '裁剪头像' })).not.toBeInTheDocument()
  }, 30_000)
})
