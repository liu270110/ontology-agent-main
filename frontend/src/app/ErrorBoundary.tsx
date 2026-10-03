import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
  /** 技术详情折叠态（受控折叠：堆栈默认不出 DOM——p-empty-skel 红线「禁止把堆栈抛给用户」） */
  showTech: boolean
}

/** 全局错误边界（16 篇 §1 providers / §7 错误态统一；39 号对账 G-S2 画板 p-status/p-empty-skel）：
 *  兜底渲染崩溃，默认只给中文摘要 + 幂等重试提示 + 一键重载；堆栈收进「查看技术详情」
 *  受控折叠（默认收起，面向开发者排障保留，与画板 p-status 500 格文案口径一致）。
 *  接入监控上报时在此取 error 与 componentStack（锚点 §6.7 可观测：上报需带 trace_id，
 *  随平台可观测件接入补）。 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, showTech: false }

  static getDerivedStateFromError(error: Error): State {
    return { error, showTech: false }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // TODO(监控): error + info.componentStack 上报（含 trace_id）
    console.error('[ErrorBoundary]', error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      const err = this.state.error
      return (
        <div
          role="alert"
          className="flex min-h-screen flex-col items-center justify-center gap-3 bg-bg p-8 text-label"
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
            <pre
              data-testid="error-stack"
              className="scroll-thin max-h-48 max-w-md overflow-auto whitespace-pre-wrap break-words rounded-lg border border-separator bg-surface p-3 text-[11px] text-label-3"
            >
              {err.stack ?? String(err)}
            </pre>
          )}
          <button
            type="button"
            className="btn btn-p rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white"
            onClick={() => window.location.reload()}
          >
            重新加载
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
