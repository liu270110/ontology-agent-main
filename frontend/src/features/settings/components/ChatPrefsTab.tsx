import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { ErrorState } from '@/components/states'
import { getPreferences, putPreferences } from '../api'
import { ROUTING_LABEL } from '@/lib/routing'
import { Select } from '@/components/select'

/** IX-SET 对话偏好 Tab（宿主 p-settings「对话偏好」卡；S-AD 切片）：默认模型渠道 +
 *  思考档位 seg（标准/深度/闪电）+ 群聊默认发言编排（四模式文案复用 group 域
 *  ROUTING_LABEL）。均为「默认值 · 对话页可改」的本地偏好，统一存 /me/preferences
 *  扩展字段（chat_default_model / chat_thinking_level / group_routing_default，
 *  mock 侧 Object.assign 透传合并）。
 *  43 号验收 P2-5：模型选项去掉「主力/备用/成本优先」渠道身份文案（live 渠道注册表
 *  /admin/models 未实装，无真实数据源），fhint 明示「由系统管理统一配置」；读降级
 *  （404/501 → degraded）与非 404 读失败分别以 fhint/ErrorState 显性化，不再静默吞。 */

/** 渠道中性化：只保留平台已登记的模型枚举值（lib/preferences DTO），渠道注册表
 *  （/admin/models）交付后改由其动态拉取可用渠道。 */
const MODEL_OPTIONS = [
  { value: 'claude-sonnet', label: 'claude-sonnet（平台默认）' },
  { value: 'gpt-4o', label: 'gpt-4o' },
  { value: 'deepseek', label: 'deepseek' },
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
  const [degraded, setDegraded] = useState(false)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [reloadTick, setReloadTick] = useState(0)

  useEffect(() => {
    setDegraded(false)
    setLoadError(null)
    void getPreferences()
      .then(p => {
        if (p.chat_default_model) setModel(p.chat_default_model)
        if (p.chat_thinking_level) setThinking(p.chat_thinking_level)
        if (p.group_routing_default) setRouting(p.group_routing_default)
        setDegraded(!!p.degraded)
      })
      .catch(e => { setLoadError(e) })
      .finally(() => setLoaded(true))
  }, [reloadTick])

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
      {loadError != null ? (
        <div className="mt-3">
          <ErrorState
            message={loadError instanceof Error ? loadError.message : undefined}
            code={loadError instanceof ApiError ? loadError.code : undefined}
            onRetry={() => setReloadTick(t => t + 1)}
          />
        </div>
      ) : (
        <>
          {degraded && (
            <div className="fhint mt-2" data-testid="set-chat-degraded-hint">
              偏好服务未接入（功能建设中），当前展示为本地默认值，尚未与服务端同步。
            </div>
          )}
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
              <div className="fhint">模型渠道由系统管理统一配置；渠道注册表交付后此处将展示可用渠道。</div>
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
        </>
      )}

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
