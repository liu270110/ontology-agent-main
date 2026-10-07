/** SSE 连接态 → 展示文案（单聊 ChatPage / 群聊 GroupChatPage 共用单事实源）。
 *  口径：offline=已断开（此前两页各自持表已分叉：离线 vs 已断开）。
 *  useSessionStream（src/sse，S7 禁改区）与 useGroupStream（S7）的 onStateChange 枚举同构。 */
export type ConnState = 'connecting' | 'open' | 'reconnecting' | 'offline'

export const CONN_STATE_TEXT: Record<ConnState, string> = {
  connecting: '连接中',
  open: '已连接',
  reconnecting: '重连中',
  offline: '已断开',
}
