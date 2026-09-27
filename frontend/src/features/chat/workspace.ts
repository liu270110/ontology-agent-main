/** Agent 工作区数据层（31 篇 API 需求 → 画框23）。
 *  文件树=沙箱 /workspace daemon 代理只读物；终端=受限 exec.run 回放；
 *  资源=会话四分组（30 篇对象模型）。M1 走 MSW mock，M4 换真端点 +
 *  WS workspace.file.* / terminal.output 增量事件。 */

import { api } from '@/api/client'


export interface WsNode {
  name: string
  path: string
  type: 'dir' | 'file'
  size?: number
  updated_at?: string
  /** Agent 修改未入库 → 树上标脏点 */
  dirty?: boolean
  children?: WsNode[]
}

export interface WsTree {
  /** 休眠回收倒计时（分钟）：面板顶部提示（20 篇：会话关闭 30 分钟后回收） */
  recycle_in_minutes?: number
  root: WsNode
}

export interface WsFile {
  path: string
  content: string
  language: string
}

export interface WsExecResult {
  command: string
  exit_code: number
  lines: string[]
}

export type ResourceStatus = 'ready' | 'processing' | 'error'

export interface WsResource {
  id: string
  type: 'attachment' | 'artifact' | 'ontology_snapshot' | 'export'
  name: string
  size: number
  status: ResourceStatus
  uploaded_by: string
  created_at: string
  ocr_status?: string
  extract_status?: string
}

export const workspaceApi = {
  tree: (sid: string) => api.get<WsTree>(`/sessions/${sid}/workspace/tree`),
  file: (sid: string, path: string) =>
    api.get<WsFile>(`/sessions/${sid}/workspace/file?path=${encodeURIComponent(path)}`),
  exec: (sid: string, command: string) =>
    api.post<WsExecResult>(`/sessions/${sid}/terminal/exec`, { command }),
  resources: (sid: string) => api.get<{ items: WsResource[] }>(`/sessions/${sid}/resources`),
}

/** 字节数人性化（文件行元信息：840KB / 1.2KB） */
export function fmtSize(n: number): string {
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)}MB`
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(n >= 10 << 10 ? 0 : 1)}KB`
  return `${n}B`
}
