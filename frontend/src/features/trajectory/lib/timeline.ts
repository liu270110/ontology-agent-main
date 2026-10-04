import type { ChatMessage } from '@/stores/session-store'

/** 轨迹时间线组装口径（画框21 p-trajectory + 08 篇只追加事件流）：
 *  数据源 = GET /sessions/{id}/messages（历史基线，用户输入/助手回复行）
 *        + GET /sessions/{id}/events（SSE 帧 JSON 回放段，use-task-events 的 fetch 流解析模式）。
 *  两源按 seq 归并升序 = 模型看到的一切（只追加）；来源分类与 theme --src-* 着色同源：
 *  system=indigo / user=accent / assistant=green / tool=teal / retrieval=orange(context) /
 *  memory=purple(subagent)。对话执行可视化批次（IX 轨迹 IX-CHT-05，docs/api/02 §3）归并口径：
 *  PLAN_UPDATED→计划行·步进列表 / SUBRUN_*→子代理行（memory 紫）/ WORKFLOW_NODE_*→工作流节点行 /
 *  THINKING_*→思考块一行（≤80 字摘要）/ APPROVAL_*→审批行 / INBOX_SPLICED→插队行 / ROUTING_DECISION→路由行。
 *  分叉点标注 = regenerate / compact 类事件 + 第二次及以后的 RUN_STARTED
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

// ---- 对话执行可视化批次（docs/api/02 §3）状态中文映射（与 stores/session-store.ts 归约同枚举口径）----

/** SUBRUN_* 终态/初值中文（SUBRUN_FINISHED 五枚举 + in_progress=STARTED 建行初值） */
const SUBRUN_STATUS_ZH: Record<string, string> = {
  in_progress: '进行中', completed: '已完成', failed: '失败', rejected_artifact: '产物被拒', cancelled: '已取消', timeout: '超时',
}
/** SUBRUN_FINISHED 合法终态（畸形帧收敛 completed，与 session-store 同口径） */
const TERMINAL_SUBRUN: ReadonlySet<string> = new Set(['completed', 'failed', 'rejected_artifact', 'cancelled', 'timeout'])

/** WORKFLOW_NODE_* 状态中文（STARTED 初值 running + FINISHED 五枚举） */
const NODE_STATUS_ZH: Record<string, string> = {
  running: '执行中', succeeded: '成功', failed: '失败', skipped: '跳过', waiting_approval: '待审批', cancelled: '已取消',
}
const TERMINAL_NODE: ReadonlySet<string> = new Set(['succeeded', 'failed', 'skipped', 'waiting_approval', 'cancelled'])

