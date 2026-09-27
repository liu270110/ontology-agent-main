import { useEffect, useState } from 'react'
import { Camera } from 'lucide-react'
import { toast } from 'sonner'
import { getPreferences, putPreferences } from '../api'

/** IX-SET-01 资料 Tab（26 篇 §3）：头像上传（圆形裁剪预览占位）+ 姓名/邮箱（邮箱只读）
 *  + 语言/时区选择 + 保存按钮（脏态才可点）。姓名/部门自更新走 PUT /me/preferences
 *  扩展字段（§5.9 无自更新端点，预登记见 R 清单）。 */
export function ProfileTab() {
  const [prefs, setPrefs] = useState<{ display_name: string; email: string; department: string; language: string; timezone: string } | null>(null)
  const [avatar, setAvatar] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void getPreferences().then(p =>
      setPrefs({ display_name: p.display_name, email: p.email, department: p.department, language: p.language, timezone: p.timezone }),
    )
  }, [])

  if (!prefs) return <div className="empty"><div className="t">加载中…</div></div>

  const patch = (p: Partial<typeof prefs>) => { setPrefs({ ...prefs, ...p }); setDirty(true) }

  const save = async () => {
    setSaving(true)
    try {
      await putPreferences(prefs)
      toast.success('资料已保存')
      setDirty(false)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <b className="text-[14px]">资料</b>

      {/* 头像（上传占位：选择本地文件圆形预览；裁剪上传服务端随 28 篇排期） */}
      <div className="mt-4 flex items-center gap-4">
        <span className="flex h-16 w-16 items-center justify-center overflow-hidden rounded-full bg-accent-soft text-xl font-semibold text-accent">
          {avatar
            ? <img src={avatar} alt="头像预览" className="h-full w-full object-cover" />
            : (prefs.display_name || prefs.email)[0]?.toUpperCase()}
        </span>
        <div>
          <label className="btn btn-g btn-sm cursor-pointer">
            <Camera size={13} aria-hidden /> 上传头像
            <input
              type="file"
              accept="image/*"
              className="hidden"
              onChange={e => {
                const f = e.target.files?.[0]
                if (f) setAvatar(URL.createObjectURL(f))
              }}
            />
          </label>
          <div className="fhint mt-1">支持 JPG/PNG，将按圆形裁剪（上传即预览）。</div>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-x-4 sm:grid-cols-2">
        <div className="field">
          <label className="field-label" htmlFor="set-profile-name">姓名</label>
          <input id="set-profile-name" data-testid="set-profile-name" className="input" value={prefs.display_name} onChange={e => patch({ display_name: e.target.value })} />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="set-profile-email">邮箱（只读）</label>
          <input id="set-profile-email" className="input text-label-3" value={prefs.email} readOnly aria-readonly />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="set-profile-lang">语言</label>
          <select id="set-profile-lang" className="input" value={prefs.language} onChange={e => patch({ language: e.target.value })}>
            <option value="zh-CN">简体中文</option>
            <option value="en-US">English (US)</option>
          </select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="set-profile-tz">时区</label>
          <select id="set-profile-tz" className="input" value={prefs.timezone} onChange={e => patch({ timezone: e.target.value })}>
            <option value="Asia/Shanghai">(GMT+8) 上海</option>
            <option value="UTC">(UTC) 协调世界时</option>
            <option value="America/New_York">(GMT-5) 纽约</option>
          </select>
        </div>
      </div>

      <div className="hairline-t mt-2 flex justify-end pt-3">
        <button type="button" className="btn btn-p" data-testid="set-profile-save" disabled={!dirty || saving} onClick={() => void save()}>
          保存{dirty ? '' : '（无修改）'}
        </button>
      </div>
    </div>
  )
}
