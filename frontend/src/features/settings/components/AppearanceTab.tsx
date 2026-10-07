import { useEffect, useState } from 'react'
import { Monitor, Moon, Sun } from 'lucide-react'
import { useTheme, type ThemePref } from '@/app/providers/theme-provider'
import { toast } from 'sonner'
import { getPreferences, putPreferences } from '../api'
import { Select } from '@/components/select'

/** IX-SET 外观与语言 Tab（宿主 p-settings L2053-2179；S-AD 切片）：
 *  主题三态三选卡（单事实源 = ThemeProvider，持久化 localStorage('oa-theme')——与顶栏
 *  ThemeToggle 同源，不另写 /me/preferences 防双事实源）+ 界面语言下拉（i18n 骨架仅 zh-CN，
 *  English 选项 disabled 标注「多语言包 v2 提供」→ 只读展示不可选）。
 *  语言选择走 PUT /me/preferences 扩展字段（admin-handlers Object.assign 透传）。
 *  玻璃强度：design-system/tokens 仅有固定 --glass-* 常量、无可调项 → 本 Tab 不提供该选择。 */

const THEME_OPTIONS: { key: ThemePref; label: string; desc: string; icon: typeof Sun }[] = [
  { key: 'light', label: '亮色', desc: '浅色底 · 日间办公', icon: Sun },
  { key: 'dark', label: '暗色', desc: '深色底 · 夜间值守', icon: Moon },
  { key: 'system', label: '跟随系统', desc: '随系统外观自动切换', icon: Monitor },
]

export function AppearanceTab() {
  const { pref, setPref } = useTheme()
  const [language, setLanguage] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void getPreferences()
      .then(p => setLanguage(p.language ?? 'zh-CN'))
      .catch(() => setLanguage('zh-CN'))
  }, [])

  const changeLanguage = async (lang: string) => {
    const prev = language ?? 'zh-CN'
    setLanguage(lang)
    setSaving(true)
    try {
      await putPreferences({ language: lang })
      toast.success('界面语言已保存')
    } catch (e) {
      setLanguage(prev)
      toast.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <b className="text-sm">外观与语言</b>

      {/* 主题三态三选卡（ThemeProvider 单事实源，即选即生效） */}
      <div className="card mt-3 !p-0" data-testid="set-appearance-theme">
        <div className="px-4 pt-3 text-xs font-semibold">主题</div>
        <div className="grid grid-cols-1 gap-2 p-3 sm:grid-cols-3" role="radiogroup" aria-label="主题">
          {THEME_OPTIONS.map(o => {
            const Icon = o.icon
            const active = pref === o.key
            return (
              <button
                key={o.key}
                type="button"
                role="radio"
                aria-checked={active}
                data-testid={`set-theme-${o.key}`}
                className="rounded-xl border p-3 text-left transition-colors"
                style={{
                  borderColor: active ? 'var(--accent)' : 'var(--separator)',
                  background: active ? 'var(--accent-soft)' : 'transparent',
                }}
                onClick={() => setPref(o.key)}
              >
                <span className="flex items-center gap-2">
                  <Icon size={14} style={{ color: active ? 'var(--accent)' : 'var(--label-3)' }} aria-hidden />
                  <b className="text-xs">{o.label}</b>
                </span>
                <span className="mt-1 block text-[11px] text-label-3">{o.desc}</span>
              </button>
            )
          })}
        </div>
        <div className="px-4 pb-3 text-[11px] text-label-3">主题偏好即选即生效，与顶栏切换按钮同源。</div>
      </div>

      {/* 界面语言（i18n 骨架仅 zh-CN；English 为 v2 多语言包预留） */}
      <div className="card mt-3 !p-4" data-testid="set-appearance-language">
        <div className="field mb-0">
          <label className="field-label" htmlFor="set-appearance-lang">界面语言</label>
          <Select
            id="set-appearance-lang"
            className="input"
            data-testid="set-appearance-lang"
            value={language ?? 'zh-CN'}
            disabled={saving || language === null}
            onChange={e => void changeLanguage(e.target.value)}
          >
            <option value="zh-CN">简体中文</option>
            <option value="en" disabled>English（多语言包 v2 提供）</option>
          </Select>
        </div>
        <div className="mt-2 text-[11px] text-label-3">多语言能力随多语言包 v2 交付；当前仅简体中文可选。</div>
      </div>
    </div>
  )
}
