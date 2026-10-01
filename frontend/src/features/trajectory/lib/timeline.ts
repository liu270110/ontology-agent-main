import type { ChatMessage } from '@/stores/session-store'

/** 轨迹时间线组装口径（画框21 p-trajectory + 08 篇只追加事件流）：
 *  数据源 = GET /sessions/{id}/messages（历史基线，用户输入/助手回复行）
 *        + GET /sessions/{id}/events（SSE 帧 JSON 回放段，use-task-events 的 fetch 流解析模式）。
 *  两源按 seq 归并升序 = 模型看到的一切（只追加）；来源分类与 theme --src-* 着色同源：
 *  system=indigo / user=accent / assistant=green / tool=teal / retrieval=orange(context) /
 *  memory=purple(subagent)。分叉点标注 = regenerate / compact 类事件 + 第二次及以后的 RUN_STARTED
 *  （重新生成）。帧无时间戳载荷（api/02 帧未下发），时间按 seq 线性推演 HH:mm:ss 兜底展示。 */

export type TrajSource = 'system' | 'user' | 'assistant' | 'tool' | 'retrieval' | 'memory'

/** SSE 回放帧（GET /sessions/{id}/events 流解析产物） */
export interface TrajFrame {
  seq: number
  name: string
  data: Record<string, unknown>
}

export interface TrajItem {
  seq: number
  source: TrajSource
  /** 事件类型徽标（中文） */
  kind: string
  title: string
  detail?: string
  /** 工具调用入参（mono 块展开） */
  request?: string
  /** 工具调用出参（mono 块展开） */
  response?: string
  /** 分叉点标注（regenerate / compact 类事件） */
  fork?: string
  /** 展示时间 HH:mm:ss（帧无时间戳，按 seq 线性推演兜底） */
  time: string
}

/** 来源过滤 chips（画框21 来源过滤卡 + D2 切片口径：全部/工具调用/检索/记忆写入/系统行） */
export const TRAJ_SOURCES: { key: TrajSource; label: string }[] = [
  { key: 'tool', label: '工具调用' },
  { key: 'retrieval', label: '检索' },
  { key: 'memory', label: '记忆写入' },
  { key: 'system', label: '系统行' },
]

/** 来源着色（tokens.css --src-* 同源） */
export const TRAJ_SOURCE_COLOR: Record<TrajSource, string> = {
  system: 'var(--src-system)',
  user: 'var(--src-user)',
  assistant: 'var(--src-assistant)',
  tool: 'var(--src-tool)',
  retrieval: 'var(--src-context)',
  memory: 'var(--src-subagent)',
}

const cut = (s: string, n = 96) => (s.length > n ? `${s.slice(0, n)}…` : s)

