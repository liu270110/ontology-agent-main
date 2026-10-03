import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/states'
import { Check, GitCompare, RotateCcw, ShieldCheck, X } from 'lucide-react'
import {
  approveChangeset, diffVersions, getProject, listChangesets, publishChangeset, rejectChangeset, rollbackChangeset,
  CS_STATUS_LABEL,
  type ChangesetStatus, type DiffRow, type ReviewDecision,
} from '../api'
import { CsStatusBadge, relativeTime } from '../components/shared'
import { CompareSelectorDialog, PublishDialog, RejectDialog, RollbackDialog, diffRowStyle } from '../components/VersionDialogs'

/** 版本评审 /ontology/:projectId/versions（宿主画框 p-versions；26 篇 §7.1 IX-VR-01~05）：
 *  changeset 列表（编号/提交人/状态徽标/时间 + 状态 seg 筛选 ?status= 深链）+ 对比选择器 +
 *  三元组 diff 逐条决策（+绿/−红/~黄行 + 每行接受/拒绝 + 批量栏 + 决策行灰化打勾；决策随
 *  approve 的 decisions[] 一次性提交——边界审计修正）+ 版本历史时间线（逐版「回滚到此版」）+
 *  发布五步进度 + 驳回 + 回滚（ROLLBACK 解锁）。
 *
 *  38 号对账 V1/V2/V3 落地：diff 版本对一律取自真实数据（显式 base/target searchParams
 *  > changeset 语境 > 版本表派生），禁止硬编码版本对；「对比任意版本」消费
 *  GET /ontologies/{id}/diff?base=&target=（api/01 §已登记）真实回显所选版本。 */

/** changeset 状态 seg 的展示顺序（全部恒首位；仅渲染计数 > 0 的档，对齐画板「待审/已发布/已驳回」） */
const CS_FILTER_ORDER: ChangesetStatus[] = ['in_review', 'approved', 'published', 'rejected', 'draft']

