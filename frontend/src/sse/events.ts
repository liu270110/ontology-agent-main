/** SSE 事件清单（api/02 §3 事件总表）。M3 主干波 11 事件 + M4+ 已知扩展；
 *  未知事件一律忽略（向前兼容裁决）。 */
export const KNOWN_EVENTS = [
  // M3 主干波
  'RUN_STARTED', 'TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TEXT_MESSAGE_END',
  'TOOL_CALL_START', 'TOOL_CALL_ARGS', 'TOOL_CALL_END', 'TOOL_CALL_RESULT',
  'RETRIEVAL_EVIDENCE', 'RUN_FINISHED', 'RUN_ERROR',
  // M4+ 扩展波（前端已实现归约的部分）
  'MESSAGES_SNAPSHOT', 'STATE_SNAPSHOT', 'STATE_DELTA',
] as const

export type SseEventName = (typeof KNOWN_EVENTS)[number] | (string & {})

export interface SseEvent {
  name: SseEventName
  /** seq = SSE `id:` 行（api/02 §2：会话内单调递增，即 task_events.seq） */
  seq: number
  data: Record<string, unknown>
}
