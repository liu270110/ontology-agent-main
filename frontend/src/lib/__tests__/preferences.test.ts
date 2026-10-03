import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '@/mocks/node'
import { createDefaultPreferences, getPreferences, PREFERENCES_DEFAULTS } from '@/lib/preferences'

/** getPreferences 降级口径单测（ocr fe2 整改 发现5/发现6）：
 *  发现5——降级集收窄 {404,501}：me2 批后端裁决后 503=瞬时故障（实现端点已规范化为
 *  5004 四字段错误体），降级默认值会让消费者全量 PUT 覆盖服务端真实偏好（静默数据丢失
 *  链）→ 503 必须照抛；404（未实现）仍降级。
 *  发现6——降级实例经 createDefaultPreferences 深隔离：notifications 不与
 *  PREFERENCES_DEFAULTS 常量共享嵌套对象。
 *  注：server.listen/close 由全局 setup（src/mocks/node-setup.ts）统一启停——
 *  文件内再 listen 会撞 msw「already enabled network」（基线收集失败偶发同机理），不自带。 */

afterEach(() => server.resetHandlers())

describe('getPreferences 降级口径（ocr fe2 发现5/6）', () => {
  it('发现5：503（瞬时故障）不降级默认值——ApiError 照抛（httpStatus 503）', async () => {
    server.use(
      http.get('*/api/v1/me/preferences', () =>
        HttpResponse.json({ code: 5004, message: '依赖服务暂不可用' }, { status: 503 }),
      ),
    )

    await expect(getPreferences()).rejects.toMatchObject({ httpStatus: 503, code: 5004 })
  })

  it('发现5 续：404（端点未实现）仍降级本地默认值', async () => {
    server.use(
      http.get('*/api/v1/me/preferences', () =>
        HttpResponse.json({ detail: 'Not Found' }, { status: 404 }),
      ),
    )

    const prefs = await getPreferences()
    expect(prefs.language).toBe('zh-CN')
    expect(prefs.notifications).toEqual({})
  })

  it('发现6：降级实例与默认常量深隔离——改实例 notifications 不污染 PREFERENCES_DEFAULTS', async () => {
    server.use(
      http.get('*/api/v1/me/preferences', () =>
        HttpResponse.json({ detail: 'Not Found' }, { status: 404 }),
      ),
    )

    const prefs = await getPreferences()
    prefs.notifications['task_finished'] = { inapp: true, email: false }

    expect(PREFERENCES_DEFAULTS.notifications).toEqual({})
    expect(createDefaultPreferences().notifications).toEqual({})
    expect(createDefaultPreferences()).not.toBe(PREFERENCES_DEFAULTS)
  })
})
