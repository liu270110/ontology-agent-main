import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, GitBranch, History, Lock, Play, Plus } from 'lucide-react'
import { listWorkflows } from '../api'
import { NewWorkflowDialog } from '../components/WfDialogs'

/** /workflows 列表页（27 篇 P15 / 画框 29）：WorkflowCard 网格（名称/版本状态/最近运行
 *  成功率/ACL 徽标）+ IX-GRP-06 新建/从模板。角色 admin/ontologist（运行：全部）。 */
export function WorkflowListPage() {
  const navigate = useNavigate()
  const [newOpen, setNewOpen] = useState(false)
  const listQ = useQuery({ queryKey: ['wf', 'list'], queryFn: () => listWorkflows() })

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-6" data-testid="wf-list-page">
      <div className="mb-4 flex items-center gap-3">
        <div>
          <h1 className="text-lg font-bold">工作流编排</h1>
          <p className="mt-0.5 text-xs text-label-3">把 Agent 插槽、工具、知识检索、审批编排为可版本化、可试运行的有向图（27 篇 P15）。</p>
        </div>
        <button type="button" className="btn btn-p ml-auto" data-testid="wf-new-open" onClick={() => setNewOpen(true)}>
          <Plus size={13} aria-hidden />
          新建 / 从模板
        </button>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {(listQ.data?.items ?? []).map(w => (
          <button
            key={w.id}
            type="button"
            data-testid={`wf-card-${w.id}`}
            className="card glare-hover p-4 text-left"
            onClick={() => navigate(`/workflows/${w.id}`)}
          >
            <div className="flex items-center gap-2">
              <span className="flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-accent-soft text-accent">
                <GitBranch size={15} aria-hidden />
              </span>
              <b className="truncate text-[13px]">{w.name}</b>
              {w.acl === 'publish' ? (
                <span className="badge b-green ml-auto">ACL · 可发布</span>
              ) : (
                <span className="badge b-gray ml-auto"><Lock size={9} aria-hidden />ACL · 可编辑</span>
              )}
            </div>
            <p className="mt-2 line-clamp-2 min-h-[32px] text-xs leading-relaxed text-label-2">{w.description}</p>
            <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
              {w.head_version && w.head_version === w.draft_version ? (
                <span className="badge b-blue">已发布 {w.head_version}</span>
              ) : (
                <>
                  <span className="badge b-orange">草稿 {w.draft_version}</span>
                  {w.head_version && <span className="badge b-gray">已发布 {w.head_version}</span>}
                </>
              )}
              <span className="badge b-gray">{w.node_count} 节点 · {w.edge_count} 边</span>
            </div>
            <div className="mt-2.5 flex items-center gap-2 text-[11px] text-label-3">
              <span className="num-tick">最近成功率 <b style={{ color: w.success_rate >= 95 ? 'var(--green)' : 'var(--orange)' }}>{w.success_rate}%</b></span>
              <span>· {w.runs} 次运行</span>
              <span className="ml-auto inline-flex items-center gap-1" style={{ color: 'var(--accent)' }}>
                打开编辑器 <ArrowRight size={11} aria-hidden />
              </span>
            </div>
          </button>
        ))}
      </div>

      {(listQ.data?.items.length ?? 0) === 0 && (
        <div className="empty mt-16">
          <Play aria-hidden />
          <div className="t">还没有工作流</div>
          <div className="d">从模板（空白 / 审批流 / 检索问答）开始编排。</div>
          <div className="acts">
            <button type="button" className="btn btn-p" onClick={() => setNewOpen(true)}>
              <Plus size={13} aria-hidden />
              新建 / 从模板
            </button>
          </div>
        </div>
      )}

      <div className="mt-4 flex items-center gap-2 text-[11px] text-label-3">
        <History size={12} aria-hidden />
        运行历史 → /workflows/:id 运行 Tab（任务中心 type=workflow_run 过滤）；发布生成不可变版本（候选非成品，宪法 3）。
      </div>

      <NewWorkflowDialog open={newOpen} onClose={() => setNewOpen(false)} />
    </div>
  )
}
