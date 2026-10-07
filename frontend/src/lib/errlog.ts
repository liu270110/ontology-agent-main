/** oa-errlog 本地错误日志（P-005 前端兜底，台账-生产化-2026-10-07）：
 *  环形留存最近 5 条（含堆栈与路径），供 ErrorBoundary「查看技术详情」折叠回看与偶发
 *  崩溃事后定位——契合 06 篇「先定位根因」：崩溃类 bug 的堆栈不能只留在案发 console 里。
 *  仅本地 localStorage 留存不上送（远程上报归 S3 批，接入监控时在此取 entry 带 trace_id）。
 *  ErrorBoundary（渲染崩溃）与 installGlobalErrorCatch（运行时错误/未处理拒绝）共用写入，
 *  避免多通道重复记账。 */

const ERRLOG_KEY = 'oa-errlog'
const ERRLOG_MAX = 5
/** 同 message 去重窗口：1s 内的重复错误合并为一条。渲染崩溃在 dev 下会同时触发
 *  ErrorBoundary.componentDidCatch 与 window error 双通道，合并防双记。 */
const DEDUP_WINDOW_MS = 1000

export interface ErrLogEntry {
  at: string
  path: string
  message: string
  stack?: string
  componentStack?: string
}

/** 去重状态（模块级）：记录最近一次「被接受」写入的 message 与时刻 */
let lastMessage = ''
let lastAt = 0

/** 读环形日志（损坏/不可用回落空数组，不抛） */
export function readErrLog(): ErrLogEntry[] {
  try {
    const raw = localStorage.getItem(ERRLOG_KEY)
    return raw ? (JSON.parse(raw) as ErrLogEntry[]) : []
  } catch {
    return []
  }
}

/** 追加一条错误（环形截断 5 条）；同 message 在 1s 窗口内的重复写入静默合并。
 *  取证失败不影响调用方（崩溃兜底优先于取证）。 */
export function appendErrLog(entry: ErrLogEntry): void {
  try {
    const now = Date.now()
    if (entry.message === lastMessage && now - lastAt < DEDUP_WINDOW_MS) return
    lastMessage = entry.message
    lastAt = now
    const log = readErrLog()
    log.unshift(entry)
    localStorage.setItem(ERRLOG_KEY, JSON.stringify(log.slice(0, ERRLOG_MAX)))
  } catch {
    /* 取证失败不影响调用方 */
  }
}

/** 全局兜底收集（P-005 S1 本地兜底）：window error + unhandledrejection → 同一环形缓冲。
 *  - capture 挂载：资源类 error（script/img 加载失败）不冒泡，capture 才收得到——但按
 *    target 过滤静默跳过（静态资源失败是白噪声，非 JS 运行时错误）；
 *  - unhandledrejection 的 reason 可能是任意值（Error/字符串/undefined），统一 String 化。
 *  返回解绑函数（测试清场用）；幂等性由调用方保证（main.tsx 只装一次）。 */
export function installGlobalErrorCatch(): () => void {
  const onError = (e: ErrorEvent) => {
    // 资源类错误过滤：target 为元素（script/img/link…）即加载失败，跳过
    if (e.target instanceof Element) return
    appendErrLog({
      at: new Date().toISOString(),
      path: window.location.pathname,
      message: String(e.error?.message ?? e.message ?? e.error ?? '未知错误'),
      stack: String(e.error?.stack ?? '').slice(0, 2000),
    })
  }
  const onRejection = (e: PromiseRejectionEvent) => {
    const reason: unknown = e.reason
    appendErrLog({
      at: new Date().toISOString(),
      path: window.location.pathname,
      message: String((reason as { message?: unknown })?.message ?? reason ?? '未处理的 Promise 拒绝'),
      stack: String((reason as { stack?: unknown })?.stack ?? '').slice(0, 2000),
    })
  }
  window.addEventListener('error', onError, true)
  window.addEventListener('unhandledrejection', onRejection)
  return () => {
    window.removeEventListener('error', onError, true)
    window.removeEventListener('unhandledrejection', onRejection)
  }
}
