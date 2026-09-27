/**
 * ontology-agent 桌面壳 — 主进程（FE-ADR-FE9：Electron 壳，宿主 Web 同源 dist 构建物）。
 *
 * 加载策略：
 *   dev  ：ELECTRON_START_URL=http://localhost:5173 直连 Vite dev server（复用 Web dev 流程）
 *   prod ：自定义协议 oa://app（standard+secure+supportFetchAPI+stream）映射 frontend/dist 静态文件，
 *          无扩展名路径回退 index.html —— BrowserRouter 依赖 history API，file:// 下不可用，
 *          custom protocol 是标准解法（见 desktop/README.md FE-ADR-FE9）。
 *
 * 安全基线：contextIsolation: true / nodeIntegration: false / sandbox: true；
 *          preload 经 contextBridge 只暴露 appInfo / setWindowTitle 两个最小能力。
 *
 * 环境变量：
 *   ELECTRON_START_URL  dev 模式起始 URL（设置后优先于 oa:// 协议）
 *   OA_WEB_DIST         覆盖 Web dist 目录（默认 dev: ../../dist；打包: resources/webapp）
 *   DESKTOP_SHOT        设置后窗口 ready-to-show 3s 自动 capturePage 存 PNG（QA 验收），完成后退出
 *   DESKTOP_SHOT_DELAY  截图前延迟毫秒数（默认 3000；dev 冷启动 Vite 转换慢时可调大）
 *   DESKTOP_SHOT_QUIT   =0 时截图后不退出（默认退出，供无人值守验收）
 */
import { app, BrowserWindow, ipcMain, Menu, nativeImage, nativeTheme, net, protocol, shell, Tray } from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import iconPng from './assets/icon.png'
import trayPng from './assets/tray.png'

const SCHEME = 'oa'
const HOST = 'app'
const APP_TITLE = 'ontology-agent'

/** 自定义协议特权：必须在 app ready 前注册。 */
protocol.registerSchemesAsPrivileged([
  {
    scheme: SCHEME,
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true, corsEnabled: true },
  },
])

/**
 * Web dist 根目录解析（优先级）：
 *   1. OA_WEB_DIST 环境变量（显式覆盖，QA 用）
 *   2. 打包布局 resources/webapp（electron-builder extraResources；同时探测布局而非仅依赖
 *      app.isPackaged —— --dir 未重命名的 electron.exe 下 isPackaged 为 false）
 *   3. 开发态 frontend/dist（desktop/dist-electron 的上两级）
 */
function resolveWebDist(): string {
  if (process.env.OA_WEB_DIST) return path.resolve(process.env.OA_WEB_DIST)
  const packedWebapp = path.resolve(__dirname, '..', '..', 'webapp')
  if (fs.existsSync(path.join(packedWebapp, 'index.html'))) return packedWebapp
  return path.resolve(__dirname, '..', '..', 'dist')
}
const WEB_DIST = resolveWebDist()

let mainWindow: BrowserWindow | null = null
let tray: Tray | null = null
let trayReady = false
let isQuitting = false

/** oa://app/<path> → dist 内绝对路径；含穿越防护与 index.html 回退语义。 */
function urlToFilePath(rawUrl: string): { file: string; isFallback: boolean } | null {
  let u: URL
  try {
    u = new URL(rawUrl)
  } catch {
    return null
  }
  if (u.protocol !== `${SCHEME}:` || u.host !== HOST) return null
  let pathname: string
  try {
    pathname = decodeURIComponent(u.pathname)
  } catch {
    return null
  }
  if (pathname === '/' || pathname === '') return { file: path.join(WEB_DIST, 'index.html'), isFallback: false }
  const resolved = path.normalize(path.join(WEB_DIST, pathname))
  if (resolved !== WEB_DIST && !resolved.startsWith(WEB_DIST + path.sep)) return null // 目录穿越防护
  const isFallback = !path.extname(resolved) // 无扩展名 → SPA 路由，回退 index.html
  return { file: isFallback ? path.join(WEB_DIST, 'index.html') : resolved, isFallback }
}

/** 注册 oa:// 协议处理器：文件命中直接回源；未命中且无扩展名回退 index.html（其余 404）。 */
function registerAppProtocol(): void {
  protocol.handle(SCHEME, async request => {
    const target = urlToFilePath(request.url)
    if (!target) return new Response('bad request', { status: 400 })
    const exists = target.isFallback || (fs.existsSync(target.file) && fs.statSync(target.file).isFile())
    if (!exists) return new Response('not found', { status: 404 })
    // dist/index.html 的资源引用带 crossorigin（CORS 模式），自定义协议响应需带 ACAO，否则模块脚本被拦
    const upstream = await net.fetch(pathToFileURL(target.file).toString())
    const headers = new Headers(upstream.headers)
    headers.set('access-control-allow-origin', '*')
    return new Response(upstream.body, { status: upstream.status, headers })
  })
}

