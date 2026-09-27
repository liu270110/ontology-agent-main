import { useEffect, useRef, useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  FileText,
  Folder,
  FolderOpen,
  Paperclip,
  Terminal,
  Timer,
} from 'lucide-react'
import { Sheet } from '@/components/sheet'
import {
  fmtSize,
  workspaceApi,
  type WsNode,
  type WsResource,
  type WsTree,
} from '../workspace'

/** Agent 工作区面板（画框23 / 31 篇）：文件树 + 终端 + 资源三页签，与上下文面板
 *  共占右栏 240 槽位（ChatPage 页签切换）。文件树=沙箱 /workspace 只读物（点击→预览
 *  Sheet）；终端=受限 exec 回放（白名单命令，输出实时追加）；资源=会话四分组
 *  （附件橙/产物青/本体快照蓝/导出绿，--src-* 着色，30 篇）。
 *  M1 范围：浏览 + 预览 + 终端执行。行级 hover 动作（下载/@引用插入消息）随 M4
 *  download-url 端点一并接（避免 M1 死入口）。 */

type WsTab = 'tree' | 'term' | 'res'

interface TermLine {
  kind: 'cmd' | 'out'
  text: string
}

const TABS: { key: WsTab; label: string; icon: typeof Folder }[] = [
  { key: 'tree', label: '文件树', icon: Folder },
  { key: 'term', label: '终端', icon: Terminal },
  { key: 'res', label: '资源', icon: Paperclip },
]

/** 资源四分组（30 篇：--src-* 来源着色） */
const RES_GROUPS: { type: WsResource['type']; title: string; dot: string }[] = [
  { type: 'attachment', title: '附件', dot: 'bg-orange' },
  { type: 'artifact', title: 'Agent 产物', dot: 'bg-teal' },
  { type: 'ontology_snapshot', title: '本体快照', dot: 'bg-accent' },
  { type: 'export', title: '导出', dot: 'bg-green' },
]

const RES_STATUS: Record<WsResource['status'], { text: string; cls: string }> = {
  ready: { text: '就绪', cls: 'b-green' },
  processing: { text: '处理中', cls: 'b-orange' },
  error: { text: '失败', cls: 'b-red' },
}

