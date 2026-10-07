import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  API_NETWORK_CODE,
  API_TIMEOUT_CODE,
  ApiError,
  DEFAULT_TIMEOUT_MS,
  NETWORK_UNAVAILABLE_MESSAGE,
} from '@/api/client'
import { KB_UPLOAD_TIMEOUT_MS, registerDocument } from '../api'
import { server } from '@/mocks/node'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts），本文件不重复 listen。
afterEach(() => {
  server.resetHandlers()
  vi.useRealTimers()
})

/** P-012（台账-生产化-2026-10-07）：kb 上传链路 postKbRaw 裸 fetch 收口——
 *  ① 独立长超时：120s（KB_UPLOAD_TIMEOUT_MS），非全站 15s 缺省（大文件 JSON 直传 15s 必超时）；
 *  ② 网络层失败归一 ApiError(-1)、超时归一 ApiError(-2)，文案=NETWORK_UNAVAILABLE_MESSAGE
 *     （全站口径，UploadDialog 经 describeError 可直接呈现行动指引文案）；
 *  ③ 成功路径语义不变（双形态兼容剥信封；401 重放与既有契约测试覆盖，此处回归一例）。 */

const REGISTER_BODY = { collection_id: 'col-1', title: '大文件.pdf', content: 'x'.repeat(64) }

describe('P-012 上传链路超时与网络错归一（postKbRaw → fetchWithTimeout 120s）', () => {
  it('① 成功路径回归：201 信封剥内层返回 DTO（重构不改成功语义）', async () => {
    const doc = await registerDocument(REGISTER_BODY)
    expect(doc).toMatchObject({ name: '大文件.pdf', status: 'pending' })
  })

  it('② 网络层失败（fetch reject=TypeError）→ ApiError(-1) 全站文案（不再裸抛 TypeError）', async () => {
    server.use(http.post('*/api/v1/kb/documents', () => HttpResponse.error()))
    const err = await registerDocument(REGISTER_BODY).then(
      () => null,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).code).toBe(API_NETWORK_CODE)
    expect((err as ApiError).message).toBe(NETWORK_UNAVAILABLE_MESSAGE)
    expect((err as ApiError).httpStatus).toBeUndefined()
  })

  it('③ 超时=独立 120s：走过 15s 全站缺省点仍在传（未复用 15s 版本）；到 120s 抛 ApiError(-2)', async () => {
    server.use(http.post('*/api/v1/kb/documents', () => new Promise<never>(() => {}))) // 服务端永挂起
    vi.useFakeTimers()
    const settle = registerDocument(REGISTER_BODY).then(
      () => 'resolved' as const,
      (e: unknown) => e,
    )
    // 走过全站 15s 缺省超时点：请求仍在传（独立长超时生效的直接证据）
    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS)
    expect(await Promise.race([settle, 'pending' as const])).toBe('pending')
    // 走到 120s：超时竞速胜出 → ApiError(-2)
    await vi.advanceTimersByTimeAsync(KB_UPLOAD_TIMEOUT_MS - DEFAULT_TIMEOUT_MS)
    const err = (await settle) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe(API_TIMEOUT_CODE)
    expect(err.message).toBe(NETWORK_UNAVAILABLE_MESSAGE)
  })
})
