import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { ApiError, api } from '@/api/client'
import { retryQueryOnError } from '../App'
import { server } from '@/mocks/node'

afterEach(() => {
  server.resetHandlers()
  cleanup()
})

/** P-012 retry 豁免（台账-生产化-2026-10-07）：全局 QueryClient retry 由「一律 1 次」改为
 *  4xx 族（404 / 信封 code=1004 / 权限 2xxx）不重试，仅网络错（-1/-2）/5xx/非 ApiError
 *  保留 retry:1（瞬时故障可能自愈）。
 *  ① 策略函数单测（App.tsx retryQueryOnError，导出即测试口）；
 *  ② 行为断言（本任务指定）：mock 404 端点 + query 配置 → fetch 计数=1；
 *     对照组：网络错 / 5xx → fetch 计数=2（豁免面之外仍重试 1 次）。
 *  测试 QueryClient 与 App.tsx 引用同一 retry 策略函数，仅 retryDelay=0 加速（不影响计数）；
 *  MutationCache 全局 onError 有意不加（操作就地 toast，全局兜底=toast 风暴，维持现状）。 */

describe('P-012 retry 策略单测（retryQueryOnError）', () => {
  it('4xx 族不重试：404（裸态与信封态）/ 信封 1004 无 status / 权限 2xxx / 429', () => {
    expect(retryQueryOnError(0, new ApiError(-1, 'HTTP 404', 404))).toBe(false) // 裸 404：无业务码，仅 httpStatus
    expect(retryQueryOnError(0, new ApiError(1004, '后端未实装：该功能接口待交付', 404))).toBe(false)
    expect(retryQueryOnError(0, new ApiError(1004, '后端未实装：该功能接口待交付'))).toBe(false) // 信封形态缺 status 亦豁免
    expect(retryQueryOnError(0, new ApiError(2001, '权限不足', 403))).toBe(false)
    expect(retryQueryOnError(0, new ApiError(2002, '权限不足', 403))).toBe(false)
    expect(retryQueryOnError(0, new ApiError(1005, '请求过于频繁', 429))).toBe(false)
  })

  it('网络错/超时与 5xx 重试 1 次（failureCount<1 即停）；非 ApiError 维持 retry:1', () => {
    expect(retryQueryOnError(0, new ApiError(-1, '网络连接不可用，请检查后端服务'))).toBe(true)
    expect(retryQueryOnError(1, new ApiError(-1, '网络连接不可用，请检查后端服务'))).toBe(false)
    expect(retryQueryOnError(0, new ApiError(-2, '网络连接不可用，请检查后端服务'))).toBe(true)
    expect(retryQueryOnError(1, new ApiError(-2, '网络连接不可用，请检查后端服务'))).toBe(false)
    expect(retryQueryOnError(0, new ApiError(5001, '上游模型服务异常', 500))).toBe(true)
    expect(retryQueryOnError(1, new ApiError(5001, '上游模型服务异常', 500))).toBe(false)
    expect(retryQueryOnError(0, new Error('boom'))).toBe(true)
    expect(retryQueryOnError(1, new Error('boom'))).toBe(false)
  })
})

// ---- 行为断言：策略挂进真实 QueryClient + MSW 计数（策略函数与 App.tsx 同源引用） ----

function useQueryWithPolicy(queryKey: unknown[], path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: retryQueryOnError, retryDelay: () => 0 } },
  })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return renderHook(() => useQuery({ queryKey, queryFn: () => api.get(path) }), { wrapper })
}

describe('P-012 retry 行为断言（mock 端点 fetch 计数）', () => {
  it('404 端点 + query 配置：fetch 计数=1（4xx 豁免，直接失败不重试）', async () => {
    let hits = 0
    server.use(
      http.get('*/api/v1/p012/ghost', () => {
        hits++
        return HttpResponse.json({ detail: 'Not Found' }, { status: 404 })
      }),
    )
    const { result } = useQueryWithPolicy(['p012-404'], '/p012/ghost')
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error).toMatchObject({ httpStatus: 404 })
    expect(hits).toBe(1)
  })

  it('对照·网络错端点：fetch 计数=2（网络层故障保留 retry:1）', async () => {
    let hits = 0
    server.use(
      http.get('*/api/v1/p012/netdown', () => {
        hits++
        return HttpResponse.error() // fetch reject=TypeError → 归一 ApiError(-1)
      }),
    )
    const { result } = useQueryWithPolicy(['p012-net'], '/p012/netdown')
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error).toMatchObject({ code: -1 })
    expect(hits).toBe(2)
  })

  it('对照·5xx 端点：fetch 计数=2（服务端错误保留 retry:1）', async () => {
    let hits = 0
    server.use(
      http.get('*/api/v1/p012/broken', () => {
        hits++
        return HttpResponse.json({ code: 5001, message: '上游模型服务异常' }, { status: 500 })
      }),
    )
    const { result } = useQueryWithPolicy(['p012-5xx'], '/p012/broken')
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error).toMatchObject({ httpStatus: 500 })
    expect(hits).toBe(2)
  })
})
