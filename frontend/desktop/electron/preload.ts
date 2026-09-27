/**
 * ontology-agent 桌面壳 — preload（沙箱模式）。
 *
 * 安全基线：contextIsolation: true / nodeIntegration: false / sandbox: true。
 * 经 contextBridge 只暴露两个最小能力（渲染层无 Node 访问权）：
 *   appInfo()       → 版本/平台/运行模式（只读元信息）
 *   setWindowTitle() → 设置窗口标题（长度白名单校验在主进程侧）
 */
import { contextBridge, ipcRenderer } from 'electron'

const bridge = {
  appInfo: () => ipcRenderer.invoke('oa:appInfo'),
  setWindowTitle: (title: string) => ipcRenderer.invoke('oa:setWindowTitle', title),
} as const

contextBridge.exposeInMainWorld('oaDesktop', bridge)

export type OaDesktopBridge = typeof bridge
