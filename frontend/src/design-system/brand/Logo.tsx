import { useId } from 'react'

/** 平台 Logo（本体立方体）：等距三面体=本体/知识块结构，三面取品牌渐变
 *  （顶面 iOS 蓝 / 左棱 indigo / 右棱 teal），顶点光点=语义基座锚点。
 *  gradient=彩色版（侧栏品牌区/favicon），mono=单色版（打印/水印/禁渐变场景）。 */
export function Logo({ size = 22, variant = 'gradient' }: { size?: number; variant?: 'gradient' | 'mono' }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, '')
  const gid = `oa-${variant}-${uid}`

  if (variant === 'mono') {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M12 2.5 20.5 7.25 12 12 3.5 7.25Z" fill="currentColor" opacity=".92" />
        <path d="M3.5 7.25 12 12v9.5l-8.5-4.75Z" fill="currentColor" opacity=".6" />
        <path d="M20.5 7.25v9.5L12 21.5V12Z" fill="currentColor" opacity=".38" />
        <circle cx="12" cy="5" r="1.4" fill="currentColor" opacity=".55" />
      </svg>
    )
  }

  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id={`${gid}-top`} x1="12" y1="2.5" x2="12" y2="12" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#5ab8ff" />
          <stop offset="1" stopColor="#0a84ff" />
        </linearGradient>
        <linearGradient id={`${gid}-left`} x1="3.5" y1="7.25" x2="12" y2="21.5" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#6e6ee8" />
          <stop offset="1" stopColor="#3f3cb4" />
        </linearGradient>
        <linearGradient id={`${gid}-right`} x1="20.5" y1="7.25" x2="12" y2="21.5" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#3fc3dc" />
          <stop offset="1" stopColor="#157f97" />
        </linearGradient>
      </defs>
      {/* 三面体 */}
      <path d="M12 2.5 20.5 7.25 12 12 3.5 7.25Z" fill={`url(#${gid}-top)`} />
      <path d="M3.5 7.25 12 12v9.5l-8.5-4.75Z" fill={`url(#${gid}-left)`} />
      <path d="M20.5 7.25v9.5L12 21.5V12Z" fill={`url(#${gid}-right)`} />
      {/* 玻璃质感：顶面棱线高光 + 内侧折面暗缘 */}
      <path d="M12 2.5 20.5 7.25 12 12 3.5 7.25Z" stroke="rgba(255,255,255,.45)" strokeWidth=".5" />
      <path d="M3.5 7.25 12 12v9.5" stroke="rgba(255,255,255,.22)" strokeWidth=".5" />
      <path d="M20.5 7.25 12 12v9.5" stroke="rgba(0,0,0,.18)" strokeWidth=".5" />
      {/* 顶点光点=语义基座锚点 */}
      <circle cx="12" cy="4.6" r="2.6" fill="#7cc4ff" opacity=".3" />
      <circle cx="12" cy="4.6" r="1.3" fill="#eaf6ff" />
    </svg>
  )
}
