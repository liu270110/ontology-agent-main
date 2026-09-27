# ontology-agent 桌面端 Electron 壳（v1）

> 决策记录 **FE-ADR-FE9**（2026-09-28）：桌面壳选型 **Electron**，排除 Tauri——本机（Windows 10 x64）无 Rust 工具链（`cargo` 不存在），Tauri 依赖本地 Rust 编译无法交付；Electron 零工具链依赖、`npmmirror` 镜像可直接装二进制。代价是体积与内存（安装包 ~100MB 级），换来 v1 交付确定性。后续若引入 Rust 工具链可重评估（见「v2 遗留」）。

## 定位：与 Web 的关系

**同一 dist 构建物，壳只负责宿主。** 桌面端不 fork 任何业务代码：

- Web 权威构建产物 = `frontend/dist`（`cd frontend && npm run build`）；
- 桌面壳 = 窗口 + 自定义协议 + 托盘 + 安全基线的宿主程序，运行时加载该 dist；
- 打包分发时经 electron-builder `extraResources` 把 `frontend/dist` 随壳带入 `resources/webapp`；
- API 端点：桌面分发构建时以 `VITE_API_BASE=http://localhost:8000/api/v1`（本地网关）构建 Web 产物——用 desktop 内的 `npm run build:web`（远程网关后续设置面板，v1 占位不实现）。

架构依据：03 篇 §2.6（窗口 1440×900 / 最小 1024×680）；BrowserRouter 依赖 history API，`file://` 下不可用，**自定义协议（standard+secure）是标准解法**。

## 目录

```
desktop/
├── package.json            # 独立 package（不污染 Web 的 package.json）
├── tsconfig.json           # 仅类型检查（esbuild 负责 transpile）
├── electron-builder.yml    # appId com.ontology.agent / nsis 目标 / extraResources: ../dist → webapp
├── electron/
│   ├── main.ts             # 主进程：协议注册、窗口、托盘、IPC、QA 截图钩子
│   ├── preload.ts          # contextBridge 最小桥（appInfo / setWindowTitle）
│   ├── types.d.ts          # 全局 ambient 类型（window.oaDesktop）
│   └── assets/             # icon.png / tray.png（node tools/make-icon.mjs 生成）
├── tools/make-icon.mjs     # 零依赖图标生成（纯 Node zlib 手写 PNG/ICO 容器）
├── build/                  # electron-builder buildResources（icon.ico / icon.png）
└── dist-electron/          # esbuild 产物（gitignore）
```

## 命令

前置：Web 产物存在（`cd frontend && npm run build`，或 `npm run build:web` 得到含桌面 API 端点的产物）。

| 命令 | 作用 |
| ---- | ---- |
| `npm install` | 安装依赖（Electron 二进制慢时：`ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`） |
| `npm run build` | esbuild 编译 main/preload → `dist-electron/`（bundle、cjs、png 内联 dataurl） |
| `npm run typecheck` | tsc --noEmit（注意：npm 解析到 TypeScript 7，`moduleResolution` 已用 `bundler`） |
| `npm start` | **生产/协议模式**：加载 `oa://app/`（映射 `frontend/dist`，SPA 路由回退 index.html） |
| `npm run dev` | **开发模式**：`ELECTRON_START_URL=http://localhost:5173` 连 Vite dev server（先在 frontend 起 `npm run dev`；Vite 端口被占漂移时按实际端口覆盖 `ELECTRON_START_URL`） |
| `npm run dir` | electron-builder `--dir` 未打包产物 → `release/win-unpacked/` |
| `npm run icons` | 重新生成图标资产 |

### QA 截图钩子（env 触发，默认关闭）

```bash
DESKTOP_SHOT=/path/shot.png npx electron .        # ready 后 3s 自动 capturePage 存 PNG，随后退出
DESKTOP_SHOT_DELAY=10000 ...                      # 冷启动慢时调大延迟
DESKTOP_SHOT_QUIT=0 ...                           # 截图后不退出
OA_DEBUG=1 ...                                    # 渲染层 console / did-fail-load 转发到 stdout
```

## 安全基线（强制）

- `contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`；
- preload 经 `contextBridge` 仅暴露 `oaDesktop.appInfo()`（版本/平台/模式，只读）与 `oaDesktop.setWindowTitle()`（主进程侧长度校验）；
- 自定义协议 `oa` 注册 `standard+secure+supportFetchAPI+stream`；`protocol.handle` 做目录穿越防护（resolve 后必须落在 dist 内），响应补 `Access-Control-Allow-Origin`（dist 资源引用带 crossorigin）；
- `setWindowOpenHandler` 拒绝新窗口（http(s) 交系统浏览器）；`will-navigate` 拦截跨源导航；
- MCP annotations 不作授权依据；后续加 IPC 能力一律走主进程白名单（不暴露 ipcRenderer 原语）。

## 已验证（2026-09-28，本机真跑）

| 模式 | 结果 | 证据 |
| ---- | ---- | ---- |
| 协议模式（`oa://app` → dist） | 窗口正常渲染（登录页，深色主题随系统） | `.qa_ix/desktop/desktop-window.png`（2854×1730，154KB） |
| dev 模式（Vite dev server + HMR 连接） | 应用挂载渲染 | `.qa_ix/desktop/desktop-window-dev.png`（142KB） |
| 打包 `--dir`（`resources/webapp`） | 窗口正常渲染 | `.qa_ix/desktop/desktop-window-packaged.png`（154KB） |
| typecheck（tsc）/ esbuild build | 通过 | — |

## 已知限制（v1 如实记录）

1. **MSW dev mock 在 Electron 内不工作**：`worker.start()` 的 SW 注册与激活均成功，但 msw 握手 promise 不返回 → React 不挂载（纯浏览器 dev 正常）。桌面 dev 请直连真实后端：起 Vite 时 `VITE_ENABLE_MOCK=0`；mock 优先的开发流留在浏览器。根因在 msw×Electron 互操作，非壳缺陷，v2 可深挖。
2. **`npm run dir` 在本机需手工收尾**：本机 E: 盘**目录 rename 被系统策略禁止**（空目录 rename 一律 EPERM，非 electron-builder 问题），builder 在解压后 rename `win-unpacked.tmp` 一步失败；变通：`cp -r win-unpacked.tmp win-unpacked` 后手工装配 `resources/app`（package.json + dist-electron）与 `resources/webapp`（../dist），已验证可运行。常规机器不受影响。--dir 下 exe 未做 rcedit 改名/图标（nsis 打包阶段才处理）。
3. **后端 CORS**：渲染源为 `oa://app`，直连网关需 FastAPI CORS 允许该 Origin（当前 dist 为 Web 默认相对路径 `/api/v1`，桌面分发构建用 `build:web` 烘焙 `http://localhost:8000/api/v1`）。
4. 托盘为 v1 占位实现：图标 + 显示/退出菜单 + 关闭隐藏；无闪烁/单击预览等增强。

## v2 遗留

- 自动更新（electron-updater + 私有源/镜像策略）；
- 托盘完整化（关闭最小化记忆、消息闪烁、双击行为设置）；
- 远程网关设置面板（替代构建期 `VITE_API_BASE`）+ 多环境配置文件；
- NSIS 安装包签名（代码签名证书）与 rcedit 产物元数据；
- 崩溃/渲染异常上报（对接平台审计与 trace_id 链路）；
- Rust 工具链可用后重评估 Tauri（FE-ADR-FE9 复审条件）；
- MSW×Electron 握手问题根修（或平台后端就绪后弃用 mock）。
