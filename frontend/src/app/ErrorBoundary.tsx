import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { appendErrLog, readErrLog } from '@/lib/errlog'

interface Props {
  children: ReactNode
  /** 兜底档位（P-004 路由级边界）：root=整站兜底（App 根，整屏居中）；
   *  route=路由区兜底（壳内 lazy 页面包裹层，错误只占当前路由区——侧栏/导航仍可用，
   *  动作钮为「重载本页」）。默认 root，既有行为不变。 */
  variant?: 'root' | 'route'
  /** 复位键（P-004 跨路由复位）：任一键变化（Object.is 逐位比较）即清 error 态重渲 children。
   *  路由边界传 [pathname]——react-router v6 Outlet 同位渲染无 key，切路由时 RouteGuard→
   *  ErrorBoundary 同型实例被 React 保留，state.error 不复位会导致「一处崩溃处处兜底」
   *  （实测 /settings 崩后切 /kb 仍显兜底直至整页重载）；键变后回崩路由则自然再捕获。 */
  resetKeys?: unknown[]
}

interface State {
  error: Error | null
  /** 技术详情折叠态（受控折叠：堆栈默认不出 DOM——p-empty-skel 红线「禁止把堆栈抛给用户」） */
  showTech: boolean
}

/** 错误边界（16 篇 §1 providers / §7 错误态统一；39 号对账 G-S2 画板 p-status/p-empty-skel）：
 *  兜底渲染崩溃，默认只给中文摘要 + 幂等重试提示 + 一键重载；堆栈收进「查看技术详情」
 *  受控折叠（默认收起，面向开发者排障保留，与画板 p-status 500 格文案口径一致）。
 *  P-004：variant="route" 轻量变体挂 App.tsx 每条 lazy 路由 element——任一页渲染崩只塌
 *  路由区，壳（侧栏/导航）仍在；根部 root 兜底保留（providers/壳层崩溃仍整站兜住）。
 *  接入监控上报时在此取 error 与 componentStack（锚点 §6.7 可观测：上报需带 trace_id，
 *  随平台可观测件接入补；本地环形先经 lib/errlog 共享写入）。 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, showTech: false }

  static getDerivedStateFromError(error: Error): State {
    return { error, showTech: false }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // TODO(监控): error + info.componentStack 上报（含 trace_id）
    console.error('[ErrorBoundary]', error, info.componentStack)
    // 偶发崩溃取证：本地环形留存最近 5 次（含组件栈与路径），供「查看技术详情」与事后定位
    appendErrLog({
      at: new Date().toISOString(),
      path: window.location.pathname,
      message: String(error?.message ?? error),
      stack: String(error?.stack ?? '').slice(0, 2000),
      componentStack: String(info.componentStack ?? '').slice(0, 1200),
    })
  }

  componentDidUpdate(prevProps: Props) {
    // 跨路由复位：resetKeys 任一键变了且正处兜底态 → 清 error 重试渲染新 children
    // （对齐 react-error-boundary 的 resetKeys 语义；未崩时无状态可清，零额外行为）
    if (
      this.state.error &&
      this.props.resetKeys &&
      prevProps.resetKeys &&
      (this.props.resetKeys.length !== prevProps.resetKeys.length ||
        this.props.resetKeys.some((k, i) => !Object.is(k, prevProps.resetKeys![i])))
    ) {
      this.setState({ error: null, showTech: false })
    }
  }

  render() {
    if (this.state.error) {
      const err = this.state.error
      const isRoute = this.props.variant === 'route'
      return (
        <div
          role="alert"
          className={
            isRoute
              ? // 路由区兜底：占满壳内容区（不整屏），侧栏/导航在边界树外不受影响
                'flex h-full min-h-64 flex-col items-center justify-center gap-3 p-8 text-label'
              : 'flex min-h-screen flex-col items-center justify-center gap-3 bg-bg p-8 text-label'
          }
        >
          <h1 className="text-lg font-bold">页面出现异常</h1>
          <p className="max-w-md text-center text-xs text-label-2">
            渲染过程发生未预期错误，您的数据不受影响。工程师已被通知 · 重试是幂等安全的；若反复出现，请联系管理员并说明发生时间与操作步骤。
          </p>
          {/* 技术详情（错误堆栈）默认收起：受控折叠按钮 aria-expanded 可达，堆栈仅展开后渲染 */}
          <button
            type="button"
            data-testid="error-tech-toggle"
            aria-expanded={this.state.showTech}
            className="btn btn-g btn-sm"
            onClick={() => this.setState(s => ({ showTech: !s.showTech }))}
          >
            {this.state.showTech ? '收起技术详情' : '查看技术详情'}
          </button>
          {this.state.showTech && (
            <>
              <pre
                data-testid="error-stack"
                className="scroll-thin max-h-48 max-w-md overflow-auto whitespace-pre-wrap break-words rounded-lg border border-separator bg-surface p-3 text-[11px] text-label-3"
              >
                {err.stack ?? String(err)}
              </pre>
              {/* 历史取证（oa-errlog 环形）：偶发崩溃重载后堆栈仍在，供事后定位 */}
              {(() => {
                const recent = readErrLog().slice(1, 4)
                if (recent.length === 0) return null
                return (
                  <div className="max-w-md self-stretch text-left">
                    <div className="ctx-t mb-1">近期崩溃记录（本地留存）</div>
                    {recent.map((e, i) => (
                      <div key={i} className="mono truncate text-[10px] text-label-3" title={`${e.at} ${e.path}`}>
                        {e.at.slice(11, 19)} {e.path} · {e.message.slice(0, 60)}
                      </div>
                    ))}
                  </div>
                )
              })()}
            </>
          )}
          <button
            type="button"
            className="btn btn-p rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white"
            onClick={() => window.location.reload()}
          >
            {isRoute ? '重载本页' : '重新加载'}
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
