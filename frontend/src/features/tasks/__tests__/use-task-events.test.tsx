import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { useTaskEvents } from '../use-task-events'

/** useTaskEvents 单测（ocr fe2 整改 发现2/发现3）：
 *  发现2——effect 重置前置：taskId→taskId 切换（抽屉复用同一 hook 实例）时旧任务的
 *  ApiError 与事件不再泄漏到新任务时间线（rerender 切 id 断言 error 即时清空）；
 *  发现3——json 解析后复查 cancelled：卸载/切换发生在 res.json() 等待窗口内时，
 *  旧 effect 不写脏错误。该窗口 MSW 无法切片 header/body 时序（delay 作用于整个响应），
 *  故 发现3 用例以 vi.stubGlobal 桩 fetch（json 挂起可控），其余用例走 MSW 契约仿真。 */

afterEach(() => {
  cleanup()
  server.resetHandlers()
  vi.unstubAllGlobals()
  localStorage.clear()
  useAuthStore.setState({
    status: 'anonymous',
    accessToken: null,
    refreshToken: null,
    expiresAt: null,
    user: null,
    mfaToken: null,
  })
})

describe('useTaskEvents（ocr fe2 发现2/3：切任务不泄漏旧错误/事件）', () => {
  it('发现2：taskId→taskId 切换即清空旧 error/events，新任务错误照常到达', async () => {
    server.use(
      http.get('*/api/v1/tasks/job-a/events', () =>
        HttpResponse.json({ code: 5004, message: 'boom-a' }, { status: 500 }),
      ),
      http.get('*/api/v1/tasks/job-b/events', () =>
        HttpResponse.json({ code: 5004, message: 'boom-b' }, { status: 500 }),
      ),
    )

    const { result, rerender } = renderHook(
      ({ id }) => useTaskEvents(id),
      { initialProps: { id: 'job-a' as string | null } },
    )

    // 旧任务错误先真实落地（确认「有旧错误可泄漏」前提）
    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.error?.message).toBe('boom-a')

    // 切任务（抽屉复用实例）：同步 effect 重跑先重置——旧错误/事件即时清空
    rerender({ id: 'job-b' })
    expect(result.current.error).toBeNull()
    expect(result.current.events).toEqual([])

    // 新任务自己的错误照常到达（重置未打断新订阅）
    await waitFor(() => expect(result.current.error?.message).toBe('boom-b'))
  })

  it('发现2 续：切到 null（关抽屉）同样清空旧错误', async () => {
    server.use(
      http.get('*/api/v1/tasks/job-a/events', () =>
        HttpResponse.json({ code: 5004, message: 'boom-a' }, { status: 500 }),
      ),
    )

    const { result, rerender } = renderHook(
      ({ id }) => useTaskEvents(id),
      { initialProps: { id: 'job-a' as string | null } },
    )
    await waitFor(() => expect(result.current.error).not.toBeNull())

    rerender({ id: null })
    expect(result.current.error).toBeNull()
    expect(result.current.events).toEqual([])
  })

  it('发现3：res.json() 等待窗口内切换 → 旧 effect 不写脏错误（cancelled 复查）', async () => {
    // 桩 fetch：headers 立即返回（!ok 成立）；第 1 次（job-a）json 永挂起直到测试放行——
    // 精确落在「await res.json() 之后、setError 之前」这一被修的窗口；第 2 次（job-b）
    // json 用独立永不 resolve 的 promise，避免放行时误灌新 effect
    let release!: (v: unknown) => void
    const slowJson = new Promise(resolve => {
      release = resolve
    })
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        call += 1
        return { ok: false, status: 500, json: () => (call === 1 ? slowJson : new Promise<never>(() => {})) }
      }),
    )

    const { result, rerender } = renderHook(
      ({ id }) => useTaskEvents(id),
      { initialProps: { id: 'job-a' as string | null } },
    )
    // 放行微任务队列：run() 越过 fetch await（cancelled 仍 false）挂入 res.json() 等待
    await act(async () => {
      await new Promise(r => setTimeout(r, 0))
    })

    rerender({ id: 'job-b' }) // 旧 effect cancelled=true（job-b 的 fetch 同被桩，无关本断言）
    release({ code: 5004, message: 'stale-a' }) // 旧任务错误体此刻才到
    await act(async () => {
      await new Promise(r => setTimeout(r, 0))
    })

    // 未复查 cancelled 时这里会被覆写为 ApiError(5004,'stale-a')
    expect(result.current.error).toBeNull()
    expect(result.current.events).toEqual([])
  })
})

describe('useTaskEvents 断流透出（fe-s1 P-009：streamClosed hook 层透出）', () => {
  it('服务端关流（reader done）→ events 照常入列 + streamClosed=true（宿主可显重连提示）', async () => {
    const encoder = new TextEncoder()
    server.use(
      http.get('*/api/v1/tasks/job-stream/events', () =>
        new HttpResponse(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode('data: {"seq":1,"type":"log","label":"开始","at":"2026-10-07T00:00:00Z","step":1}\n\n'),
              )
              controller.close() // 推一帧后服务端关流（代理切断/后端重启同形态）
            },
          }),
          { headers: { 'Content-Type': 'text/event-stream' } },
        ),
      ),
    )

    const { result } = renderHook(() => useTaskEvents('job-stream'))
    await waitFor(() => expect(result.current.streamClosed).toBe(true), { timeout: 5_000 })
    expect(result.current.events.map(e => e.seq)).toEqual([1])
    // 断流 ≠ 建流失败：error 通道保持空，语义不混淆
    expect(result.current.error).toBeNull()
  })

  it('建流失败（404）只走 error 通道，streamClosed 不置位', async () => {
    server.use(
      http.get('*/api/v1/tasks/job-miss/events', () =>
        HttpResponse.json({ code: 5004, message: 'no such task' }, { status: 404 }),
      ),
    )
    const { result } = renderHook(() => useTaskEvents('job-miss'))
    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.streamClosed).toBe(false)
  })
})
