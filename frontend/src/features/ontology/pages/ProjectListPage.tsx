import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, FileUp, Plus } from 'lucide-react'
import { toast } from 'sonner'
import { useAuthStore } from '@/stores/auth-store'
import { listProjects, type OntoProject } from '../api'
import { TierBadge, relativeTime } from '../components/shared'
import { NewProjectWizard } from '../components/NewProjectWizard'
import { ImportTurtleDialog } from '../components/ImportTurtleDialog'

/** /ontology 本体项目列表（宿主画框 p-onto-list；26 篇 §6.1）：
 *  项目卡网格（名称/三档方案徽标/命名空间 mono/版本/进入）+ IX-OL-01 新建向导
 *  + IX-OL-02 导入 Turtle。roles=ontologist/admin/curator（routes meta）。 */

export function ProjectListPage() {
  const navigate = useNavigate()
  const canWrite = useAuthStore(s => s.can('ontology:write'))
  const [wizardOpen, setWizardOpen] = useState(false)
  const [importProject, setImportProject] = useState<OntoProject | null>(null)

  const { data, isLoading } = useQuery({ queryKey: ['ontology', 'projects'], queryFn: listProjects })
  const projects = useMemo(() => data?.items ?? [], [data])

  return (
    <div className="mx-auto max-w-[1080px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">本体项目</h1>
        <span className="text-xs text-label-3">以本体为语义基座：TBox 版本化 · 变更走评审（候选非成品）</span>
        {canWrite && (
          <button type="button" className="btn btn-p btn-sm ml-auto" onClick={() => setWizardOpen(true)}>
            <Plus size={13} aria-hidden /> 新建项目
          </button>
        )}
      </div>

      {/* 项目卡网格 */}
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {projects.map(p => (
          <div key={p.id} className="card flex flex-col !p-4" data-testid={`project-card-${p.id}`}>
            <div className="flex items-center gap-2">
              <b className="truncate text-sm">{p.name}</b>
              <TierBadge tier={p.tier} />
            </div>
            <div className="mono mt-1.5 truncate text-[11px] text-label-3" title={p.namespace}>
              {p.namespace}
            </div>
            <p className="mt-2 line-clamp-2 min-h-8 text-[11px] leading-4 text-label-2">{p.description}</p>
            <div className="mt-2 flex items-center gap-2 text-[11px] text-label-3">
              <span className="badge b-green">v{p.head_version.replace(/^v/, '')} 已发布</span>
              {p.draft_version && <span className="badge b-orange">草稿 {p.draft_version}</span>}
              <span>
                {p.class_count} 类 · {p.entity_count} 实例
              </span>
            </div>
            <div className="hairline-t mt-3 flex items-center gap-2 pt-3 text-[11px] text-label-3">
              更新于 {relativeTime(p.updated_at)}
              <span className="ml-auto flex gap-1.5">
                {canWrite && (
                  <button type="button" className="btn btn-g btn-sm" onClick={() => setImportProject(p)}>
                    <FileUp size={12} aria-hidden /> 导入
                  </button>
                )}
                <button type="button" className="btn btn-p btn-sm" onClick={() => navigate(`/ontology/${p.id}`)}>
                  进入 <ArrowRight size={12} aria-hidden />
                </button>
              </span>
            </div>
          </div>
        ))}
      </div>

      {isLoading && <div className="empty mt-6"><div className="t">加载中…</div></div>}
      {!isLoading && projects.length === 0 && (
        <div className="empty mt-6">
          <div className="t">还没有本体项目</div>
          <div className="d">从「新建项目」开始，或先到知识库完成一次抽取。</div>
        </div>
      )}

      {/* 跳转 chips（26 篇矩阵联动） */}
      <div className="mt-3 flex gap-2 text-[11px]">
        <Link
          to="/ontology/versions"
          className="rounded-full border border-separator px-3 py-1 text-label-2 hover:border-accent hover:text-accent"
        >
          版本与评审 → 审批闭环
        </Link>
      </div>

      <NewProjectWizard open={wizardOpen} onClose={() => setWizardOpen(false)} />
      <ImportTurtleDialog
        open={!!importProject}
        project={importProject}
        onClose={() => setImportProject(null)}
        onQueued={jobId => toast.success(`导入任务已创建：JOB #${jobId}`, { description: '进度在任务中心跟踪' })}
      />
    </div>
  )
}
