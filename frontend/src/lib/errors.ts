import { ApiError } from '@/api/client'

/** 错误码 → 用户文案（api/01 §4.3 全集的子集映射；未登记码回落 message）。
 *  1002 凭据类失败统一「邮箱或密码错误」（28 篇 §2 红线：不暴露「账号不存在 vs 密码错误」，防枚举）；
 *  限速 1005/2005/429 按 Retry-After 倒计时提示（16 篇 §7；后端登记码为 2005，前端归并同语义）。 */
const MESSAGES: Record<number, string> = {
  1001: '未认证或会话已过期，请重新登录',
  1002: '邮箱或密码错误',
  1003: '登录状态已过期，请重新登录',
  1005: '操作过于频繁，请稍后再试',
  2001: '权限不足',
  2002: '权限不足',
  2005: '操作过于频繁，请稍后再试',
  3001: '参数校验失败',
  4041: '资源不存在',
  5001: '上游模型服务异常',
}

function rateLimitText(retryAfter: number | undefined): string {
  const s = Number(retryAfter ?? 0)
  if (!Number.isFinite(s) || s <= 0) return '操作过于频繁，请稍后再试'
  if (s >= 60) return `操作过于频繁，请约 ${Math.ceil(s / 60)} 分钟后再试`
  return `操作过于频繁，请 ${s} 秒后再试`
}

export function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    // 限速族（含 Retry-After 时给出剩余时间）
    if (e.code === 1005 || e.code === 2005 || e.httpStatus === 429) return rateLimitText(e.retryAfter)
    return MESSAGES[e.code] ?? e.message
  }
  if (e instanceof Error) return e.message
  return '未知错误'
}
