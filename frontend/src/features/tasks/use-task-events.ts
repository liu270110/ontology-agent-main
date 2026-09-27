import { useEffect, useState } from 'react'
import type { TaskEvent } from './api'

/** 任务事件流订阅（api/01 §5.2 GET /tasks/{id}/events，Accept: text/event-stream）：
 *  fetch 流式读 + SSE 帧解析（id/event/data，对齐 api/02 §2）；seq 对账去重后按序推进。
 *  用 fetch 而非 EventSource：jsdom 无 EventSource，vitest 下经 MSW 可测（S6 ⑥用例依赖）。
 *  取消用 cancelled 标志 + reader.cancel()，不向 fetch 传 AbortSignal——jsdom 的 AbortSignal
 *  与 Node(undici) fetch 不同类，传参会在 jsdom 测试环境直接抛错（S6 联调实录）。 */
export function useTaskEvents(taskId: string | null): TaskEvent[] {
  const [events, setEvents] = useState<TaskEvent[]>([])

  useEffect(() => {
    if (!taskId) {
      setEvents([])
      return
    }
    let cancelled = false
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

    const merge = (ev: TaskEvent) =>
      setEvents(prev => (prev.some(x => x.seq === ev.seq) ? prev : [...prev, ev].sort((a, b) => a.seq - b.seq)))

    async function run() {
      try {
        const res = await fetch(`/api/v1/tasks/${taskId}/events`, {
          headers: { Accept: 'text/event-stream' },
        })
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
      } catch {
        /* 网络错误 / 取消：组件卸载或断流，静默 */
      }
    }
    void run()
    return () => {
      cancelled = true
      void reader?.cancel().catch(() => {})
    }
  }, [taskId])

  return events
}