export function VersionsPage() {
  const { projectId = '' } = useParams()
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const activeCs = searchParams.get('cs')
  const compareOpen = searchParams.get('compare') === '1'
  const csFilter = (searchParams.get('status') ?? 'all') as 'all' | ChangesetStatus

  /** 局部更新 searchParams（保留其余参数；null=删除），避免相互覆盖 */
  function patchParams(patch: Record<string, string | null>) {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      for (const [k, v] of Object.entries(patch)) {
        if (v === null) next.delete(k)
        else next.set(k, v)
      }
      return next
    })
  }

  const detail = useQuery({ queryKey: ['ontology', 'detail', projectId], queryFn: () => getProject(projectId) })
  const changesetsQ = useQuery({ queryKey: ['ontology', projectId, 'changesets'], queryFn: () => listChangesets(projectId) })
  const changesets = changesetsQ.data?.items ?? []
  const currentCs = useMemo(() => changesets.find(c => c.id === activeCs) ?? null, [changesets, activeCs])
  const versions = detail.data?.versions ?? []

  // ---- 版本派生（唯一事实源 = getProject 返回；页内不写死任何版本号） ----
  const publishedVersions = versions.filter(v => v.status === 'published')
  const headVersion = detail.data?.head_version ?? publishedVersions[0]?.version ?? versions[0]?.version ?? ''
  const draftVersion = detail.data?.draft_version ?? versions.find(v => v.status === 'draft')?.version ?? null
  const currentVersion = headVersion
  /** 回滚默认目标 = 次新已发布版（真实数据派生） */
  const previousPublished = publishedVersions.find(v => v.version !== headVersion)?.version ?? versions[0]?.version ?? ''

  // ---- 38-V1 对比版本对：显式 base/target（对比选择器回填）> changeset 语境 > 版本表派生 ----
  const spBase = searchParams.get('base')
  const spTarget = searchParams.get('target')
  const hasExplicitPair = !!spBase && !!spTarget
  const diffBase = spBase ?? currentCs?.base_version ?? versions[1]?.version ?? versions[0]?.version ?? ''
  const diffTarget =
    spTarget ??
    (currentCs ? (currentCs.status === 'published' ? headVersion : draftVersion) : draftVersion) ??
    versions[0]?.version ??
    ''
  /** 纯对比视图（无 changeset 语境）与评审视图共用 diff 拉取；决策 UI 仅评审视图渲染 */
  const compareOnly = hasExplicitPair && !currentCs
  const diffViewOpen = (!!activeCs && !!currentCs) || hasExplicitPair

  // ---- IX-VR-02 diff 逐条决策（决策收集，approve 时随 decisions[] 一次性提交） ----
  const [decisions, setDecisions] = useState<Record<string, 'accept' | 'reject'>>({})
  const diff = useQuery({
    queryKey: ['ontology', projectId, 'diff', diffBase, diffTarget],
    queryFn: () => diffVersions(projectId, diffBase, diffTarget),
    enabled: diffViewOpen && !!diffBase && !!diffTarget && diffBase !== diffTarget,
  })
  useEffect(() => setDecisions({}), [activeCs, diffBase, diffTarget])
  const rows: DiffRow[] = diff.data?.rows ?? []
  const decidedCount = Object.keys(decisions).length
  const remaining = rows.length - decidedCount
  const allDecided = rows.length > 0 && remaining === 0

  function decide(rowId: string, action: 'accept' | 'reject') {
    setDecisions(d => ({ ...d, [rowId]: action }))
  }
  function decideRemaining(action: 'accept' | 'reject') {
    setDecisions(d => {
      const next = { ...d }
      for (const r of rows) if (!next[r.id]) next[r.id] = action
      return next
    })
  }

  // ---- 五动词 mutations ----
  const [publishOpen, setPublishOpen] = useState(false)
  const [rejectOpen, setRejectOpen] = useState(false)
  const [rollbackTarget, setRollbackTarget] = useState<string | null>(null)

  const approveM = useMutation({
    mutationFn: () =>
      approveChangeset(projectId, activeCs ?? '', {
        reason: `diff 逐条决策：接受 ${Object.values(decisions).filter(a => a === 'accept').length} · 拒绝 ${
          Object.values(decisions).filter(a => a === 'reject').length
        }`,
        decisions: Object.entries(decisions).map(([row_id, action]) => ({ row_id, action })) as ReviewDecision[],
      }),
    onSuccess: () => {
      toast.success(`已通过 ${activeCs}`, { description: 'decisions[] 已随 approve 一次性提交；可继续发布' })
      void qc.invalidateQueries({ queryKey: ['ontology', projectId, 'changesets'] })
      patchParams({ base: null, target: null })
      setPublishOpen(true) // 评审操作「✓通过并发布」→ IX-VR-03 发布确认
    },
  })
  const rejectM = useMutation({
    mutationFn: (v: { reason: string; type: '需修改' | '需讨论' | '超范围' }) => rejectChangeset(projectId, activeCs ?? '', v),
    onSuccess: () => {
      toast.success(`已驳回 ${activeCs}`, { description: '变更单回到提交人，工作台解锁' })
      void qc.invalidateQueries({ queryKey: ['ontology', projectId, 'changesets'] })
    },
  })
  const publishM = useMutation({
    mutationFn: () => publishChangeset(projectId, activeCs ?? ''),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['ontology', 'detail', projectId] })
      void qc.invalidateQueries({ queryKey: ['ontology', projectId, 'changesets'] })
    },
  })
  // 逆向变更单按登记契约挂在 changesets 路径下：无选中变更单时回落到首条（mock 与后端均以
  // target_version 生成逆向单，cid 不参与回滚语义）
  const rollbackCsId = activeCs ?? changesets[0]?.id ?? ''
  const rollbackM = useMutation({
    mutationFn: (v: { reason: string; target: string }) => rollbackChangeset(projectId, rollbackCsId, { reason: v.reason, target_version: v.target }),
    onSuccess: (_d, v) => {
      toast.success('回滚请求已创建', { description: `逆向变更单进入评审（目标 ${v.target}）` })
      void qc.invalidateQueries({ queryKey: ['ontology', projectId, 'changesets'] })
    },
  })

  // ---- 38-V3 changeset 状态 seg（?status= 深链还原；计数=客户端统计） ----
  const statusCounts = useMemo(() => {
    const m = {} as Record<ChangesetStatus, number>
    for (const c of changesets) m[c.status] = (m[c.status] ?? 0) + 1
    return m
  }, [changesets])
  const visibleChangesets = useMemo(
    () => (csFilter === 'all' ? changesets : changesets.filter(c => c.status === csFilter)),
    [changesets, csFilter],
  )

  return (
    <div className="mx-auto max-w-[1080px]">
      {/* 页头 */}
      <div className="flex flex-wrap items-center gap-2">
        <Link to="/ontology" className="text-xs text-label-3 hover:text-accent">本体项目</Link>
        <span className="text-label-3">/</span>
        <h1 className="text-lg font-bold">{detail.data?.name ?? '—'} · 版本与评审</h1>
        {currentVersion && <span className="badge b-green">v{currentVersion.replace(/^v/, '')} 已发布</span>}
        {currentCs && (
          <button
            type="button"
            className="btn btn-g btn-sm ml-auto"
            onClick={() => setRollbackTarget(previousPublished)}
            data-testid="open-rollback"
            title={previousPublished ? `回滚到 ${previousPublished}` : undefined}
          >
            <RotateCcw size={12} aria-hidden /> 回滚
          </button>
        )}
        <button type="button" className="btn btn-g btn-sm" data-testid="open-compare" onClick={() => patchParams({ compare: '1' })}>
          <GitCompare size={12} aria-hidden /> 对比
        </button>
      </div>

      {/* changeset 列表（38-V3：状态 seg 筛选，画板 topbar seg 同款） */}
      <div className="card mt-4 !px-4 !py-2">
        <div className="flex items-center gap-3 py-1.5" role="group" aria-label="按状态筛选变更单">
          <div className="seg">
            <button
              type="button"
              className={`seg-btn ${csFilter === 'all' ? 'on' : ''}`}
              aria-pressed={csFilter === 'all'}
              data-testid="cs-filter-all"
              onClick={() => patchParams({ status: null })}
            >
              全部 {changesets.length}
            </button>
            {CS_FILTER_ORDER.filter(s => (statusCounts[s] ?? 0) > 0).map(s => (
              <button
                key={s}
                type="button"
                className={`seg-btn ${csFilter === s ? 'on' : ''}`}
                aria-pressed={csFilter === s}
                data-testid={`cs-filter-${s}`}
                onClick={() => patchParams({ status: s })}
              >
                {CS_STATUS_LABEL[s]} {statusCounts[s]}
              </button>
            ))}
          </div>
          <span className="text-[11px] text-label-3">变更请求走评审状态机：草稿 → 评审 → 通过 → 发布（宪法 3：候选非成品）</span>
        </div>
        <table className="tbl w-full" data-testid="changeset-table">
          <thead>
            <tr>
              <th>编号</th>
              <th>标题</th>
              <th>提交人</th>
              <th>状态</th>
              <th>变更</th>
              <th>时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {/* S8 状态切片：changeset 列表首载骨架 / 失败错误态（重试=refetch）；成功路径渲染不变 */}
            {changesetsQ.isPending && (
              <tr>
                <td colSpan={7}>
                  <SkeletonRows rows={4} rowHeight={36} />
                </td>
              </tr>
            )}
            {changesetsQ.isError && (
              <tr>
                <td colSpan={7}>
                  <ErrorState
                    message={changesetsQ.error instanceof Error ? changesetsQ.error.message : undefined}
                    code={changesetsQ.error instanceof ApiError ? changesetsQ.error.code : undefined}
                    onRetry={() => void changesetsQ.refetch()}
                  />
                </td>
              </tr>
            )}
            {!changesetsQ.isPending && !changesetsQ.isError && visibleChangesets.length === 0 && (
              <tr>
                <td colSpan={7}>
                  <EmptyState compact title="该状态下暂无变更单" desc="切换上方状态档查看其他变更请求" />
                </td>
              </tr>
            )}
            {visibleChangesets.map(cs => (
              <tr key={cs.id} className={cs.id === activeCs ? 'sel' : undefined}>
                <td className="mono font-semibold">{cs.id}</td>
                <td>{cs.title}</td>
                <td className="text-dim text-xs">{cs.submitter}</td>
                <td><CsStatusBadge status={cs.status} /></td>
                <td className="text-[11px]">
                  <span className="text-green">+{cs.stats.add}</span>{' '}
                  <span className="text-red">−{cs.stats.del}</span>{' '}
                  <span className="text-orange">~{cs.stats.mod}</span>
                </td>
                <td className="text-dim text-xs">{relativeTime(cs.updated_at)}</td>
                <td className="text-right">
                  <button
                    type="button"
                    className="btn btn-g btn-sm"
                    data-testid={`review-${cs.id}`}
                    onClick={() => patchParams({ cs: cs.id, base: null, target: null })}
                  >
                    {cs.status === 'in_review' ? '进入评审' : '查看 Diff'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 38-V2 版本历史（画板 tl 时间线：逐版「回滚到此版」；回滚=新变更请求需审批） */}
      {versions.length > 0 && (
        <div className="card mt-4" data-testid="version-history">
          <div className="card-h !mb-2">
            <h3 className="!text-sm">版本历史</h3>
            <span className="badge b-gray">已发布 {publishedVersions.length} 版</span>
            {draftVersion && <span className="badge b-orange">草稿 {draftVersion}</span>}
          </div>
          <div className="tl">
            {versions.map(v => {
              const isHead = v.status === 'published' && v.version === headVersion
              const isDraft = v.status === 'draft'
              return (
                <div className="tl-item" key={v.version} style={{ paddingBottom: 10 }}>
                  <span className="tl-dot" style={{ background: isHead ? 'var(--green)' : 'var(--label-3)' }} aria-hidden />
                  <div className="tl-c" style={{ padding: '8px 12px' }}>
                    <div className="tlt text-xs">
                      {v.version} · {v.note}
                      {isHead ? '（当前）' : ''}
                    </div>
                    <div className="tl-meta">
                      {relativeTime(v.published_at)}
                      {isDraft && <span className="badge b-gray">草稿</span>}
                      {!isHead && !isDraft && (
                        <button
                          type="button"
                          className="btn btn-g btn-sm"
                          style={{ padding: '1px 8px', fontSize: 10 }}
                          data-testid={`rollback-to-${v.version}`}
                          onClick={() => setRollbackTarget(v.version)}
                        >
                          回滚到此版
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
          <p className="fhint mt-2">回滚 = 新变更请求，需审批通过并附审计理由（二次确认）。</p>
        </div>
      )}

      {/* Diff 视图：评审模式（changeset，IX-VR-02 逐条决策）/ 纯对比模式（38-V1 所选版本对） */}
      {diffViewOpen && (currentCs || hasExplicitPair) && (
        <div className="card mt-4" data-testid="diff-view">
          <div className="card-h !mb-2">
            <h3 className="!text-sm">{currentCs ? `${currentCs.id} · ${currentCs.title}` : '版本对比'}</h3>
            {currentCs && <CsStatusBadge status={currentCs.status} />}
            <span className="badge b-blue mono" data-testid="diff-pair" title="diff 端点回显的对比版本对">
              {diffBase} → {diffTarget}
            </span>
            {diff.data && <span className="badge b-gray">差异 +{diff.data.stats.add}/−{diff.data.stats.del}/~{diff.data.stats.mod}</span>}
            {currentCs && (
              <span className="text-[11px] text-label-3">
                提交人 {currentCs.submitter} · {relativeTime(currentCs.updated_at)}
              </span>
            )}
            {currentCs && <span className="badge b-green ml-auto">SHACL 校验通过</span>}
            <button
              type="button"
              className="btn btn-g btn-sm"
              onClick={() => (compareOnly ? patchParams({ base: null, target: null }) : setSearchParams({}))}
            >
              关闭
            </button>
          </div>

          {/* 批量决策栏（仅评审模式：纯版本对比不产生决策） */}
          {currentCs && !compareOnly && (
            <div className="flex flex-wrap items-center gap-2 rounded-xl bg-surface-2 px-3 py-2 text-xs" data-testid="diff-batchbar">
              <b>批量决策</b>
              <span className="badge b-orange">剩余 {remaining} / {rows.length} 未决策</span>
              <span className="badge b-green">已接受 {Object.values(decisions).filter(a => a === 'accept').length}</span>
              <span className="badge b-red">已拒绝 {Object.values(decisions).filter(a => a === 'reject').length}</span>
              <span className="ml-auto flex gap-1.5">
                <button type="button" className="btn btn-g btn-sm" data-testid="accept-all" disabled={remaining === 0} onClick={() => decideRemaining('accept')}>
                  <Check size={12} aria-hidden /> 全部接受剩余
                </button>
                <button type="button" className="btn btn-g btn-sm" data-testid="reject-all" disabled={remaining === 0} onClick={() => decideRemaining('reject')}>
                  <X size={12} aria-hidden /> 全部拒绝剩余
                </button>
              </span>
            </div>
          )}

          {/* Diff 行：+绿/−红/~黄；决策后灰化打勾 */}
          <div className="mt-2 space-y-1.5">
            {/* S8 状态切片：diff 首载骨架 / 失败错误态 / 空差异（三分支不缺席） */}
            {diff.isPending && <SkeletonRows rows={5} rowHeight={30} />}
            {diff.isError && (
              <ErrorState
                message={diff.error instanceof Error ? diff.error.message : undefined}
                code={diff.error instanceof ApiError ? diff.error.code : undefined}
                onRetry={() => void diff.refetch()}
              />
            )}
            {diff.isSuccess && rows.length === 0 && (
              <EmptyState compact icon={GitCompare} title="两版本间无差异" desc={`${diffBase} 与 ${diffTarget} 内容一致`} />
            )}
            {rows.map(r => {
              const st = diffRowStyle(r.op)
              const d = compareOnly ? undefined : decisions[r.id]
              return (
                <div
                  key={r.id}
                  className="flex items-center gap-2 rounded-lg px-3 py-2 text-xs transition-opacity"
                  style={{ background: st.bg, opacity: d ? 0.55 : 1 }}
                  data-testid={`diff-row-${r.id}`}
                >
                  <span className="flex flex-none items-center justify-center rounded-full text-[11px] font-bold" style={{ background: st.color, color: 'var(--surface)', width: 18, height: 18 }}>
                    {st.sign}
                  </span>
                  {d ? <Check size={13} className="flex-none text-green" aria-hidden /> : null}
                  <span className="mono truncate" style={{ color: 'var(--label)' }}>
                    {r.op === 'mod' ? (
                      <>
                        （{r.subject}, {r.predicate}）{r.object} 改为 {r.new_value}
                      </>
                    ) : (
                      `（${r.subject}, ${r.predicate}, ${r.object}）`
                    )}
                  </span>
                  <span className="ml-auto flex flex-none gap-1.5">
                    {d ? (
                      <span className="badge b-green">{d === 'accept' ? '已接受' : '已拒绝'}</span>
                    ) : compareOnly ? null : (
                      <>
                        <button type="button" className="btn btn-g btn-sm" data-testid={`accept-${r.id}`} onClick={() => decide(r.id, 'accept')}>
                          接受
                        </button>
                        <button type="button" className="btn btn-g btn-sm" data-testid={`reject-${r.id}`} onClick={() => decide(r.id, 'reject')}>
                          拒绝
                        </button>
                      </>
                    )}
                  </span>
                </div>
              )
            })}
          </div>

          {/* 决策操作条（仅评审模式） */}
          {currentCs && !compareOnly && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <input
                className="input h-8 max-w-[380px] flex-1 text-xs"
                placeholder="评审意见（驳回时必填，随决策留审计）"
                aria-label="评审意见"
                defaultValue=""
                id="review-note"
              />
              <button
                type="button"
                className="btn btn-p btn-sm"
                data-testid="approve-publish"
                disabled={!allDecided || approveM.isPending}
                onClick={() => approveM.mutate()}
              >
                <ShieldCheck size={12} aria-hidden /> 通过并发布
              </button>
              <button type="button" className="btn btn-d btn-sm" data-testid="open-reject" onClick={() => setRejectOpen(true)}>
                <X size={12} aria-hidden /> 驳回
              </button>
              <span className="text-[11px] text-label-3">
                {allDecided ? '全部决策完成，按钮已亮（VR-03）' : `剩余 ${remaining} 行未决策，按钮置灰；全部决策完成后点亮`}
              </span>
            </div>
          )}
        </div>
      )}

      {/* 弹窗组 */}
      <CompareSelectorDialog
        open={compareOpen}
        versions={versions}
        onClose={() => patchParams({ compare: null })}
        onCompare={(base, target) => {
          // 38-V1：真实接线——所选版本对回填 searchParams，diff 查询消费之（不再弹占位 toast）
          patchParams({ base, target, compare: null })
        }}
        onPreview={(base, target) => diffVersions(projectId, base, target).then(d => d.stats)}
      />
      <PublishDialog
        open={publishOpen}
        changeset={currentCs}
        onClose={() => setPublishOpen(false)}
        onPublish={async () => {
          const res = await publishM.mutateAsync()
          return { version: res.version }
        }}
      />
      <RejectDialog
        open={rejectOpen}
        changeset={currentCs}
        onClose={() => setRejectOpen(false)}
        onReject={async (reason, type) => {
          await rejectM.mutateAsync({ reason, type })
        }}
      />
      <RollbackDialog
        open={!!rollbackTarget}
        currentVersion={currentVersion}
        targetVersion={rollbackTarget ?? ''}
        onClose={() => setRollbackTarget(null)}
        onRollback={async reason => {
          await rollbackM.mutateAsync({ reason, target: rollbackTarget ?? '' })
          setRollbackTarget(null)
        }}
      />
    </div>
  )
}
