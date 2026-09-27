import { useEffect, useRef } from 'react'
import { useAuthStore } from '@/stores/auth-store'
import { KNOWN_EVENTS, type SseEvent, type SseEventName } from './events'

/** 会话 SSE 流（16 篇 §3.1）：具名事件监听（api/02 帧格式带 event: 行，onmessage 只收无名帧）
 *  + 断线指数退避重连。重连续传（api/02 §4）：
 *  - 常规断线：EventSource 原生携带 Last-Event-ID 头；
 *  - 跳号补发：store 对账返回 'gap' 时主动断开，以 ?last_event_id= 显式重连（api/02 §4
 *    登记的 query 兜底参数名——2026-09-27 对账 §8 裁决废弃未登记的 ?last_seq= 同义参数）；
 *  - 鉴权：EventSource 无法自定义 header，按 api/01 §2.2 以 ?access_token= 兜底（每次
 *    连接取 store 当前令牌，令牌刷新后经 onerror→重连自然换新）。seq 对账 dup/gap 由
 *    session-store.apply 承担（§3.2）。 */
export interface SessionStreamOptions {
  sessionId: string | null
  /** 返回 'gap' 表示跳号：hook 断开并以 ?last_event_id= 重连补发（api/02 §4） */
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

    function dispatch(name: SseEventName, e: MessageEvent) {
      let data: Record<string, unknown> = {}
      try {
        data = JSON.parse(e.data as string)
      } catch {
        return
      }
      const seq = Number(e.lastEventId || 0)
      if (seq > 0) lastSeqRef.current = seq // 续传基线：跳号补发时 ?last_event_id= 的取值来源
      const r = handler.current({ name, seq, data })
      if (r === 'gap') {
        // 跳号（api/02 §4）：主动断开并以最后已见 seq 显式重连——手动 close 后 EventSource
        // 原生 lastEventId 已重置，不能依赖
        es?.close()
        if (closed) return
        retry += 1
        timer = setTimeout(connect, 50)
      }
    }

    function connect() {
      if (closed) return
      stateCb.current?.(retry === 0 ? 'connecting' : 'reconnecting')
      const params = new URLSearchParams()
      const token = useAuthStore.getState().accessToken
      if (token) params.set('access_token', token) // api/01 §2.2：EventSource 无 header 兜底
      if (lastSeqRef.current > 0) params.set('last_event_id', String(lastSeqRef.current))
      const qs = params.toString()
      es = new EventSource(`/api/v1/sessions/${sessionId}/events${qs ? `?${qs}` : ''}`)
      es.onopen = () => {
        retry = 0
        stateCb.current?.('open')
      }
      for (const name of KNOWN_EVENTS) {
        es.addEventListener(name, e => dispatch(name, e as MessageEvent))
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