/** INBOX_SPLICED kind 中文（api/02 §3 三枚举） */
const INBOX_KIND_ZH: Record<string, string> = { followup: '追问', steer: '转向', inject: '注入' }

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
  /** 工作流节点标题表（STARTED 带 title，FINISHED 只有 node_id——回填显示名） */
  const wfNodeTitles = new Map<string, string>()
  let runCount = 0
  // ---- 对话执行可视化批次聚合桶（docs/api/02 §3；归并口径=每实体一行，落位终态帧 seq）----
  const plans = new Map<string, { revision: number; seq: number; steps: string[] }>()
  const subruns = new Map<string, {
    seq: number; endSeq?: number; label?: string; goal?: string; index?: number; total?: number; status: string
    phase?: string; toolName?: string; toolCount?: number; preview?: string; tokens?: number
    durationMs?: number; summary?: string; error?: string
  }>()
  const wfNodes = new Map<string, {
    seq: number; endSeq?: number; runId: string; nodeId: string; nodeType?: string; title?: string
    attempt?: number; status: string; durationMs?: number; error?: string
  }>()
  const thinking = new Map<string, { seq: number; endSeq?: number; text: string; effort?: string }>()
  const approvals = new Map<string, {
    seq: number; endSeq?: number; taskId: string; action?: string; paramHash?: string; executionMode?: string
    summary?: string; decision?: string; ticketId?: string
  }>()

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
      // —— 执行结构波 6 事件归类（40 篇 §5.3：新事件归并进既有 TrajSource 分类，subagent
      //    紫已备于 memory 槽位；plan 归 system 从简；工作流节点走 tool 执行槽）——
      case 'PLAN_UPDATED': {
        const items = Array.isArray(d.items) ? (d.items as { content?: unknown; status?: unknown }[]) : []
        const done = items.filter(i => i.status === 'completed').length
        extras.push({
          seq: f.seq, source: 'system', kind: '计划',
          title: `计划更新 rev ${String(d.revision ?? '')} · ${done}/${items.length} 完成`,
          detail: cut(items.map(i => String(i.content ?? '')).filter(Boolean).join('；')),
        })
        break
      }
      case 'SUBRUN_STARTED':
        extras.push({
          seq: f.seq, source: 'memory', kind: '子代理',
          title: `子代理启动：${String(d.label ?? '')}（depth ${String(d.depth ?? 0)} · ${String(d.index ?? 0)}/${String(d.total ?? 0)}）`,
          detail: cut(String(d.goal ?? '')),
        })
        break
      case 'SUBRUN_UPDATED': {
        // 纯实时事件不落 task_events（40 篇 §4.1）——回放通道正常不见，兼容分支仅防未来落库
        const preview = d.preview != null ? String(d.preview) : d.tool_name != null ? `${String(d.tool_name)} · ${String(d.tool_count ?? 0)} 次调用` : ''
        if (preview) {
          extras.push({
            seq: f.seq, source: 'memory', kind: '子代理',
            title: `子代理心跳：${String(d.sub_run_id ?? '').slice(0, 8)}`,
            detail: cut(preview),
          })
        }
        break
      }
      case 'SUBRUN_FINISHED': {
        const ms = Number(d.duration_ms ?? 0)
        const err = d.error as { message?: string } | undefined
        extras.push({
          seq: f.seq, source: 'memory', kind: '子代理',
          title: `子代理结束：${String(d.status ?? '')}${ms > 0 ? ` · ${(ms / 1000).toFixed(1)}s` : ''}`,
          detail: d.summary != null ? cut(String(d.summary)) : err?.message ? cut(String(err.message)) : undefined,
        })
        break
      }
      case 'WORKFLOW_NODE_STARTED':
        wfNodeTitles.set(String(d.node_id ?? ''), String(d.title ?? ''))
        extras.push({
          seq: f.seq, source: 'tool', kind: '节点',
          title: `工作流节点开始：${String(d.title ?? d.node_id ?? '')}`,
          detail: d.node_type != null ? `类型 ${String(d.node_type)} · attempt ${String(d.attempt ?? 1)}` : undefined,
        })
        break
      case 'WORKFLOW_NODE_FINISHED': {
        const nms = Number(d.duration_ms ?? 0)
        const nerr = d.error as { message?: string } | undefined
        extras.push({
          seq: f.seq, source: 'tool', kind: '节点',
          title: `工作流节点结束：${wfNodeTitles.get(String(d.node_id ?? '')) ?? String(d.node_id ?? '')} → ${String(d.status ?? '')}${nms > 0 ? ` · ${(nms / 1000).toFixed(1)}s` : ''}`,
          detail: nerr?.message ? cut(String(nerr.message)) : undefined,
        })
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
      // ---- 对话执行可视化批次（docs/api/02 §3 + 40/42 篇；载荷口径与 session-store 归约一致）----
      case 'PLAN_UPDATED': {
        // 计划行·步进列表：整表替换语义 → 同 plan_id 合并一行（revision 乱序防抖，与 store 同口径），落位最新帧
        const pid = String(d.plan_id ?? '')
        const rev = Number(d.revision)
        if (!pid || !Number.isFinite(rev)) break
        const prev = plans.get(pid)
        if (prev && rev < prev.revision) break
        const items = Array.isArray(d.items) ? (d.items as { content?: unknown; status?: unknown }[]) : []
        plans.set(pid, {
          revision: rev,
          seq: f.seq,
          steps: items
            .map(it => {
              const status = String(it.status ?? '')
              const content = String(it.content ?? '')
              return status ? `[${status}] ${content}` : content
            })
            .filter(Boolean),
        })
        break
      }
      case 'SUBRUN_STARTED': {
        // 子代理行（source=memory 复用紫色 --src-subagent 着色）：STARTED 建行
        const sid = String(d.sub_run_id ?? '')
        if (!sid) break
        subruns.set(sid, {
          seq: f.seq, status: 'in_progress',
          label: typeof d.label === 'string' ? d.label : undefined,
          goal: typeof d.goal === 'string' ? d.goal : undefined,
          index: typeof d.index === 'number' ? d.index : undefined,
          total: typeof d.total === 'number' ? d.total : undefined,
        })
        break
      }
      case 'SUBRUN_UPDATED': {
        // 心跳帧（服务端 300ms 合并）行内刷新：仅收最新一拍
        const s0 = subruns.get(String(d.sub_run_id ?? ''))
        if (!s0) break
        if (typeof d.phase === 'string') s0.phase = d.phase
        if (typeof d.tool_name === 'string') s0.toolName = d.tool_name
        if (typeof d.tool_count === 'number') s0.toolCount = d.tool_count
        if (typeof d.preview === 'string') s0.preview = d.preview
        if (typeof d.tokens === 'number') s0.tokens = d.tokens
        break
      }
      case 'SUBRUN_FINISHED': {
        // 终态落位：畸形 status 收敛 completed（与 session-store 同口径）
        const s0 = subruns.get(String(d.sub_run_id ?? ''))
        if (!s0) break
        const st = typeof d.status === 'string' ? d.status : ''
        s0.status = TERMINAL_SUBRUN.has(st) ? st : 'completed'
        if (typeof d.duration_ms === 'number') s0.durationMs = d.duration_ms
        if (typeof d.summary === 'string') s0.summary = d.summary
        if (typeof d.error === 'string') s0.error = d.error
        s0.endSeq = f.seq
        break
      }
      case 'WORKFLOW_NODE_STARTED': {
        const wid = String(d.workflow_run_id ?? '')
        const nid = String(d.node_id ?? '')
        if (!wid || !nid) break
        wfNodes.set(`${wid}/${nid}`, {
          seq: f.seq, runId: wid, nodeId: nid, status: 'running',
          nodeType: typeof d.node_type === 'string' ? d.node_type : undefined,
          title: typeof d.title === 'string' ? d.title : undefined,
          attempt: typeof d.attempt === 'number' ? d.attempt : undefined,
        })
        break
      }
      case 'WORKFLOW_NODE_FINISHED': {
        // 组/节点 START 缺帧防御性补行（终态不丢，与 store 同口径）
        const wid = String(d.workflow_run_id ?? '')
        const nid = String(d.node_id ?? '')
        if (!wid || !nid) break
        const key = `${wid}/${nid}`
        const n0 = wfNodes.get(key) ?? { seq: f.seq, runId: wid, nodeId: nid, status: 'running' }
        const st = typeof d.status === 'string' ? d.status : ''
        if (TERMINAL_NODE.has(st)) n0.status = st
        if (typeof d.attempt === 'number') n0.attempt = d.attempt
        if (typeof d.duration_ms === 'number') n0.durationMs = d.duration_ms
        if (typeof d.error === 'string') n0.error = d.error
        n0.endSeq = f.seq
        wfNodes.set(key, n0)
        break
      }
      case 'THINKING_START': {
        // 思考块聚合：同 message_id 聚一行（重复 START 按「初始化」语义重置，与 store 同口径）
        const mid = String(d.message_id ?? '')
        if (!mid) break
        thinking.set(mid, {
          seq: f.seq, text: '',
          effort: typeof d.reasoning_effort === 'string' ? d.reasoning_effort : undefined,
        })
        break
      }
      case 'THINKING_CONTENT': {
        const t0 = thinking.get(String(d.message_id ?? ''))
        if (t0) t0.text += String(d.delta ?? '')
        break
      }
      case 'THINKING_END': {
        const t0 = thinking.get(String(d.message_id ?? ''))
        if (t0) t0.endSeq = f.seq
        break
      }
      case 'APPROVAL_REQUIRED': {
        // 审批行：REQUIRED 建行（waiting）
        const rid = String(d.run_id ?? '')
        if (!rid) break
        approvals.set(rid, {
          seq: f.seq,
          taskId: String(d.task_id ?? ''),
          action: typeof d.action_iri === 'string' ? d.action_iri : undefined,
          paramHash: typeof d.param_hash === 'string' ? d.param_hash : undefined,
          executionMode: typeof d.execution_mode === 'string' ? d.execution_mode : undefined,
          summary: typeof d.summary === 'string' ? d.summary : undefined,
        })
        break
      }
      case 'APPROVAL_RESOLVED': {
        // 裁决落位：approved/rejected（escalated/timeout 非 wire 枚举，与 store 同口径忽略）
        const a0 = approvals.get(String(d.run_id ?? ''))
        const decision = typeof d.decision === 'string' ? d.decision : ''
        if (!a0 || (decision !== 'approved' && decision !== 'rejected')) break
        a0.decision = decision
        if (typeof d.ticket_id === 'string') a0.ticketId = d.ticket_id
        a0.endSeq = f.seq
        break
      }
      case 'INBOX_SPLICED': {
        // 插队受理回执行（api/02 §3 ★ M4.5-A）：逐帧一行
        const kind = typeof d.kind === 'string' ? d.kind : ''
        extras.push({
          seq: f.seq, source: 'system', kind: '插队',
          title: `插队受理（${INBOX_KIND_ZH[kind] ?? (kind || '未知')} · 来自 ${String(d.source ?? '—')}）`,
          detail: cut(String(d.text ?? '')),
        })
        break
      }
      case 'ROUTING_DECISION': {
        // 路由决议行（27 篇 X15）：协调者 mode/selected/reason
        const names = Array.isArray(d.names) ? d.names.map(x => String(x)) : []
        const selected = Array.isArray(d.selected) ? d.selected.map(x => String(x)) : []
        extras.push({
          seq: f.seq, source: 'system', kind: '路由',
          title: `路由决议 · ${String(d.mode ?? '—')} → ${names.length ? names.join('、') : selected.join('、') || '—'}`,
          detail: cut(
            [typeof d.selected_by === 'string' ? `选定者 ${d.selected_by}` : '', typeof d.reason === 'string' ? d.reason : '']
              .filter(Boolean)
              .join(' · '),
          ),
        })
        break
      }
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
  // 计划行·步进列表：每 plan 一行，落位最新 revision 帧（整表替换语义）
  for (const [pid, p] of plans) {
    extras.push({
      seq: p.seq, source: 'system', kind: '计划',
      title: `执行计划（${pid.slice(0, 8)} · v${p.revision} · ${p.steps.length} 步）`,
      detail: p.steps.length ? cut(p.steps.map((s, i) => `${i + 1}. ${s}`).join('；')) : undefined,
    })
  }
  // 子代理行：落位终态帧（未 FINISHED 落 STARTED 帧）；source=memory 紫色 --src-subagent
  for (const [sid, s0] of subruns) {
    const parts = [
      s0.goal,
      s0.phase,
      s0.toolCount != null ? `工具 ${s0.toolCount} 个${s0.toolName ? `（${s0.toolName}）` : ''}` : '',
      s0.tokens != null ? `${s0.tokens} tokens` : '',
      s0.durationMs != null ? `${s0.durationMs}ms` : '',
      s0.error ?? s0.summary ?? '',
    ].filter(Boolean)
    extras.push({
      seq: s0.endSeq ?? s0.seq, source: 'memory', kind: '子代理',
      title: `子代理 ${s0.label ?? sid.slice(0, 8)}${s0.index != null && s0.total != null ? `（${s0.index}/${s0.total}）` : ''} · ${SUBRUN_STATUS_ZH[s0.status] ?? s0.status}`,
      detail: cut(parts.join(' · ')),
    })
  }
  // 工作流节点行：组/节点一行，落位终态帧
  for (const n of wfNodes.values()) {
    extras.push({
      seq: n.endSeq ?? n.seq, source: 'system', kind: '工作流',
      title: `工作流节点 ${n.title ?? n.nodeType ?? n.nodeId} · ${NODE_STATUS_ZH[n.status] ?? n.status}`,
      detail: cut([`workflow ${n.runId.slice(0, 8)}`, n.attempt != null ? `第 ${n.attempt} 次尝试` : '', n.durationMs != null ? `${n.durationMs}ms` : '', n.error ?? ''].filter(Boolean).join(' · ')),
    })
  }
  // 思考块行：THINKING_* 聚合一行，附 ≤80 字文本摘要；未 END 落当前帧位
  for (const t of thinking.values()) {
    if (!t.text) continue
    extras.push({
      seq: t.endSeq ?? t.seq, source: 'assistant', kind: '思考',
      title: `思考块（${t.effort ? `effort=${t.effort} · ` : ''}${t.text.length} 字）`,
      detail: cut(t.text, 80),
    })
  }
  // 审批行：REQUIRED→RESOLVED 归并一行，落位裁决帧（未裁决落 REQUIRED 帧、显示待审批）
  for (const a of approvals.values()) {
    extras.push({
      seq: a.endSeq ?? a.seq, source: 'system', kind: '审批',
      title: `审批 · ${(a.summary ?? a.action ?? a.taskId) || '审批请求'}${a.decision ? (a.decision === 'approved' ? ' → 已批准' : ' → 已拒绝') : ' → 待审批'}`,
      detail: cut([`task ${a.taskId}`, a.action, a.paramHash ? `param_hash ${a.paramHash}` : '', a.executionMode, a.ticketId ? `票 ${a.ticketId}` : ''].filter(Boolean).join(' · ')),
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