/** 外链白名单：应用自身（oa://app / dev server），其余导航拦截，http(s) 交系统浏览器。 */
function isAllowedUrl(url: string): boolean {
  try {
    const u = new URL(url)
    if (u.protocol === `${SCHEME}:` && u.host === HOST) return true
    const start = process.env.ELECTRON_START_URL
    return !!(start && url.startsWith(start))
  } catch {
    return false
  }
}

function showMainWindow(): void {
  if (!mainWindow) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
}

function createMainWindow(): BrowserWindow {
  // 深色底防白闪：backgroundColor 跟随系统深浅色（03 篇窗口规格 1440×900 / 最小 1024×680）
  const dark = nativeTheme.shouldUseDarkColors
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    title: APP_TITLE,
    backgroundColor: dark ? '#0b0f14' : '#f8fafc',
    show: false,
    autoHideMenuBar: true,
    icon: nativeImage.createFromDataURL(iconPng),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  })

  win.once('ready-to-show', () => showMainWindow())

  // QA/诊断：OA_DEBUG=1 时把渲染层 console 与加载失败转发到主进程 stdout
  if (process.env.OA_DEBUG) {
    win.webContents.on('console-message' as never, (...args: unknown[]) => {
      const detail = args[1] as Record<string, unknown> | null
      if (detail && typeof detail === 'object') {
        console.log(`[renderer] ${String(detail.message ?? '')} (${String(detail.sourceId ?? '')}:${String(detail.line ?? '')})`)
      } else {
        console.log(`[renderer:${String(args[1])}] ${String(args[2])} (${String(args[4])}:${String(args[3])})`)
      }
    })
    win.webContents.on('did-fail-load', (_event, code, desc, url) => {
      console.error(`[oa-desktop] did-fail-load ${code} ${desc} ${url}`)
    })
    win.webContents.on('render-process-gone', (_event, details) => {
      console.error('[oa-desktop] renderer gone:', details.reason)
    })
  }

  // 关闭 = 隐藏到托盘（托盘不可用时退化为正常关闭）
  win.on('close', event => {
    if (isQuitting || !trayReady) return
    event.preventDefault()
    win.hide()
  })
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null
  })

  // 安全：禁新窗口；拦截跨源导航
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) void shell.openExternal(url)
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (event, url) => {
    if (isAllowedUrl(url)) return
    event.preventDefault()
    if (/^https?:/i.test(url)) void shell.openExternal(url)
  })

  // QA 验收钩子：DESKTOP_SHOT 设置时 ready 后 3s 自动截图（默认截完退出；仅 env 触发，平时零开销）
  const shotPath = process.env.DESKTOP_SHOT
  if (shotPath) {
    win.webContents.once('did-finish-load', () => {
      setTimeout(() => {
        void (async () => {
          try {
            const image = await win.webContents.capturePage()
            const size = image.getSize()
            fs.mkdirSync(path.dirname(shotPath), { recursive: true })
            fs.writeFileSync(shotPath, image.toPNG())
            console.log(`[oa-desktop] screenshot saved: ${shotPath} (${size.width}x${size.height}, ${image.toPNG().length} bytes)`)
          } catch (err) {
            console.error('[oa-desktop] screenshot failed:', err)
          } finally {
            if (process.env.DESKTOP_SHOT_QUIT !== '0') app.quit()
          }
        })()
      }, Number(process.env.DESKTOP_SHOT_DELAY ?? 3000))
    })
  }

  const startUrl = process.env.ELECTRON_START_URL
  void (startUrl ? win.loadURL(startUrl) : win.loadURL(`${SCHEME}://${HOST}/`))
  return win
}

/** 托盘：图标 + 显示/退出菜单；失败时降级（关闭即退出，不驻留）。 */
function createTray(): void {
  try {
    const icon = nativeImage.createFromDataURL(trayPng)
    tray = new Tray(icon)
    tray.setToolTip(APP_TITLE)
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: '显示主窗口', click: () => showMainWindow() },
        { type: 'separator' },
        { label: '退出', click: () => app.quit() },
      ]),
    )
    tray.on('click', () => showMainWindow())
    trayReady = true
  } catch (err) {
    console.warn('[oa-desktop] tray unavailable, close will quit:', err)
    trayReady = false
  }
}

/** preload 桥接的主进程实现（能力面 = appInfo + setWindowTitle，v1 最小集）。 */
function registerIpc(): void {
  ipcMain.handle('oa:appInfo', () => ({
    name: APP_TITLE,
    version: app.getVersion(),
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
    platform: process.platform,
    arch: process.arch,
    mode: app.isPackaged ? ('production' as const) : ('development' as const),
  }))
  ipcMain.handle('oa:setWindowTitle', (_event, title: unknown) => {
    if (typeof title !== 'string' || title.length === 0 || title.length > 100) return false
    mainWindow?.setTitle(title)
    return true
  })
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => showMainWindow())

  app.whenReady().then(() => {
    Menu.setApplicationMenu(null)
    registerAppProtocol()
    registerIpc()
    mainWindow = createMainWindow()
    createTray()
  })

  app.on('before-quit', () => {
    isQuitting = true
  })

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit()
  })

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) mainWindow = createMainWindow()
    else showMainWindow()
  })
}
