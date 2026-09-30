import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { getPreferences, putPreferences } from '../api'
import { ROUTING_LABEL } from '@/features/group/api'
import { Select } from '@/components/select'

/** IX-SET 对话偏好 Tab（宿主 p-settings「对话偏好」卡；S-AD 切片）：默认模型渠道 +
 *  思考档位 seg（标准/深度/闪电）+ 群聊默认发言编排（四模式文案复用 group 域
 *  ROUTING_LABEL）。均为「默认值 · 对话页可改」的本地偏好，统一存 /me/preferences
 *  扩展字段（chat_default_model / chat_thinking_level / group_routing_default，
 *  mock 侧 Object.assign 透传合并）。 */

const MODEL_OPTIONS = [
  { value: 'claude-sonnet', label: 'Claude Sonnet（渠道 · 主力）' },
  { value: 'gpt-4o', label: 'GPT-4o（渠道 · 备用）' },
  { value: 'deepseek', label: 'DeepSeek（渠道 · 成本优先）' },
]

const THINKING_LEVELS = [
  { value: 'standard', label: '标准' },
  { value: 'deep', label: '深度' },
  { value: 'flash', label: '闪电' },
]

export function ChatPrefsTab() {
  const [model, setModel] = useState('claude-sonnet')
  const [thinking, setThinking] = useState('standard')
  const [routing, setRouting] = useState('orchestrator')
  const [loaded, setLoaded] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void getPreferences()
      .then(p => {
        if (p.chat_default_model) setModel(p.chat_default_model)
        if (p.chat_thinking_level) setThinking(p.chat_thinking_level)
        if (p.group_routing_default) setRouting(p.group_routing_default)
      })
      .catch(() => { /* 读失败用默认值 */ })
      .finally(() => setLoaded(true))
  }, [])

  const save = async () => {
    setSaving(true)
    try {
      await putPreferences({ chat_default_model: model, chat_thinking_level: thinking, group_routing_default: routing })
      toast.success('对话偏好已保存')
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
        <b className="text-sm">对话偏好</b>
        <span className="badge b-gray">默认值 · 对话页可改</span>
      </div>
      <div className="card mt-3 !p-4" data-testid="set-chat-prefs">
        <div className="field">
          <label className="field-label" htmlFor="set-chat-model">默认模型渠道</label>
          <Select
            id="set-chat-model"
            className="input"
            data-testid="set-chat-model"
            value={model}
            disabled={!loaded || saving}
            onChange={e => { setModel(e.target.value); setDirty(true) }}
          >
            {MODEL_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </Select>
        </div>

        <div className="field">
          <span className="field-label">默认思考档位</span>
          <div className="seg w-full" role="radiogroup" aria-label="思考档位" data-testid="set-chat-thinking">
            {THINKING_LEVELS.map(t => (
              <button
                key={t.value}
                type="button"
                role="radio"
                aria-checked={thinking === t.value}
                data-testid={`set-chat-thinking-${t.value}`}
                className={`seg-btn flex-1 ${thinking === t.value ? 'on' : ''}`}
                disabled={!loaded || saving}
                onClick={() => { setThinking(t.value); setDirty(true) }}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        <div className="field mb-0">
          <label className="field-label" htmlFor="set-chat-routing">群聊默认发言编排</label>
          <Select
            id="set-chat-routing"
            className="input"
            data-testid="set-chat-routing"
            value={routing}
            disabled={!loaded || saving}
            onChange={e => { setRouting(e.target.value); setDirty(true) }}
          >
            {(Object.keys(ROUTING_LABEL) as (keyof typeof ROUTING_LABEL)[]).map(m => (
              <option key={m} value={m}>{ROUTING_LABEL[m]}</option>
            ))}
          </Select>
          <div className="mt-1.5 text-[11px] text-label-3">新建群聊会话的初始路由；会话内可随时切换（切换写审计）。</div>
        </div>
      </div>

      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          type="button"
          className="btn btn-p"
          data-testid="set-chat-save"
          disabled={!loaded || !dirty || saving}
          onClick={() => void save()}
        >
          保存{dirty ? '' : '（无修改）'}
        </button>
      </div>
    </div>
  )
}
