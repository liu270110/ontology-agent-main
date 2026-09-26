import { Monitor, Moon, Sun } from 'lucide-react'
import { useTheme, type ThemePref } from '@/app/providers/theme-provider'
import { cn } from '@/lib/cn'

/** 顶栏主题三态切换（亮 / 暗 / 跟随系统）：单事实源 = theme-provider，
 *  持久化 localStorage('oa-theme')（ui-store.theme 双事实源已随 S1 收口移除）。 */

const OPTIONS: { key: ThemePref; label: string; icon: typeof Sun }[] = [
  { key: 'light', label: '亮色', icon: Sun },
  { key: 'dark', label: '暗色', icon: Moon },
  { key: 'system', label: '跟随系统', icon: Monitor },
]

export function ThemeToggle() {
  const { pref, setPref } = useTheme()
  return (
    <div
      role="radiogroup"
      aria-label="主题"
      className="flex items-center gap-0.5 rounded-lg border border-separator bg-surface-2 p-0.5"
    >
      {OPTIONS.map(({ key, label, icon: Icon }) => (
        <button
          key={key}
          type="button"
          role="radio"
          aria-checked={pref === key}
          title={label}
          aria-label={label}
          onClick={() => setPref(key)}
          className={cn(
            'flex h-6 w-6 items-center justify-center rounded-md transition-colors',
            pref === key ? 'bg-accent-soft text-accent' : 'text-label-3 hover:text-label',
          )}
        >
          <Icon size={13} aria-hidden />
        </button>
      ))}
    </div>
  )
}
