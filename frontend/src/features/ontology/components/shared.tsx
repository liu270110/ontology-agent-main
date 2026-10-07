import type { ChangesetStatus, OntoTier } from '../api'
import { CS_STATUS_LABEL, TIER_LABEL } from '../api'

/** 本体域共享小件：方案徽标 / changeset 状态徽标 / 三色计数 chips / 相对时间。
 *  颜色只走令牌（03 篇铁律）；与 kb 域 shared 解耦（域间不互引）。 */

export function TierBadge({ tier }: { tier: OntoTier }) {
  const cls = tier === 'heavy' ? 'b-purple' : tier === 'standard' ? 'b-blue' : 'b-gray'
  return <span className={`badge ${cls}`}>{TIER_LABEL[tier]}</span>
}

export function CsStatusBadge({ status }: { status: ChangesetStatus }) {
  const cls =
    status === 'published' ? 'b-green' : status === 'in_review' ? 'b-orange' : status === 'approved' ? 'b-blue' : status === 'rejected' ? 'b-red' : 'b-gray'
  return <span className={`badge ${cls}`}>{CS_STATUS_LABEL[status]}</span>
}

/** 三色变更计数 chips（+绿/−红/~黄；画板 IX-ON-06 / IX-VR-03 同源） */
export function ChangeCountChips({ stats, className = '' }: { stats: { add: number; del: number; mod: number }; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`}>
      <span className="badge b-green">+{stats.add}</span>
      <span className="badge b-red">−{stats.del}</span>
      <span className="badge b-orange">~{stats.mod}</span>
    </span>
  )
}

/** 相对时间（列表口径同 kb 域）——单源 lib/reltime（36 §7 收编；再导出兼容既有引用） */
export { relativeTime } from '@/lib/reltime'

/** 命名空间 IRI zod 校验共用正则（必须为绝对 IRI 且以 # 或 / 结尾） */
export const NAMESPACE_IRI_REGEX = /^https?:\/\/[^\s]+[#/]$/
