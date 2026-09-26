import { Construction } from 'lucide-react'
import type { RouteMeta } from './routes'

/** 未上线页面占位（M1 壳骨架批）：页面随 S2~S7 切片逐域挂载（30 篇 §2 切片表）。 */
export function PlaceholderPage({ meta }: { meta: RouteMeta }) {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-soft text-accent">
        <Construction size={26} aria-hidden />
      </div>
      <h1 className="mt-4 text-base font-bold">{meta.title}</h1>
      <p className="mt-1.5 text-xs text-label-2">此模块随 M1 后续切片上线，当前为壳骨架占位页。</p>
      <code className="mt-3 rounded bg-surface-2 px-2 py-1 font-mono text-[11px] text-label-3">{meta.path}</code>
    </div>
  )
}
