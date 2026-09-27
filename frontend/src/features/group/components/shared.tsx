/** 群聊域共享小件：AgentAvatar（五分类色板着色，27 篇 §2）/ 角色徽标 / 颜色映射。
 *  颜色只走令牌（03 篇铁律）。 */

export type AvatarColor = 'purple' | 'green' | 'orange' | 'teal' | 'indigo' | 'red' | 'gray'

export const AVATAR_COLOR: Record<AvatarColor, { bg: string; fg: string }> = {
  purple: { bg: 'var(--purple-soft)', fg: 'var(--purple)' },
  green: { bg: 'var(--green-soft)', fg: 'var(--green)' },
  orange: { bg: 'var(--orange-soft)', fg: 'var(--orange)' },
  teal: { bg: 'var(--teal-soft)', fg: 'var(--teal)' },
  indigo: { bg: 'var(--indigo-soft)', fg: 'var(--indigo)' },
  red: { bg: 'var(--red-soft)', fg: 'var(--red)' },
  gray: { bg: 'var(--surface-2)', fg: 'var(--label-2)' },
}

export function AgentAvatar({
  name,
  color = 'gray',
  size = 30,
}: {
  name: string
  color?: AvatarColor
  size?: number
}) {
  const c = AVATAR_COLOR[color]
  return (
    <span
      className="flex flex-none items-center justify-center rounded-full font-semibold"
      style={{ width: size, height: size, fontSize: size <= 26 ? 10 : 12, background: c.bg, color: c.fg }}
      aria-hidden
    >
      {name.slice(0, 1)}
    </span>
  )
}

export function ModelChip({ model }: { model: string }) {
  if (!model) return null
  return <span className="chipmodel">{model}</span>
}

export function RoleBadge({ role }: { role: 'coordinator' | 'speaker' | 'observer' }) {
  if (role === 'coordinator') return <span className="badge b-purple">协调者</span>
  if (role === 'speaker') return <span className="badge b-blue">发言者</span>
  return <span className="badge b-gray">观察者</span>
}