/** 文件树节点递归渲染：目录可折叠（teal），文件点击预览；dirty 橙点=Agent 修改未归档 */
function TreeNode({
  node,
  depth,
  expanded,
  pickedPath,
  onToggle,
  onPick,
}: {
  node: WsNode
  depth: number
  expanded: Set<string>
  pickedPath: string | null
  onToggle: (p: string) => void
  onPick: (n: WsNode) => void
}) {
  const isDir = node.type === 'dir'
  const open = expanded.has(node.path)
  const selected = pickedPath === node.path
  return (
    <div>
      <button
        type="button"
        data-testid={`ws-node-${node.name}`}
        onClick={() => (isDir ? onToggle(node.path) : onPick(node))}
        className={`flex w-full items-center gap-1 rounded-lg py-[3px] pr-1.5 text-left text-[11.5px] leading-5 hover:bg-surface-2 ${
          selected ? 'bg-accent-soft' : ''
        }`}
        style={{ paddingLeft: 6 + depth * 12 }}
      >
        {isDir ? (
          <>
            {open ? (
              <ChevronDown size={11} className="flex-none text-label-3" aria-hidden />
            ) : (
              <ChevronRight size={11} className="flex-none text-label-3" aria-hidden />
            )}
            {open ? (
              <Folder size={12} className="flex-none" style={{ color: 'var(--teal)' }} aria-hidden />
            ) : (
              <Folder size={12} className="flex-none text-label-2" aria-hidden />
            )}
            <span className={open ? 'font-semibold' : ''} style={open ? { color: 'var(--teal)' } : undefined}>
              {node.name}/
            </span>
          </>
        ) : (
          <>
            <span className="w-[11px] flex-none" aria-hidden />
            <FileText size={12} className="flex-none text-label-2" aria-hidden />
            <span className={`truncate ${selected ? 'font-semibold text-accent' : ''}`}>{node.name}</span>
            {node.dirty && <span className="h-1.5 w-1.5 flex-none rounded-full bg-orange" aria-label="有改动" />}
            <span className="mono ml-auto flex-none pl-1.5 text-[10px] text-label-3">
              {node.dirty ? node.updated_at : fmtSize(node.size ?? 0)}
            </span>
          </>
        )}
      </button>
      {isDir && open && (
        <div>
          {node.children?.length ? (
            node.children.map(c => (
              <TreeNode
                key={c.path}
                node={c}
                depth={depth + 1}
                expanded={expanded}
                pickedPath={pickedPath}
                onToggle={onToggle}
                onPick={onPick}
              />
            ))
          ) : (
            <div className="py-0.5 text-[10.5px] text-label-3" style={{ paddingLeft: 6 + (depth + 1) * 12 + 22 }}>
              （空目录）
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function collectDirs(node: WsNode, acc: string[] = []): string[] {
  if (node.type === 'dir') {
    acc.push(node.path)
    node.children?.forEach(c => collectDirs(c, acc))
  }
  return acc
}

export function WorkspacePanel({ sessionId }: { sessionId: string }) {
  const [tab, setTab] = useState<WsTab>('tree')
  const [tree, setTree] = useState<WsTree | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [preview, setPreview] = useState<{ path: string; content: string; language: string } | null>(null)
  const [resources, setResources] = useState<WsResource[] | null>(null)
  const [termLines, setTermLines] = useState<TermLine[]>([
    { kind: 'cmd', text: 'ls /workspace/artifacts/' },
    { kind: 'out', text: '排查报告草稿 v0.1.md' },
    { kind: 'out', text: '台账数据.json' },
  ])
  const [cmd, setCmd] = useState('')
  const [execBusy, setExecBusy] = useState(false)
  const termEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    void workspaceApi.tree(sessionId).then(t => {
      if (!alive) return
      setTree(t)
      setExpanded(new Set(collectDirs(t.root)))
    })
    void workspaceApi.resources(sessionId).then(r => {
      if (alive) setResources(r.items)
    })
    return () => {
      alive = false
    }
  }, [sessionId])

  useEffect(() => {
    // ?.() 兼容 jsdom（无 scrollIntoView 实现）
    termEndRef.current?.scrollIntoView?.({ block: 'end' })
  }, [termLines, tab])

  async function pick(n: WsNode) {
    try {
      setPreview(await workspaceApi.file(sessionId, n.path))
    } catch {
      setPreview({ path: n.path, content: '（读取失败：文件可能已被工作区回收）', language: 'text' })
    }
  }

  async function runCmd() {
    const c = cmd.trim()
    if (!c || execBusy) return
    setCmd('')
    setExecBusy(true)
    setTermLines(v => [...v, { kind: 'cmd', text: c }])
    try {
      const r = await workspaceApi.exec(sessionId, c)
      setTermLines(v => [...v, ...r.lines.map(l => ({ kind: 'out' as const, text: l }))])
    } catch {
      setTermLines(v => [...v, { kind: 'out', text: 'exec: 沙箱通道不可用' }])
    } finally {
      setExecBusy(false)
    }
  }

  const recycle = tree?.recycle_in_minutes

  return (
    <aside data-testid="ws-panel" className="flex w-60 flex-none flex-col border-l border-separator bg-surface">
      <div className="flex items-center gap-1.5 px-3.5 pb-1 pt-3">
        <FolderOpen size={13} className="text-teal" aria-hidden />
        <b className="text-[12.5px]">Agent 工作区</b>
        {recycle != null && (
          <span
            data-testid="ws-recycle"
            className="badge ml-auto gap-1 text-[10px]"
            style={recycle <= 30 ? { background: 'var(--orange-soft)', color: 'var(--orange)' } : undefined}
            title="工作区易失：会话关闭后进入休眠，到期快照归档并销毁（20 篇 SBX）"
          >
            <Timer size={10} aria-hidden />
            {recycle} 分钟后回收
          </span>
        )}
      </div>

      <div className="px-3.5 pt-1.5">
        <div className="seg w-full" role="tablist" aria-label="工作区视图">
          {TABS.map(t => (
            <button
              key={t.key}
              type="button"
              role="tab"
              data-testid={`ws-tab-${t.key}`}
              aria-selected={tab === t.key}
              className={`seg-btn flex-1 whitespace-nowrap ${tab === t.key ? 'on' : ''}`}
              onClick={() => setTab(t.key)}
            >
              <t.icon size={11} className="mr-0.5 inline align-[-1px]" aria-hidden />
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'tree' && (
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2.5 py-2">
          <div className="mono px-1.5 pb-1 text-[10.5px] text-label-3">/workspace/</div>
          {tree ? (
            tree.root.children?.map(c => (
              <TreeNode
                key={c.path}
                node={c}
                depth={0}
                expanded={expanded}
                pickedPath={preview?.path ?? null}
                onToggle={p =>
                  setExpanded(s => {
                    const n = new Set(s)
                    if (n.has(p)) n.delete(p)
                    else n.add(p)
                    return n
                  })
                }
                onPick={n => void pick(n)}
              />
            ))
          ) : (
            <div className="px-1.5 py-3 text-[11.5px] text-label-3">正在读取沙箱文件树…</div>
          )}
        </div>
      )}

      {tab === 'term' && (
        <div className="flex min-h-0 flex-1 flex-col px-3.5 pb-3 pt-2">
          <div
            data-testid="ws-term"
            className="scroll-thin min-h-0 flex-1 overflow-y-auto rounded-xl border border-separator bg-surface-2 p-2.5 font-mono text-[11px] leading-[1.7]"
          >
            {termLines.map((l, i) => (
              <div key={i} className={l.kind === 'cmd' ? '' : 'text-label-2'} style={l.kind === 'cmd' ? { color: 'var(--green)' } : undefined}>
                {l.kind === 'cmd' ? `$ ${l.text}` : l.text}
              </div>
            ))}
            <div className="flex items-center gap-1.5" style={{ color: 'var(--green)' }}>
              <span aria-hidden>$</span>
              <input
                data-testid="ws-term-input"
                value={cmd}
                onChange={e => setCmd(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') void runCmd()
                }}
                disabled={execBusy}
                aria-label="终端命令"
                title="受限 shell：白名单 ls / pwd / cat / head / tail"
                className="w-full min-w-0 flex-1 border-none bg-transparent font-mono text-[11px] text-label outline-none"
                placeholder={execBusy ? '执行中…' : '输入命令…'}
              />
            </div>
            <div ref={termEndRef} />
          </div>
          <div className="pt-1 text-[10px] text-label-3">白名单命令：ls · pwd · cat · head · tail（其余走审批链）</div>
        </div>
      )}

      {tab === 'res' && (
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-3.5 pb-3 pt-2">
          {!resources ? (
            <div className="py-3 text-[11.5px] text-label-3">正在读取资源列表…</div>
          ) : (
            RES_GROUPS.map(g => {
              const items = resources.filter(r => r.type === g.type)
              return (
                <section key={g.type} className="mt-2 first:mt-0">
                  <div className="flex items-center gap-1.5 text-[11px] font-semibold text-label-3">
                    <span className={`h-1.5 w-1.5 rounded-full ${g.dot}`} aria-hidden />
                    {g.title}
                    <span className="font-normal">{items.length}</span>
                  </div>
                  {items.length === 0 ? (
                    <div className="py-0.5 pl-3 text-[10.5px] text-label-3">（无）</div>
                  ) : (
                    items.map(r => {
                      const st = RES_STATUS[r.status]
                      return (
                        <div
                          key={r.id}
                          data-testid={`ws-res-${r.id}`}
                          className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[11.5px] hover:bg-surface-2"
                          title={`${r.name} · ${fmtSize(r.size)} · 上传者 ${r.uploaded_by}`}
                        >
                          <FileText size={12} className="flex-none text-label-2" aria-hidden />
                          <span className="min-w-0 flex-1 truncate">{r.name}</span>
                          <span className="mono flex-none text-[10px] text-label-3">{fmtSize(r.size)}</span>
                          <span className={`badge flex-none text-[10px] ${st.cls}`}>{st.text}</span>
                        </div>
                      )
                    })
                  )}
                </section>
              )
            })
          )}
        </div>
      )}

      {/* 文件预览抽屉（31 篇：文本/Markdown mono 渲染；图片/PDF 随 M4 preview 端点） */}
      <Sheet open={!!preview} onClose={() => setPreview(null)} title={preview?.path ?? ''} width={520}>
        {preview && (
          <pre
            data-testid="ws-preview"
            className="scroll-thin mono m-0 max-h-[calc(100vh-160px)] overflow-auto whitespace-pre-wrap break-words rounded-xl border border-separator bg-surface-2 p-3.5 text-[11.5px] leading-[1.8] text-label"
          >
            {preview.content}
          </pre>
        )}
      </Sheet>
    </aside>
  )
}
