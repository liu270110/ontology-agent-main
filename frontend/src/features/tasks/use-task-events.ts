import { useEffect, useState } from 'react'
import { ApiError, authHeaders, sseUrl } from '@/api/client'
import { useAuthStore } from '@/stores/auth-store'
import type { TaskEvent } from './api'

/** 任务事件流订阅（api/01 §5.2 GET /tasks/{id}/events，Accept: text/event-stream）：
 *  fetch 流式读 + SSE 帧解析（id/event/data，对齐 api/02 §2）；seq 对账去重后按序推进。
 *  用 fetch 而非 EventSource：jsdom 无 EventSource，vitest 下经 MSW 可测（S6 ⑥用例依赖）。
 *  取消用 cancelled 标志 + reader.cancel()，不向 fetch 传 AbortSignal——jsdom 的 AbortSignal
 *  与 Node(undici) fetch 不同类，传参会在 jsdom 测试环境直接抛错（S6 联调实录）。
 *  F4（联调 2026-10-04，B:A-10）：URL 经 client.sseUrl 拼 API_BASE（替代硬编码 /api/v1）；
 *  补 Authorization 头（原裸 fetch 无令牌，鉴权部署下时间线必空）；错误不再静默吞——
 *  HTTP 非 2xx / 网络失败置 error 返回，抽屉渲染错误行而非永远「等待事件推送…」。 */

export interface TaskEventStream {
  events: TaskEvent[]
  /** 流建立失败（401/404/5xx/网络）：非 null 时时间线不可用，消费方渲染错误态 */
  error: ApiError | Error | null
}

export function useTaskEvents(taskId: string | null): TaskEventStream {
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [error, setError] = useState<TaskEventStream['error']>(null)

  useEffect(() => {
    // ocr 整改（fe2 发现2）：重置提到 if (!taskId) 之前——taskId→taskId 切换（抽屉复用
    // 同一 hook 实例）时 effect 重跑会先清空旧任务的 ApiError 与事件，不再泄漏到新任务时间线。
    setEvents([])
    setError(null)
    if (!taskId) return
    let cancelled = false
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

    const merge = (ev: TaskEvent) =>
      setEvents(prev => (prev.some(x => x.seq === ev.seq) ? prev : [...prev, ev].sort((a, b) => a.seq - b.seq)))

    async function run() {
      try {
        const res = await fetch(sseUrl(`/tasks/${taskId}/events`), {
          headers: {
            Accept: 'text/event-stream',
            ...authHeaders(useAuthStore.getState().accessToken),
          },
        })
        if (cancelled) return
        if (!res.ok) {
          // 错误信封优先取登记文案；非 JSON（裸 404 等）回落 HTTP 状态（F8①同口径：不编造业务码）
          const body = (await res.json().catch(() => null)) as { code?: number; message?: string } | null
          // ocr 整改（fe2 发现3）：json 解析（可能含响应体延迟）后复查 cancelled——
          // 卸载/切换后旧 effect 不写脏状态（对齐下方 catch 分支既有守卫）
          if (cancelled) return
          setError(new ApiError(body?.code ?? -1, body?.message ?? `HTTP ${res.status}`, res.status))
          return
        }
        reader = res.body?.getReader() ?? null
        if (!reader) return
        const decoder = new TextDecoder()
        let buf = ''
        for (;;) {
          const { done, value } = await reader.read()
          if (done || cancelled) break
          buf += decoder.decode(value, { stream: true })
          const frames = buf.split('\n\n')
          buf = frames.pop() ?? ''
          for (const frameText of frames) {
            const dataLine = frameText.split('\n').find(l => l.startsWith('data: '))
            if (!dataLine) continue
            try {
              merge(JSON.parse(dataLine.slice(6)) as TaskEvent)
            } catch { /* 半帧容错 */ }
          }
        }
      } catch (e) {
        // 网络错误：非取消场景不再静默——置错误态（取消=组件卸载/换任务，保持静默）
        if (!cancelled) setError(e instanceof Error ? e : new Error(String(e)))
      }
    }
    void run()
    return () => {
      cancelled = true
      void reader?.cancel().catch(() => {})
    }
  }, [taskId])

  return { events, error }
}
