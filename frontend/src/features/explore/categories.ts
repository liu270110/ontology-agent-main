/** 图谱浏览分类口径（E10 裁决 + E1/E2 共用单源；41 篇 §1.2/§2 V3、37 号:18 登记）：
 *  画板 ui-pages p-explore 顶栏 seg 为「全部/标准/条款/特性/约束」（标准文档域示例档，
 *  ui-pages.html:918 实测），代码无对应分类族——按 37 号登记的口径裁决：以代码
 *  CATEGORY_COLOR 实际分类族（GraphCanvas.tsx:142-152）为准，扩为 5 档
 *  「全部/对象/事件/约束/规则」。37 号 E10 销账注记：画板档位（标准/条款/特性/约束）
 *  是文档域文案，与 ABox 分类族（object/device/area/fault/event/workorder/rule/constraint）
 *  无映射关系，不照抄画板文案、对齐代码真实分类族（2026-10-05）。 */

export type ExploreBucket = 'object' | 'event' | 'constraint' | 'rule'
export type ExploreSeg = 'all' | ExploreBucket

/** 顶栏 seg 五档（E10：4 档 → 5 档，补「规则」档） */
export const EXPLORE_SEGMENTS: { key: ExploreSeg; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'object', label: '对象' },
  { key: 'event', label: '事件' },
  { key: 'constraint', label: '约束' },
  { key: 'rule', label: '规则' },
]

/** 图例档位 → 代表色类目（E2：色值经 categoryColor 单源取令牌，不另造色表） */
export const EXPLORE_BUCKETS: { key: ExploreBucket; label: string; colorCategory: string }[] = [
  { key: 'object', label: '对象', colorCategory: 'object' },
  { key: 'event', label: '事件', colorCategory: 'event' },
  { key: 'constraint', label: '约束', colorCategory: 'constraint' },
  { key: 'rule', label: '规则', colorCategory: 'rule' },
]

/** 显式分类 → 档位映射表（E1 连带修，41 篇 V3 评审 P2：替换旧 segGroup
 *  「非 event/constraint 一律归 object」兜底桶——5 档改造漏配时 device/area 会静默
 *  落错桶）。同族归并口径（与 CATEGORY_COLOR 色族一致）：object/line（accent）与
 *  device/area/workorder（设备/区域/工单，对象性类目）→「对象」；fault 与 event 同橙
 *  →「事件」；constraint（红）单独档；rule（靛）单独档。 */
const BUCKET_MAP: Record<string, ExploreBucket> = {
  object: 'object',
  line: 'object',
  device: 'object',
  area: 'object',
  workorder: 'object',
  event: 'event',
  fault: 'event',
  constraint: 'constraint',
  rule: 'rule',
}

/** 已告警集合：未知类 console.warn 一次/类，防画布逐帧刷屏 */
const warnedCategories = new Set<string>()

/** 分类 → 档位：显式表命中直返；未登记类归「对象」并 console.warn 一次（41 篇 V3 评审
 *  P2 要求的显式兜底——不静默），提示补表而非吞差异。 */
export function categoryBucket(category: string): ExploreBucket {
  const hit = BUCKET_MAP[category]
  if (hit) return hit
  if (!warnedCategories.has(category)) {
    warnedCategories.add(category)
    console.warn(`[explore] 未登记分类「${category}」，暂归「对象」档（explore/categories.ts BUCKET_MAP 待补）`)
  }
  return 'object'
}
