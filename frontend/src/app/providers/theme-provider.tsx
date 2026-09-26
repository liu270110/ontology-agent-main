import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

/** 用户主题偏好：三态（亮 / 暗 / 跟随系统）——S1 主题面板收口后的唯一事实源。
 *  （ui-store.theme 未接线字段已移除，消双事实源；30 篇 §3-3 / 03 篇令牌唯一事实源。） */
export type ThemePref = 'light' | 'dark' | 'system'
/** 实际生效主题（解析 system 后），html .dark 类据此切换 */
export type Theme = 'light' | 'dark'

interface ThemeContextValue {
  /** 用户偏好（三态） */
  pref: ThemePref
  /** 实际生效主题 */
  theme: Theme
  toggle: () => void
  setPref: (t: ThemePref) => void
  /** 兼容别名：等价 setPref */
  setTheme: (t: ThemePref) => void
}

const ThemeContext = createContext<ThemeContextValue>({
  pref: 'system',
  theme: 'light',
  toggle: () => {},
  setPref: () => {},
  setTheme: () => {},
})

const STORAGE_KEY = 'oa-theme'
const DARK_QUERY = '(prefers-color-scheme: dark)'

function initialPref(): ThemePref {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'light' || saved === 'dark' || saved === 'system') return saved
  // 首访跟随系统
  return 'system'
}

/** 主题 Provider（30 篇 §3-3）：偏好三态，持久化 localStorage('oa-theme')；
 *  实际生效 = 偏好解析（system → prefers-color-scheme，jsdom 等无 matchMedia 环境回落 light），
 *  令牌实际取值由 design-system/tokens/tokens.css 的 .dark 块提供，本层不写任何色值。 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [pref, setPref] = useState<ThemePref>(initialPref)
  const systemPrefersDark = useSystemPrefersDark()

  const theme: Theme = pref === 'system' ? (systemPrefersDark ? 'dark' : 'light') : pref

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    localStorage.setItem(STORAGE_KEY, pref)
  }, [theme, pref])

  const toggle = useCallback(() => setPref(_p => (theme === 'dark' ? 'light' : 'dark')), [theme])

  const value = useMemo(
    () => ({ pref, theme, toggle, setPref, setTheme: setPref }),
    [pref, theme, toggle],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

/** system 档的系统偏好监听（无 matchMedia 环境（jsdom）恒 light）。 */
function useSystemPrefersDark(): boolean {
  const [dark, setDark] = useState(() => window.matchMedia?.(DARK_QUERY).matches ?? false)
  useEffect(() => {
    const mql = window.matchMedia?.(DARK_QUERY)
    if (!mql) return
    const onChange = (e: MediaQueryListEvent) => setDark(e.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [])
  return dark
}

export function useTheme() {
  return useContext(ThemeContext)
}
