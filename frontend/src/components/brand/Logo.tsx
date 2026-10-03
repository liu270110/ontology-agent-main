import { useId } from 'react'

/** 品牌 Logo（29 篇 FE-ADR-FE2 品牌资产）：本体立方体三面渐变=对象—行为—规则，顶面光点=语义基座锚点。
 *  variant: gradient 主版（登录卡/品牌位）；mono 单色线性（侧栏小尺寸/深浅自适应）。 */
export function Logo({ variant = 'gradient', size = 26 }: { variant?: 'gradient' | 'mono'; size?: number }) {
  const id = useId().replace(/[:]/g, '')
  if (variant === 'mono') {
    return (
      <svg width={size} height={size} viewBox="0 0 64 64" role="img" aria-label="ontology-agent" style={{ color: 'inherit' }}>
        <g fill="none" stroke="currentColor" strokeWidth={3} strokeLinejoin="round">
          <path d="M32 6 56 19 56 45 32 58 8 45 8 19 Z" />
          <path d="M8 19 32 32 56 19 M32 32 V58" />
        </g>
        <circle cx="32" cy="19" r="3.5" fill="currentColor" />
      </svg>
    )
  }
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" role="img" aria-label="ontology-agent">
      <defs>
        <linearGradient id={`${id}t`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#9ed3ff" />
          <stop offset="1" stopColor="#0a84ff" />
        </linearGradient>
        <linearGradient id={`${id}l`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#0a84ff" />
          <stop offset="1" stopColor="#0050b4" />
        </linearGradient>
        <linearGradient id={`${id}r`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#0063e6" />
          <stop offset="1" stopColor="#003a8f" />
        </linearGradient>
        <radialGradient id={`${id}n`} cx="0.35" cy="0.3" r="1">
          <stop offset="0" stopColor="#ffffff" />
          <stop offset="1" stopColor="#bfe0ff" />
        </radialGradient>
      </defs>
      <polygon points="32,6 56,19 32,32 8,19" fill={`url(#${id}t)`} />
      <polygon points="8,19 32,32 32,58 8,45" fill={`url(#${id}l)`} />
      <polygon points="32,32 56,19 56,45 32,58" fill={`url(#${id}r)`} />
      <path d="M8 19 32 32 56 19 M32 32 V58" stroke="rgba(255,255,255,.5)" strokeWidth={1.4} fill="none" strokeLinejoin="round" />
      <path d="M8 19 32 6 56 19 56 45 32 58 8 45 Z" stroke="rgba(255,255,255,.35)" strokeWidth={1} fill="none" strokeLinejoin="round" />
      <circle cx="32" cy="19" r="5.5" fill="rgba(255,255,255,.25)" />
      <circle cx="32" cy="19" r="3.2" fill={`url(#${id}n)`} />
    </svg>
  )
}
