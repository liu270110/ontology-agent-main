import { useEffect, useRef } from 'react'
import { useAuthStore } from '@/stores/auth-store'
import { sseUrl } from '@/api/client'
import { KNOWN_EVENTS, type SseEvent, type SseEventName } from './events'

/** 会话 SSE 流（16 篇 §3.1）：具名事件监听（api/02 帧格式带 event: 行，onmessage 只收无名帧）
 *  + 断线指数退避重连。重连续传（api/02 §4）：
 *  - 常规断线：EventSource 原生携带 Last-Event-ID 头；
 *  - 跳号补发：store 对账返回 'gap' 时主动断开，以 ?last_event_id= 显式重连（api/02 §4
 *    登记的 query 兜底参数名——2026-09-27 对账 §8 裁决废弃未登记的 ?last_seq= 同义参数）；
 *  - 鉴权：EventSource 无法自定义 header，按 api/01 §2.2 以 ?access_token= 兜底（每次
 *    连接取 store 当前令牌，令牌刷新后经 onerror→重连自然换新）。seq 对账 dup/gap 由
 *    session-store.apply 承担（§3.2）。
 *  F4（联调 2026-10-04）：URL 一律经 client.sseUrl 拼接（API_BASE 单源，替代硬编码 /api/v1）。 */
export interface SessionStreamOptions {
  sessionId: string | null
  /** 返回 'gap' 表示跳号：hook 断开并以 ?last_event_id= 重连补发（api/02 §4） */
  onEvent: (evt: SseEvent) => 'gap' | void
  onStateChange?: (state: 'connecting' | 'open' | 'reconnecting' | 'offline') => void
  /** F7（C-7）：跳号历史补齐——gap 时在重连前调用一次；resolve=true 表示该 seq 已被历史
   *  补齐覆盖（hook 以该帧 seq 为续传基线重连），false/异常=补齐失败（hook 以最后已应用
   *  seq 重连，由服务端从缺口处补发、store 对账去重兜底）。 */
  onGapBackfill?: (evt: SseEvent) => Promise<boolean> | boolean
}

export function useSessionStream({ sessionId, onEvent, onStateChange, onGapBackfill }: SessionStreamOptions) {
  const handler = useRef(onEvent)
  const stateCb = useRef(onStateChange)
  const gapCb = useRef(onGapBackfill)
  const lastSeqRef = useRef(0)
  handler.current = onEvent
  stateCb.current = onStateChange
  gapCb.current = onGapBackfill

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
      const r = handler.current({ name, seq, data })
      if (r === 'gap') {
        // F7（C-7）：跳号不前移 lastSeqRef（原实现在此提前自增——重连基线越过缺口，
        // 缺口帧永久丢失且已入 baseline 的文本缺尾）。改为：先历史补齐（onGapBackfill），
        // 成功 → 以该帧 seq 为续传基线重连；失败/未提供 → 以最后已应用 seq 重连，
        // 服务端从缺口处补发、store 按 seq 对账去重（api/02 §4 重连语义不变）。
        es?.close()
        if (closed) return
        stateCb.current?.('reconnecting')
        const pending = { name, seq, data }
        const cb = gapCb.current
        if (cb) {
          void Promise.resolve()
            .then(() => cb(pending))
            .then(covered => {
              if (covered) lastSeqRef.current = Math.max(lastSeqRef.current, seq)
            })
            .catch(() => {})
            .finally(() => {
              if (closed) return
              timer = setTimeout(connect, 0)
            })
        } else {
          timer = setTimeout(connect, 50)
        }
        return
      }
      // 非 gap（applied/dup）才前移续传基线，且只进不退（防迟到 dup 帧回拨基线）
      if (seq > 0) lastSeqRef.current = Math.max(lastSeqRef.current, seq)
    }

    function connect() {
      if (closed) return
      stateCb.current?.(retry === 0 ? 'connecting' : 'reconnecting')
      es = new EventSource(
        sseUrl(`/sessions/${sessionId}/events`, {
          accessToken: useAuthStore.getState().accessToken, // api/01 §2.2：EventSource 无 header 兜底
          lastEventId: lastSeqRef.current,
        }),
      )
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
