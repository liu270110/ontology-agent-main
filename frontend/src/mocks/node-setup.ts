import { beforeAll, afterEach, afterAll } from 'vitest'
import { server } from './node'

// vitest 全局启停 MSW node server（16 篇 §8：测试一律走契约仿真）。
// jsdom 相对 URL fetch 必须由 MSW node 拦截（缺 listen 时 login 抛 Invalid URL）。
beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())
