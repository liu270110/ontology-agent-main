/** 相对时间（S6 治理域列表口径；kb/ontology 域内各自持有同源实现，域间不互引） */
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
