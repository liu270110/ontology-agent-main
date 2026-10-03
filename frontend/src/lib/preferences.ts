import { api, ApiError } from '@/api/client'

/** 当前用户偏好（/me/preferences，api/01 §5.13 me 预登记；DTO 与 mocks/admin-handlers.ts
 *  PREFS 一一对应）。跨域消费（工作台新手引导卡 onboarding-card + 设置各 Tab）→
 *  按 02 篇 §4 下沉共享层 src/lib，features/settings/api 经再导出保持域内引用面不变
 *  （16 篇 §1 架构守卫：features 域间禁止横向 import）。 */

export interface Preferences {
  display_name: string
  email: string
  department: string
  language: string
  timezone: string
  totp_enabled: boolean
  notifications: Record<string, { inapp: boolean; email: boolean; locked?: boolean }>
  /** ---- S-AD 切片扩展偏好（全部可选；PUT /me/preferences mock 侧 Object.assign 透传合并，
   *  GET 原样带回；旧后端忽略未知字段亦不破坏读写） ---- */
  /** 新手引导完成标记（工作台全三步完成时写入；true 后引导卡不再渲染） */
  onboarding_done?: boolean
  /** 对话偏好：默认模型（claude-sonnet | gpt-4o | deepseek） */
  chat_default_model?: string
  /** 对话偏好：思考档位（standard 标准 | deep 深度 | flash 闪电） */
  chat_thinking_level?: string
  /** 对话偏好：群聊编排默认模式（mention | round_robin | all | orchestrator，文案同 lib/routing ROUTING_LABEL） */
  group_routing_default?: string
  /** 记忆：总开关（关闭后不再召回、不再沉淀） */
  memory_enabled?: boolean
  /** 记忆：群聊会话写入个性化记忆（L1） */
  memory_group_l1_write?: boolean
  /** 记忆：清除记忆走失效边（墓碑式软删，可追溯） */
  memory_clear_via_invalidate?: boolean
}

/** 本地默认偏好（F8⑥ B:A-9 前端半：/me/preferences 404——后端 me 域未挂载（live 实测
 *  2026-10-04 裸 404 {detail}）→ 降级本地默认值继续渲染，设置页不再永挂「加载中…」。
 *  display_name/email 留空由消费方以 auth-store 用户信息兜底；PUT 在降级态下仍可发起
 *  （后端上线后自然转正），保存失败照常 toast 不静默。 */
export const PREFERENCES_DEFAULTS: Preferences = {
  display_name: '',
  email: '',
  department: '',
  language: 'zh-CN',
  timezone: 'Asia/Shanghai',
  totp_enabled: false,
  notifications: {},
}

export const getPreferences = async (): Promise<Preferences> => {
  try {
    return await api.get<Preferences>('/me/preferences')
  } catch (e) {
    // 仅对「端点不存在/未实现」降级（404/501/503——B7 后端裁决口径的占位路由形态）；
    // 鉴权失败（401/1003）等照抛，避免掩盖会话过期
    if (e instanceof ApiError && (e.httpStatus === 404 || e.httpStatus === 501 || e.httpStatus === 503)) {
      return { ...PREFERENCES_DEFAULTS }
    }
    throw e
  }
}
export const putPreferences = (body: Partial<Preferences>) => api.put<Preferences>('/me/preferences', body)
