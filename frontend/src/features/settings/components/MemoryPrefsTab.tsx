import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { getPreferences, putPreferences } from '../api'

/** IX-SET 记忆偏好 Tab（宿主 p-settings「记忆」卡；S-AD 切片）：记忆总开关 +
 *  群聊对话写入 L1 + 清除记忆走失效边（墓碑式软删可追溯）三 toggle，统一存
 *  /me/preferences 扩展字段（memory_enabled / memory_group_l1_write /
 *  memory_clear_via_invalidate）。开关样式与 mcp ServerDetailSheet role=switch 同构。
 *  清除动作本身留在记忆管理页（此处只管偏好，不触发删除）。 */

interface MemoryPrefs {
  memory_enabled: boolean
  memory_group_l1_write: boolean
  memory_clear_via_invalidate: boolean
}

const ROWS: { key: keyof MemoryPrefs; label: string; desc: string; testid: string }[] = [
  { key: 'memory_enabled', label: '记忆总开关', desc: '关闭后不再召回、不再沉淀', testid: 'set-memory-enabled' },
  { key: 'memory_group_l1_write', label: '群聊会话写入个性化记忆（L1）', desc: '群聊发言摘要进入会话级 L1 短期记忆', testid: 'set-memory-group-l1' },
  { key: 'memory_clear_via_invalidate', label: '清除记忆走失效边（可追溯）', desc: '清除产生失效边（invalidated），不物理删除，可在记忆管理页「含已失效」中追溯', testid: 'set-memory-invalidate' },
]

export function MemoryPrefsTab() {
  const [prefs, setPrefs] = useState<MemoryPrefs | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void getPreferences()
      .then(p => setPrefs({
        memory_enabled: p.memory_enabled ?? true,
        memory_group_l1_write: p.memory_group_l1_write ?? false,
        memory_clear_via_invalidate: p.memory_clear_via_invalidate ?? true,
      }))
      .catch(() => setPrefs({ memory_enabled: true, memory_group_l1_write: false, memory_clear_via_invalidate: true }))
  }, [])

  if (!prefs) return <div className="empty"><div className="t">加载中…</div></div>

  const toggle = (key: keyof MemoryPrefs) => {
    setPrefs({ ...prefs, [key]: !prefs[key] })
    setDirty(true)
  }

  const save = async () => {
    setSaving(true)
    try {
      await putPreferences(prefs)
      toast.success('记忆偏好已保存')
      setDirty(false)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <b className="text-sm">记忆</b>
        <span className={`badge ${prefs.memory_enabled ? 'b-green' : 'b-gray'}`} data-testid="set-memory-badge">
          {prefs.memory_enabled ? '已开启' : '已关闭'}
        </span>
      </div>
      <div className="card mt-3 !p-4" data-testid="set-memory-prefs">
        {ROWS.map((r, i) => (
          <div
            key={r.key}
            className={`flex items-center justify-between gap-3 py-2.5 ${i < ROWS.length - 1 ? 'border-b border-separator' : ''}`}
            data-testid={r.testid}
          >
            <div className="min-w-0">
              <b className="block text-xs">{r.label}</b>
              <span className="text-[11px] text-label-3">{r.desc}</span>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={prefs[r.key]}
              aria-label={r.label}
              data-testid={`${r.testid}-switch`}
              disabled={saving}
              className="relative h-5 w-9 flex-none rounded-full transition-colors"
              style={{ background: prefs[r.key] ? 'var(--green)' : 'var(--surface-2)', border: '1px solid var(--separator)' }}
              onClick={() => toggle(r.key)}
            >
              <span className="absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white shadow transition-all" style={{ left: prefs[r.key] ? 18 : 3 }} />
            </button>
          </div>
        ))}
        <div className="mt-2 text-[11px] leading-relaxed text-label-3">
          清除记忆在记忆管理页发起：产生失效边而非物理删除，出处与审计全程留痕。
        </div>
      </div>

      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          type="button"
          className="btn btn-p"
          data-testid="set-memory-save"
          disabled={!dirty || saving}
          onClick={() => void save()}
        >
          保存{dirty ? '' : '（无修改）'}
        </button>
      </div>
    </div>
  )
}
