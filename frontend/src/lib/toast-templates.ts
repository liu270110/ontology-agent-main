import { ApiError } from '@/api/client'

/** Toast 文案模板单源（36 §B1/B2；board 设计图 §B2 三态样例同源）：
 *  sonner 语义（title/description/action/duration），场景文案只在此定义，
 *  消费方禁止散写字符串（33 §6 一致性）。注意与 lib/errors.ts 的 describeError
 *  （错误码→业务文案映射，auth/dashboard/kb 消费）职责不同：本模块面向 toast
 *  一句话反馈，ApiError 统一透出「消息（错误码 N）」便于可追溯（设计宪法 #5）。 */

/** 网络层异常识别：fetch 失败多为 TypeError（Failed to fetch），再兜常见网络措辞 */
function isNetworkError(e: unknown): boolean {
  if (e instanceof TypeError) return true
  const msg = e instanceof Error ? e.message : ''
  return /failed to fetch|network ?error|networkerror|load failed|fetch failed/i.test(msg)
}

/** 错误 → 用户可读一句话（36 §B1：ApiError 带错误码；网络层给行动指引；
 *  禁止透出 stack/原始 JSON——33 §7）。 */
export function describeError(e: unknown): string {
  if (isNetworkError(e)) return '网络连接不可用，请检查网络后重试'
  if (e instanceof ApiError) return `${e.message}（错误码 ${e.code}）`
  if (e instanceof Error) return e.message
  return '未知错误'
}

/** —— 发送失败（36 §B1：error + 重试 action，duration 6s；草稿由 MessageInput 保留不清）—— */
export const SEND_FAILED = {
  title: '发送失败',
  retryLabel: '重试',
  durationMs: 6000,
} as const

export const sendFailedDescription = (e: unknown): string => `${describeError(e)}。草稿已保留。`

/** —— 导出 Markdown（36 §B1：toast.promise 三态，loading 带会话标题）—— */
export const EXPORT_MD = {
  loading: (title: string) => `正在导出「${title}」…`,
  success: '已导出 Markdown',
} as const

/** —— 停止生成（36 §B1：IX-CHT-06 反馈闭环补齐）—— */
export const STOP_GENERATED = {
  title: '已停止生成',
  description: '已生成的部分已保留',
} as const

/** —— 消息基线失败（36 §B1 错误态文案/警示条/恢复提示；§B2 重试成功 toast）—— */
export const BASELINE = {
  errorTitle: '消息加载失败',
  errorDesc: '历史消息没有取回来——它们还在服务端，不会丢失。可重试，或先继续发送新消息。',
  reloadLabel: '重新加载',
  continueLabel: '仍要继续对话',
  banner: '历史消息未能加载 · 显示为空 ≠ 没有历史',
  reloaded: '历史消息已加载',
} as const
