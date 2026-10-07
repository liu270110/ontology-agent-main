import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuthStore } from '@/stores/auth-store'
import { useSessionStream } from '@/sse/useSessionStream'
import type { SseEvent } from '@/sse/events'

/** useSessionStream 单测（对账 2026-09-27 §8.4 修复三行为，api/02 §4 重连语义）：
 *  ① 鉴权兜底：连接 URL 带 ?access_token=（api/01 §2.2，EventSource 无法自定义 header）；
 *  ② 事件驱动 seq 基线：收到 lastEventId=7 帧后常规断线，重连 URL 携带 last_event_id=7；
 *  ③ 跳号补发（F7 修法定稿）：handler 对 seq=9 返回 'gap' → 不前移续传基线：
 *     - 未提供 onGapBackfill：以最后已应用 seq 重连（服务端从缺口补发，store 去重兜底）；
 *     - 提供 onGapBackfill 且补齐成功：以该帧 seq=9 重连（缺口已被历史覆盖）；
 *     - onGapBackfill 返回 false：仍以最后已应用 seq 重连；
 *  ④ 无令牌时 URL 不含 access_token 参数。
 *  jsdom 无 EventSource：以 FakeEventSource 桩注入全局（台账 C-7 同款 shim）——构造时记录
 *  URL + 静态实例注册表，提供 emit（按具名事件分发一帧）/ fail（模拟 onerror）/ open 辅助
 *  与 close 断言。 */
class FakeEventSource {
  static instances: FakeEventSource[] = []
  static reset() {
    FakeEventSource.instances = []
  }

  url: string
  closed = false
  onopen: (() => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (ev: MessageEvent) => void) {
    const list = this.listeners.get(type) ?? []
    list.push(listener)
    this.listeners.set(type, list)
  }

  close() {
    this.closed = true
  }

  /** 测试辅助：推一帧具名事件（data 序列化为 JSON，lastEventId 即 SSE id: 行 = seq）。
   *  close 后不再投递（对齐真 EventSource：手动 close 后帧不再到达）。 */
  emit(type: string, data: Record<string, unknown>, lastEventId = '') {
    if (this.closed) return
    for (const fn of [...(this.listeners.get(type) ?? [])]) {
      fn(new MessageEvent(type, { data: JSON.stringify(data), lastEventId }))
    }
  }

  /** 测试辅助：模拟断线（触发 hook 的 onerror → 指数退避重连） */
  fail() {
    this.onerror?.(new Event('error'))
  }

  open() {
    this.onopen?.()
  }
}

