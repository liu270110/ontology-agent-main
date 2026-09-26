import { setupServer } from 'msw/node'
import { handlers } from './handlers'

/** vitest 用 MSW node 服务（16 篇 §8：测试一律走 MSW 契约仿真）。
 *  用例内经 server.use(...) 覆写单端点（如 401→refresh→重放演练）。 */
export const server = setupServer(...handlers)
