import { useEffect, useState } from 'react'
import { authHeaders, sseUrl } from '@/api/client'
import { useAuthStore } from '@/stores/auth-store'
import type { TrajFrame } from './lib/timeline'

/** 轨迹回放段拉取（api/01 §5.2 GET /sessions/{id}/events，Accept: text/event-stream）：
 *  读 use-task-events 的 fetch 流解析模式（jsdom 无 EventSource，vitest 经 MSW 可测）——
 *  SSE 帧（id/event/data）→ {seq,name,data}[]，seq 对账去重升序。回放页只读不推进订阅，
 *  断流/卸载即静默终止（已收到的帧保留）；mock 侧经 framesBySession 缓冲原样回放
 *  （08 篇：回放、审计、恢复共享同一事件流）。
 *  F4（联调 2026-10-04）：URL 经 client.sseUrl 拼 API_BASE（替代硬编码 /api/v1），
 *  补 Authorization 头（对齐 client 同源鉴权模式，原裸 fetch 无令牌在鉴权部署下必 401）。 */
export function useTrajectoryFrames(sessionId: string | null): TrajFrame[] {
  const [frames, setFrames] = useState<TrajFrame[]>([])

  useEffect(() => {
    if (!sessionId) {
      setFrames([])
      return
    }
    let cancelled = false
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

    const merge = (f: TrajFrame) =>
      setFrames(prev => (prev.some(x => x.seq === f.seq) ? prev : [...prev, f].sort((a, b) => a.seq - b.seq)))

    async function run() {
      try {
        const res = await fetch(sseUrl(`/sessions/${sessionId}/events`), {
          headers: {
            Accept: 'text/event-stream',
            ...authHeaders(useAuthStore.getState().accessToken),
          },
        })
        reader = res.body?.getReader() ?? null
        if (!reader) return
        const decoder = new TextDecoder()
        let buf = ''
        for (;;) {
          const { done, value } = await reader.read()
          if (done || cancelled) break
          buf += decoder.decode(value, { stream: true })
          const chunks = buf.split('\n\n')
          buf = chunks.pop() ?? ''
          for (const chunk of chunks) {
            const lines = chunk.split('\n')
            const idLine = lines.find(l => l.startsWith('id: '))
            const nameLine = lines.find(l => l.startsWith('event: '))
            const dataLine = lines.find(l => l.startsWith('data: '))
            if (!idLine || !dataLine) continue // 心跳注释帧（: ping）等无载荷帧
            try {
              merge({
                seq: Number(idLine.slice(4)),
                name: nameLine ? nameLine.slice(7) : 'message',
                data: JSON.parse(dataLine.slice(6)) as Record<string, unknown>,
              })
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
  }, [sessionId])

  return frames
}
