import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/** 全局错误边界（16 篇 §1 providers / §7 错误态统一）：兜底渲染崩溃，
 *  中文提示 + 一键重载；接入监控上报时在此取 error 与 componentStack
 *  （锚点 §6.7 可观测：上报需带 trace_id，随平台可观测件接入补）。 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // TODO(监控): error + info.componentStack 上报（含 trace_id）
    console.error('[ErrorBoundary]', error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <div
          role="alert"
          className="flex min-h-screen flex-col items-center justify-center gap-3 bg-bg p-8 text-label"
        >
          <h1 className="text-lg font-bold">页面出现异常</h1>
          <p className="max-w-md text-center text-xs text-label-2">
            渲染过程发生未预期错误，您的数据不受影响。可尝试重新加载；若反复出现，请联系管理员并说明发生时间与操作步骤。
          </p>
          <pre className="scroll-thin max-h-32 max-w-md overflow-auto rounded-lg border border-separator bg-surface p-3 text-[11px] text-label-3">
            {String(this.state.error?.message ?? this.state.error)}
          </pre>
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
