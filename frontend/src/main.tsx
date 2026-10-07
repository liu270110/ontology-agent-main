import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './app/App'
import { enableMockIfDev } from './mocks/enable-mock'
import { installGlobalErrorCatch } from './lib/errlog'
import './app/index.css'

// 全局错误兜底收集（P-005 S1 本地兜底，台账-生产化-2026-10-07）：JS 运行时错误 +
// 未处理 Promise 拒绝 → oa-errlog 本地环形（与 ErrorBoundary 共享写入，1s 同 message 去重）。
// 先于异步引导挂载：mock 装配/首帧渲染前的崩溃也有据可查（远程上报归 S3 批）。
installGlobalErrorCatch()

async function bootstrap() {
  await enableMockIfDev()
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  )
}
void bootstrap()
