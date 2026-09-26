import { useEffect, useRef } from 'react'
import { KNOWN_EVENTS, type SseEvent, type SseEventName } from './events'

/** 会话 SSE 流（16 篇 §3.1）：具名事件监听（api/02 帧格式带 event: 行，onmessage 只收无名帧）
 *  + 断线指数退避重连；seq 对账与补发由 session-store.apply / 调用方负责（§3.2）。
 *  重连时 EventSource 原生携带 Last-Event-ID 头，网关据此补发。 */
export interface SessionStreamOptions {
  sessionId: string | null
  /** 返回 'gap' 表示跳号：hook 将断开并以 ?last_seq= 重连补发（api/02 §3.2；TODO: last_seq 参数待登记 api/02） */
  onEvent: (evt: SseEvent) => 'gap' | void
  onStateChange?: (state: 'connecting' | 'open' | 'reconnecting' | 'offline') => void
}

export function useSessionStream({ sessionId, onEvent, onStateChange }: SessionStreamOptions) {
  const handler = useRef(onEvent)
  const stateCb = useRef(onStateChange)
  const lastSeqRef = useRef(0)
  handler.current = onEvent
  stateCb.current = onStateChange

  useEffect(() => {
    if (!sessionId) return
    lastSeqRef.current = 0
    let es: EventSource | null = null
    let retry = 0
    let closed = false
    let timer: ReturnType<typeof setTimeout> | undefined

    function dispatch(name: SseEventName, e: MessageEvent, lastSeq: number) {
      let data: Record<string, unknown> = {}
      try {
        data = JSON.parse(e.data as string)
      } catch {
        return
      }
      const r = handler.current({ name, seq: Number(e.lastEventId || 0), data })
      if (r === 'gap') {
        // 跳号（§3.2）：断开重连，服务端按 last_seq 补发缺口帧（EventSource 原生 lastEventId 已越位，不能依赖）
        es?.close()
        if (closed) return
        retry += 1
        timer = setTimeout(connect, 50)
        void lastSeq
      }
    }

    function connect() {
      if (closed) return
      stateCb.current?.(retry === 0 ? 'connecting' : 'reconnecting')
      const since = lastSeqRef.current
      const url = `/api/v1/sessions/${sessionId}/events${since ? `?last_seq=${since}` : ''}`
      es = new EventSource(url)
      es.onopen = () => {
        retry = 0
        stateCb.current?.('open')
      }
      for (const name of KNOWN_EVENTS) {
        es.addEventListener(name, e => dispatch(name, e as MessageEvent, since))
      }
      es.onerror = () => {
        es?.close()
        if (closed) return
        retry += 1
        stateCb.current?.(retry <= 3 ? 'reconnecting' : 'offline')
        timer = setTimeout(connect, Math.min(1000 * 2 ** retry, 15_000))
      }
    }
    connect()
    return () => {
      closed = true
      if (timer) clearTimeout(timer)
      es?.close()
    }
  }, [sessionId])
}
