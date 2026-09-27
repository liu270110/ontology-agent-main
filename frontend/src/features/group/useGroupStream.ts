import { useEffect, useRef } from 'react'

/** 群聊 SSE 流：useSessionStream（src/sse/）同款模式轻量复制——具名事件监听 + 断线指数退避
 *  重连 + ?last_seq= 补发。之所以不复用 useSessionStream 本体：其监听集合 = KNOWN_EVENTS
 *  静态清单（api/02），未含群聊扩展事件 ROUTING_DECISION（X15），且 src/sse/ 属 S7 禁改区；
 *  归约语义完全对齐（seq 对账 dup/gap 由 group-store.apply 承担）。 */
export interface GroupStreamOptions {
  sessionId: string | null
  onEvent: (name: string, seq: number, data: Record<string, unknown>) => 'gap' | void
  onStateChange?: (state: 'connecting' | 'open' | 'reconnecting' | 'offline') => void
}

/** 监听集合 = 主干波 11 事件 + 快照 + 群聊扩展（X15） */
const GROUP_EVENTS = [
  'RUN_STARTED', 'TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TEXT_MESSAGE_END',
  'TOOL_CALL_START', 'TOOL_CALL_ARGS', 'TOOL_CALL_END', 'TOOL_CALL_RESULT',
  'RETRIEVAL_EVIDENCE', 'RUN_FINISHED', 'RUN_ERROR',
  'MESSAGES_SNAPSHOT', 'STATE_SNAPSHOT', 'STATE_DELTA',
  'ROUTING_DECISION',
]

export function useGroupStream({ sessionId, onEvent, onStateChange }: GroupStreamOptions) {
  const handler = useRef(onEvent)
  const stateCb = useRef(onStateChange)
  handler.current = onEvent
  stateCb.current = onStateChange

  useEffect(() => {
    if (!sessionId) return
    if (typeof EventSource === 'undefined') {
      // jsdom/vitest 环境无 EventSource（同 tasks 域注记）：跳过直播，历史基线仍可渲染
      stateCb.current?.('offline')
      return
    }
    let es: EventSource | null = null
    let retry = 0
    let closed = false
    let lastSeq = 0
    let timer: ReturnType<typeof setTimeout> | undefined

    function dispatch(name: string, e: MessageEvent) {
      let data: Record<string, unknown> = {}
      try {
        data = JSON.parse(e.data as string)
      } catch {
        return
      }
      // 与 useSessionStream 同款：hook 不维护 seq 基线（重连自 0 重放，store 按 seq 对账
      // dedup / 补缺），跳号由 group-store.apply 返回 'gap' 触发即时重连
      const r = handler.current(name, Number(e.lastEventId || 0), data)
      if (r === 'gap') {
        // 跳号（api/02 §3.2 同款）：断开重连，服务端重放缓冲帧，store 对账去重补缺
        es?.close()
        if (closed) return
        timer = setTimeout(connect, 50)
      }
    }

    function connect() {
      if (closed) return
      stateCb.current?.(retry === 0 ? 'connecting' : 'reconnecting')
      const since = lastSeq
      const url = `/api/v1/sessions/${sessionId}/events${since ? `?last_seq=${since}` : ''}`
      es = new EventSource(url)
      es.onopen = () => {
        retry = 0
        stateCb.current?.('open')
      }
      for (const name of GROUP_EVENTS) {
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
