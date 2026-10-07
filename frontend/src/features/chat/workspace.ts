/** Agent 工作区数据层（31 篇 API 需求 → 画框23）。
 *  C3 live 对接（2026-10-07）：四端点后端已实装（services/agent/api/workspace.py）——
 *  信封=裸 DTO/{items}（apiFetch 无 code 字段视为裸数据放行，双形态兼容），字段与
 *  WsNode/WsTree/WsFile/WsExecResult/WsResource 名级一致；WS workspace.file.* /
 *  terminal.output 实时增量事件随沙箱批（现以 store 事件缓冲 + 300ms 防抖重拉兜底）。 */

import { api } from '@/api/client'


export interface WsNode {
  name: string
  path: string
  type: 'dir' | 'file'
  size?: number
  /** ISO8601（live 真端点；mock 相对时间文案已随 live 投影退役） */
  updated_at?: string
  /** Agent 修改未入库 → 树上标脏点（live v1 无改动追踪恒缺省，WS 批接入） */
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
  /** 0=成功；非零=命令自身退出码；124=超时终止（5s 上限）；126=执行失败；127=可执行缺失。
   *  白名单/参数越界不走本 DTO——4001 结构化拒绝（TERMINAL_CMD_NOT_ALLOWED）走 ApiError。 */
  exit_code: number
  lines: string[]
}

export type ResourceStatus = 'ready' | 'processing' | 'error'

export interface WsResource {
  /** live M1 顶层产出=ws-<sha256(name)[:12]>（名称派生稳定 id）；表行 id 随 artifacts 建表批 */
  id: string
  /** live M1 顶层扫描恒 'artifact'；attachment/ontology_snapshot/export 随 artifacts 表批扩展 */
  type: 'attachment' | 'artifact' | 'ontology_snapshot' | 'export'
  name: string
  size: number
  status: ResourceStatus
  /** live M1 恒 'sandbox'（宿主工作目录直读）；user/agent:<name> 随上传通道批 */
  uploaded_by: string
  created_at: string
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
