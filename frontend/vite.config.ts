import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'

// deps 预打包缓存按项目绝对路径隔离（2026-10-04）：舰队 worktree 经 junction 共享主仓
// node_modules——多实例 config 不同（proxy/env）互踢同一 .vite/deps，后启动者使先启动者
// 的依赖 504 Outdated Optimize Dep（/kb 白屏实锤）。按路径哈希分目录后各实例缓存互不干扰。
const CACHE_KEY = crypto.createHash('md5').update(path.resolve(__dirname)).digest('hex').slice(0, 8)

// 版本单源=package.json（桌面端 package.json 同步维护，见 CHANGELOG §版本纪律）；
// __BUILD_TIME__ 取构建时刻，设置·关于卡展示（桌面/Web 同源对照）。
const pkg = JSON.parse(readFileSync(path.resolve(__dirname, 'package.json'), 'utf-8')) as { version: string }

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
    __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
  },
  cacheDir: `node_modules/.vite-${CACHE_KEY}`,
  server: {
    port: 5173,
    // live 模式（VITE_ENABLE_MOCK=0）dev 代理：/api → 真实网关（2026-10-04 mock 退役，
    // 40 号文档）。SSE（/sessions/{id}/events、/tasks/{id}/events）经 http-proxy 流式透传。
    proxy: {
      '/api': { target: process.env.OA_GATEWAY_ORIGIN ?? 'http://localhost:8364', changeOrigin: true },
    },
  },
  build: {
    // S8 性能预算（22 篇 §4/30 篇 §10）：路由级 lazy（App.tsx）已让重库全部落在异步 chunk，
    // 交给 rollup 自然分包——2026-10-04 实测（docs/frontend/06）曾用 manualChunks 按
    // node_modules 子串分 vendor 组，结果 react/react-dom 作为 vendor 组模块的共享依赖被
    // rollup 吞进 vendor-markdown/vendor-xyflow，入口被迫静态加载 311KB xyflow+162KB
    // markdown（首屏 JS 230KB gzip）。移除后 react 系自然留主包（勿再拆 react vendor：
    // 拆分致 default export 互操作断裂，实测 useState undefined），重库各自成异步共享
    // chunk（xyflow+d3/codemirror/rjsf/markdown），首屏 JS 127KB gzip（-45%）。
  },
  test: {
    environment: 'jsdom',
    globals: true,
    // 懒加载路由 chunk + query 首拉链路（S8）：单测默认 5s 不够
    testTimeout: 15000,
    // jsdom 相对 URL fetch 需 MSW node 拦截（S1 回归：缺 setup 后 login 请求抛 Invalid URL）
    setupFiles: ['./src/mocks/node-setup.ts'],
  },
})