beforeEach(() => {
  FakeEventSource.reset()
  localStorage.clear()
  useAuthStore.setState({
    status: 'anonymous',
    accessToken: null,
    refreshToken: null,
    expiresAt: null,
    user: null,
    mfaToken: null,
  })
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function renderStream(onEvent: (evt: SseEvent) => 'gap' | void = () => {}) {
  return renderHook(() => useSessionStream({ sessionId: 's-1', onEvent }))
}

describe('useSessionStream（对账 §8.4：鉴权兜底 + last_event_id 续传/跳号补发）', () => {
  it('① 连接 URL 带 access_token：连接时取 auth-store 当前令牌拼 ?access_token=', () => {
    vi.useFakeTimers()
    useAuthStore.setState({ accessToken: 'tok-1' })

    renderStream()

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0].url).toContain('access_token=tok-1')
  })

  it('② 事件驱动 seq 基线：lastEventId=7 帧后 onerror 断线，重连 URL 带 last_event_id=7', () => {
    vi.useFakeTimers()
    const onEvent = vi.fn()
    renderStream(onEvent)
    const first = FakeEventSource.instances[0]
    expect(first.url).not.toContain('last_event_id') // 基线为 0：不带续传参数

    act(() => {
      first.emit('TEXT_MESSAGE_START', { role: 'assistant' }, '7')
    })
    expect(onEvent).toHaveBeenCalledWith({ name: 'TEXT_MESSAGE_START', seq: 7, data: { role: 'assistant' } })

    act(() => {
      first.fail() // 常规断线：退避 1000*2^1=2000ms 后重连
    })
    expect(first.closed).toBe(true)
    act(() => {
      vi.advanceTimersByTime(2000)
    })

    expect(FakeEventSource.instances).toHaveLength(2)
    expect(FakeEventSource.instances[1].url).toContain('last_event_id=7')
  })

  it('③ 跳号不前移基线：handler 对 seq=9 返回 gap → close 旧连接，重连 URL 不带越缺口的 last_event_id', () => {
    vi.useFakeTimers()
    const onEvent: (evt: SseEvent) => 'gap' | void = evt => (evt.seq === 9 ? 'gap' : undefined)
    renderStream(onEvent)
    const first = FakeEventSource.instances[0]

    act(() => {
      first.emit('TEXT_MESSAGE_START', {}, '9')
    })
    // gap：主动断开（手动 close 后 EventSource 原生 lastEventId 已重置，不能依赖）
    expect(first.closed).toBe(true)

    act(() => {
      vi.advanceTimersByTime(50) // gap 重连延迟 50ms（无补齐回调的原生兜底路径）
    })

    expect(FakeEventSource.instances).toHaveLength(2)
    const second = FakeEventSource.instances[1]
    expect(second.closed).toBe(false)
    // F7（C-7）：基线未越缺口——不带 last_event_id（最后已应用 seq=0），服务端从缺口处补发
    expect(second.url).not.toContain('last_event_id=9')
    expect(second.url).not.toContain('last_event_id=0')
  })

  it('③-b F7 补齐成功：onGapBackfill 返回 true → 以该帧 seq=9 重连（缺口已被历史覆盖）', async () => {
    vi.useFakeTimers()
    const onEvent: (evt: SseEvent) => 'gap' | void = evt => (evt.seq === 9 ? 'gap' : undefined)
    const onGapBackfill = vi.fn(() => true)
    renderHook(() => useSessionStream({ sessionId: 's-1', onEvent, onGapBackfill }))
    const first = FakeEventSource.instances[0]

    act(() => {
      first.emit('TEXT_MESSAGE_START', {}, '9')
    })
    expect(first.closed).toBe(true)

    // 补齐 promise 微任务 + 0ms 重连定时器一并推进
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(onGapBackfill).toHaveBeenCalledWith({ name: 'TEXT_MESSAGE_START', seq: 9, data: {} })

    expect(FakeEventSource.instances).toHaveLength(2)
    expect(FakeEventSource.instances[1].url).toContain('last_event_id=9')
  })

  it('③-c F7 补齐失败：onGapBackfill 返回 false/异常 → 以最后已应用 seq 重连（服务端补发兜底）', async () => {
    vi.useFakeTimers()
    const onEvent: (evt: SseEvent) => 'gap' | void = evt => (evt.seq === 9 ? 'gap' : undefined)
    const onGapBackfill = vi.fn(() => Promise.reject(new Error('network down')))
    renderHook(() => useSessionStream({ sessionId: 's-1', onEvent, onGapBackfill }))
    const first = FakeEventSource.instances[0]

    act(() => {
      first.emit('TEXT_MESSAGE_START', {}, '9') // gap（基线保持 0）
      first.emit('RUN_FINISHED', {}, '10') // close 后帧不投递（shim 对齐真 EventSource）
    })
    expect(first.closed).toBe(true)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(FakeEventSource.instances).toHaveLength(2)
    expect(FakeEventSource.instances[1].url).not.toContain('last_event_id=9')
    expect(FakeEventSource.instances[1].url).not.toContain('last_event_id=10')
  })

  it('④ 无令牌时 URL 不含 access_token 参数', () => {
    vi.useFakeTimers()

    renderStream()

    expect(FakeEventSource.instances).toHaveLength(1)
    const url = FakeEventSource.instances[0].url
    expect(url).toBe('/api/v1/sessions/s-1/events')
    expect(url).not.toContain('access_token')
  })
})
