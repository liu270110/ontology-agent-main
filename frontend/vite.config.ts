import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { readFileSync } from 'node:fs'
import path from 'node:path'

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
  server: { port: 5173 },
  build: {
    // S8 性能预算（22 篇 §4/30 篇 §10）：重库拆 chunk 懒加载；react 系留主包（拆分致 default export 互操作断裂，实测 useState undefined）
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes('node_modules')) {
            if (id.includes('echarts')) return 'vendor-echarts'
            if (id.includes('@codemirror') || id.includes('lezer')) return 'vendor-codemirror'
            if (id.includes('@xyflow')) return 'vendor-xyflow'
            if (id.includes('@rjsf') || id.includes('ajv')) return 'vendor-rjsf'
            if (id.includes('@headless-tree')) return 'vendor-tree'
            if (id.includes('react-markdown') || id.includes('remark') || id.includes('rehype') || id.includes('unified') || id.includes('shiki') || id.includes('highlight.js')) return 'vendor-markdown'
          }
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
  },
})
