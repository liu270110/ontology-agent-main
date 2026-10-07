import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { ArrowRight, FileUp, Plus } from 'lucide-react'
import { toast } from 'sonner'
import { useAuthStore } from '@/stores/auth-store'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonCards } from '@/components/states'
import { Tooltip } from '@/components/tooltip'
import { listProjects, TIER_LABEL, type OntoProject, type OntoTier } from '../api'
import { TierBadge, relativeTime } from '../components/shared'
import { NewProjectWizard } from '../components/NewProjectWizard'
import { ImportTurtleDialog } from '../components/ImportTurtleDialog'

/** /ontology 本体项目列表（宿主画框 p-onto-list；26 篇 §6.1）：
 *  项目卡网格（名称/三档方案徽标/命名空间 mono/版本/进入）+ 三档方案 seg 筛选
 *  （38 号对账 L 组：light/standard/heavy，字段=OntoProject.tier）+ 尾部虚线新建卡
 *  （点击唤起 IX-OL-01 向导）+ 卡片 stagger 入场（前 8 项 index×40ms 递增延迟）
 *  + IX-OL-02 导入 Turtle。roles=ontologist/admin/curator（routes meta）。 */

/** 三档方案一句话解释（TierBadge 悬停提示；代码三档 heavy/standard/light，
 *  对齐任务口径：完整治理 → 精简 TBox → 术语层） */
const TIER_TOOLTIP: Record<OntoProject['tier'], string> = {
  heavy: '重型：完整 TBox + SHACL 全量治理（推理与规则引擎齐备）',
  standard: '标准：精简 TBox（类/属性/公理按需 + 增量推理）',
  light: '轻量：术语层（术语与实例为主，仅基础校验）',
}

/** seg 筛选档位顺序（三档 fixed 闭环集，恒渲染不按计数隐藏——与 changeset 状态 seg 不同：
 *  方案档位是建模契约不是事实状态） */
const TIER_FILTER_ORDER: OntoTier[] = ['light', 'standard', 'heavy']

/** stagger 上限：仅前 8 张卡吃 index×40ms 递增延迟，其后 0 延迟立即入场 */
const STAGGER_CAP = 8

export function ProjectListPage() {
  const navigate = useNavigate()
  const canWrite = useAuthStore(s => s.can('ontology:write'))
  const [wizardOpen, setWizardOpen] = useState(false)
  const [importProject, setImportProject] = useState<OntoProject | null>(null)
  const [tierFilter, setTierFilter] = useState<'all' | OntoTier>('all')

  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ['ontology', 'projects'], queryFn: listProjects })
  const allProjects = useMemo(() => data?.items ?? [], [data])
  const projects = useMemo(
    () => (tierFilter === 'all' ? allProjects : allProjects.filter(p => p.tier === tierFilter)),
    [allProjects, tierFilter],
  )

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

      {/* 三档方案 seg 筛选（38 号对账 L 组；testid 对齐 VersionsPage cs-filter-* 口径） */}
      <div className="mt-3 flex items-center gap-3" role="group" aria-label="按方案档位筛选项目">
        <div className="seg">
          <button
            type="button"
            className={`seg-btn ${tierFilter === 'all' ? 'on' : ''}`}
            aria-pressed={tierFilter === 'all'}
            data-testid="tier-filter-all"
            onClick={() => setTierFilter('all')}
          >
            全部 {allProjects.length}
          </button>
          {TIER_FILTER_ORDER.map(t => (
            <button
              key={t}
              type="button"
              className={`seg-btn ${tierFilter === t ? 'on' : ''}`}
              aria-pressed={tierFilter === t}
              data-testid={`tier-filter-${t}`}
              onClick={() => setTierFilter(t)}
            >
              {TIER_LABEL[t]} {allProjects.filter(p => p.tier === t).length}
            </button>
          ))}
        </div>
      </div>

      {/* 项目卡网格（stagger 入场：前 8 张 index×40ms 递增延迟，data-stagger 记录延迟档位） */}
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {projects.map((p, index) => (
          <motion.div
            key={p.id}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.28, ease: 'easeOut', delay: index < STAGGER_CAP ? index * 0.04 : 0 }}
            data-stagger={index < STAGGER_CAP ? index : undefined}
          >
            <div className="card flex h-full flex-col !p-4" data-testid={`project-card-${p.id}`}>
            <div className="flex items-center gap-2">
              <b className="truncate text-sm">{p.name}</b>
              {/* S8 Tooltip：三档方案徽标悬停解释 */}
              <Tooltip content={TIER_TOOLTIP[p.tier]}>
                <TierBadge tier={p.tier} />
              </Tooltip>
            </div>
            <div className="mono mt-1.5 truncate text-[11px] text-label-3" title={p.namespace}>
              {p.namespace}
            </div>
            <p className="mt-2 line-clamp-2 min-h-8 text-[11px] leading-4 text-label-2">{p.description}</p>
            <div className="mt-2 flex items-center gap-2 text-[11px] text-label-3">
              {p.head_version && <span className="badge b-green">v{p.head_version.replace(/^v/, '')} 已发布</span>}
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
          </motion.div>
        ))}
      </div>

      {/* 尾部虚线新建卡（38 号对账 L 组）：网格末位常驻入口，点击唤起 IX-OL-01 向导；
          写权限门控与头部「新建项目」按钮同口径 */}
      {canWrite && !isError && (
        <button
          type="button"
          data-testid="project-card-new"
          onClick={() => setWizardOpen(true)}
          className="card mt-3 flex min-h-[120px] w-full flex-col items-center justify-center gap-1.5 !p-4 text-label-3 transition-colors hover:border-accent hover:text-accent"
          style={{ borderStyle: 'dashed' }}
        >
          <Plus size={18} aria-hidden />
          <b className="text-sm">新建本体项目</b>
          <span className="text-[11px]">三档方案向导：轻量 / 标准 / 重型</span>
        </button>
      )}

      {/* S8 状态切片：首载骨架卡 / 失败错误态（重试=refetch） */}
      {isLoading && <SkeletonCards count={3} className="mt-4" />}
      {!isLoading && isError && (
        <ErrorState
          className="mt-6"
          message={error instanceof Error ? error.message : undefined}
          code={error instanceof ApiError ? error.code : undefined}
          onRetry={() => void refetch()}
        />
      )}
      {!isLoading && !isError && projects.length === 0 && allProjects.length > 0 && (
        <div className="empty mt-6">
          <div className="t">该方案档位暂无项目</div>
          <div className="d">切回「全部」查看其余项目。</div>
        </div>
      )}
      {!isLoading && !isError && allProjects.length === 0 && (
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
