/** 相对时间（全站单源，36 §7 收编：kb/memory/ontology 域 shared 原同源实现已改为本模块再导出）。
 *  口径：<1min 刚刚 / <60min N 分钟前 / <24h N 小时前 / 昨天 / <7天 N 天前 / 其余本地日期 */
export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return iso
  const diff = Date.now() - then
  const min = Math.round(diff / 60_000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.round(min / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.round(h / 24)
  if (d === 1) return '昨天'
  if (d < 7) return `${d} 天前`
  return new Date(iso).toLocaleDateString()
}
