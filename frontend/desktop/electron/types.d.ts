/**
 * 桌面壳全局类型（纯 ambient .d.ts，无 import/export —— 保持全局作用域生效）。
 */

/** esbuild dataurl loader 内联的图片导入（main.ts 用） */
declare module '*.png' {
  const src: string
  export default src
}

/** 主进程 appInfo 返回契约（preload bridge 的 invoke 结果） */
interface OaDesktopAppInfo {
  name: string
  version: string
  electron: string
  chrome: string
  node: string
  platform: string
  arch: string
  mode: 'development' | 'production'
}

/** 渲染层可用的桌面壳桥（window.oaDesktop，纯 Web 环境下为 undefined） */
interface OaDesktopBridge {
  appInfo(): Promise<OaDesktopAppInfo>
  setWindowTitle(title: string): Promise<boolean>
  /** 打开/聚焦管理控制台独立窗口（双区 IA；Web 环境无此能力，调用方需降级为路由跳转） */
  openConsole(): Promise<boolean>
}

interface Window {
  oaDesktop?: OaDesktopBridge
}