const TRAJ_EPOCH = Date.now()
function timeFor(seq: number): string {
  const d = new Date(TRAJ_EPOCH + seq * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

type TrajItemBase = Omit<TrajItem, 'time'>

/** 历史消息 + SSE 回放帧 → 按 seq 归并的时间线（纯函数，页面 useMemo 消费） */
export function buildTimeline(messages: ChatMessage[], frames: TrajFrame[]): TrajItem[] {
  const extras: TrajItemBase[] = []
  const tools = new Map<string, { seq: number; tool: string; args: string; endSeq?: number; result?: { seq: number; ok: boolean; summary?: string; costMs?: number } }>()
  const liveMsgs = new Map<string, { seq: number; content: string; endSeq?: number }>()
  let runCount = 0

  for (const f of [...frames].sort((a, b) => a.seq - b.seq)) {
    const d = f.data
    switch (f.name) {
      case 'RUN_STARTED': {
        runCount += 1
        extras.push({
          seq: f.seq, source: 'system', kind: '系统',
          title: `开始运行（${String(d.run_id ?? '')}）`,
          // 分叉语义（08 篇）：第二次及以后的 RUN_STARTED = 重新生成
          ...(runCount > 1 ? { fork: '重新生成分叉' } : {}),
        })
        break
      }
      case 'RUN_FINISHED': {
        const usage = d.usage as { tokens?: number; cost?: number } | undefined
        extras.push({ seq: f.seq, source: 'system', kind: '系统', title: `运行完成${usage?.tokens ? ` · ${usage.tokens} tokens` : ''}` })
        break
      }
      case 'RUN_ERROR':
        extras.push({ seq: f.seq, source: 'system', kind: '系统', title: `运行失败 · ${String(d.message ?? '未知错误')}` })
        break
      case 'TOOL_CALL_START': {
        const id = String(d.tool_call_id ?? '')
        tools.set(id, { seq: f.seq, tool: String(d.tool_name ?? 'tool'), args: '' })
        break
      }
      case 'TOOL_CALL_ARGS': {
        const t = tools.get(String(d.tool_call_id ?? ''))
        if (t) t.args += String(d.delta ?? '')
        break
      }
      case 'TOOL_CALL_END': {
        const t = tools.get(String(d.tool_call_id ?? ''))
        if (t) t.endSeq = f.seq
        break
      }
      case 'TOOL_CALL_RESULT': {
        const t = tools.get(String(d.tool_call_id ?? ''))
        if (t) t.result = { seq: f.seq, ok: Boolean(d.ok), summary: d.summary != null ? String(d.summary) : undefined, costMs: typeof d.cost_ms === 'number' ? d.cost_ms : undefined }
        break
      }
      case 'TEXT_MESSAGE_START': {
        const id = String(d.message_id ?? '')
        if (!liveMsgs.has(id)) liveMsgs.set(id, { seq: f.seq, content: '' })
        break
      }
      case 'TEXT_MESSAGE_CONTENT': {
        const id = String(d.message_id ?? '')
        const m = liveMsgs.get(id) ?? { seq: f.seq, content: '' }
        m.content += String(d.delta ?? '')
        liveMsgs.set(id, m)
        break
      }
      case 'TEXT_MESSAGE_END': {
        const m = liveMsgs.get(String(d.message_id ?? ''))
        if (m) m.endSeq = f.seq
        break
      }
      case 'RETRIEVAL_EVIDENCE': {
        const chunks = Array.isArray(d.chunks) ? (d.chunks as { doc_id?: string }[]) : []
        const paths = Array.isArray(d.graph_paths) ? (d.graph_paths as unknown[]) : []
        extras.push({
          seq: f.seq, source: 'retrieval', kind: '检索',
          title: `证据检索 · 命中 ${chunks.length} 分片 / ${paths.length} 图谱路径${d.degraded ? '（证据链暂缺，降级）' : ''}`,
          detail: cut(chunks.map(c => String(c.doc_id ?? '')).filter(Boolean).join(' · ')),
        })
        break
      }
      case 'run.usage': {
        // api/02 M4 扩展：上下文用量四分组 → 记忆写入 / 上下文注入（检索）两行
        const g = d.groups as { memory?: { summary?: string }[]; graph?: unknown[]; docs?: unknown[] } | undefined
        if (g?.memory?.length) {
          extras.push({
            seq: f.seq, source: 'memory', kind: '记忆',
            title: `记忆写入/召回 ${g.memory.length} 条`,
            detail: cut(g.memory.map(m => String(m.summary ?? '')).filter(Boolean).join('；')),
          })
        }
        if (g?.graph?.length || g?.docs?.length) {
          extras.push({ seq: f.seq + 1, source: 'retrieval', kind: '检索', title: `上下文注入 · GraphRAG 路径 ${g.graph?.length ?? 0} · 引用文档 ${g.docs?.length ?? 0}` })
        }
        break
      }
      case 'artifact.created': {
        const a = d.artifact as { name?: string; summary?: string } | undefined
        extras.push({ seq: f.seq, source: 'system', kind: '系统', title: `产物生成：${a?.name ?? '未命名产物'}`, detail: a?.summary ? cut(a.summary) : undefined })
        break
      }
      case 'workspace.file.created':
      case 'workspace.file.modified':
      case 'workspace.file.deleted':
        extras.push({
          seq: f.seq, source: 'system', kind: '系统',
          title: `工作区文件${f.name.endsWith('created') ? '创建' : f.name.endsWith('modified') ? '更新' : '删除'}：${String(d.path ?? '')}`,
        })
        break
      default:
        // 分叉语义（08 篇）：compact / regenerate 类具名事件高亮「分叉」徽标；其余未知事件忽略（api/02 向前兼容）
        if (/compact/i.test(f.name)) {
          extras.push({ seq: f.seq, source: 'system', kind: '系统', title: `上下文压缩（折叠 seq ≤ ${String(d.compacted_before_seq ?? '—')}）`, fork: '压缩分叉' })
        } else if (/regenerate|retry/i.test(f.name)) {
          extras.push({ seq: f.seq, source: 'system', kind: '系统', title: '重新生成', fork: '重新生成分叉' })
        }
        break
    }
  }

  // 工具调用行：完成位（RESULT > END > START）落位；入参/出参 mono 块
  for (const t of tools.values()) {
    const seq = t.result?.seq ?? t.endSeq ?? t.seq
    extras.push({
      seq, source: 'tool', kind: '工具',
      title: `${t.tool}()${t.result ? (t.result.ok ? ' → 成功' : ' → 失败') : ' → 运行中'}`,
      detail: `${cut(t.result?.summary ?? '', 72)}${t.result?.costMs != null ? ` · ${t.result.costMs}ms` : ''}`,
      request: t.args || undefined,
      response: t.result ? JSON.stringify({ ok: t.result.ok, summary: t.result.summary, cost_ms: t.result.costMs }) : undefined,
    })
  }
  // 回放段流式助手回复行：落位在 MESSAGE_END
  for (const m of liveMsgs.values()) {
    if (!m.content) continue
    extras.push({ seq: m.endSeq ?? m.seq, source: 'assistant', kind: '助手', title: `助手回复（流式 ${m.content.length} 字）`, detail: cut(m.content) })
  }

  // 历史消息基线行（GET /sessions/{id}/messages；无 seq 兜底大号防穿插）
  const msgItems: TrajItemBase[] = messages.map((m, i) => ({
    seq: m.seq ?? 1_000_000 + i,
    source: m.role === 'user' ? 'user' : 'assistant',
    kind: m.role === 'user' ? '用户' : '助手',
    title: m.role === 'user' ? '用户输入' : '助手回复',
    detail: cut(m.content),
  }))

  return [...extras, ...msgItems]
    .sort((a, b) => a.seq - b.seq)
    .map(it => ({ ...it, time: timeFor(it.seq) }))
}
