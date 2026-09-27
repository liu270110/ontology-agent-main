import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Check, GitCompare, RotateCcw, ShieldCheck, X } from 'lucide-react'
import {
  approveChangeset, diffVersions, getProject, listChangesets, publishChangeset, rejectChangeset, rollbackChangeset,
  type DiffRow, type ReviewDecision,
} from '../api'
import { CsStatusBadge, relativeTime } from '../components/shared'
import { CompareSelectorDialog, PublishDialog, RejectDialog, RollbackDialog, diffRowStyle } from '../components/VersionDialogs'

/** 版本评审 /ontology/:projectId/versions（宿主画框 p-versions；26 篇 §7.1 IX-VR-01~05）：
 *  changeset 列表（编号/提交人/状态徽标/时间）+ 对比选择器 + 三元组 diff 逐条决策
 *  （+绿/−红/~黄行 + 每行接受/拒绝 + 批量栏 + 决策行灰化打勾；决策随 approve 的
 *  decisions[] 一次性提交——边界审计修正）+ 发布五步进度 + 驳回 + 回滚（ROLLBACK 解锁）。 */

export function VersionsPage() {
  const { projectId = '' } = useParams()
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const activeCs = searchParams.get('cs')
  const compareOpen = searchParams.get('compare') === '1'

  const detail = useQuery({ queryKey: ['ontology', 'detail', projectId], queryFn: () => getProject(projectId) })
  const changesetsQ = useQuery({ queryKey: ['ontology', projectId, 'changesets'], queryFn: () => listChangesets(projectId) })
  const changesets = changesetsQ.data?.items ?? []
  const currentCs = useMemo(() => changesets.find(c => c.id === activeCs) ?? null, [changesets, activeCs])
  const versions = detail.data?.versions ?? []

  // ---- IX-VR-02 diff 逐条决策（决策收集，approve 时随 decisions[] 一次性提交） ----
  const [decisions, setDecisions] = useState<Record<string, 'accept' | 'reject'>>({})
  const diff = useQuery({
    queryKey: ['ontology', projectId, 'diff', currentCs?.base_version, currentCs?.status === 'published' ? currentCs.base_version : versions[0]?.version],
    queryFn: () => diffVersions(projectId, versions[1]?.version ?? 'v2.0', versions[0]?.version ?? 'v2.2-draft'),
    enabled: !!activeCs,
  })
  useEffect(() => setDecisions({}), [activeCs])
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
      setSearchParams({ cs: activeCs ?? '' })
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
  const rollbackM = useMutation({
    mutationFn: (v: { reason: string; target: string }) => rollbackChangeset(projectId, activeCs ?? '', { reason: v.reason, target_version: v.target }),
    onSuccess: (_d, v) => {
      toast.success('回滚请求已创建', { description: `逆向变更单进入评审（目标 ${v.target}）` })
      void qc.invalidateQueries({ queryKey: ['ontology', projectId, 'changesets'] })
    },
  })

  const currentVersion = detail.data?.head_version ?? 'v2.1'

  return (
    <div className="mx-auto max-w-[1080px]">
      {/* 页头 */}
      <div className="flex flex-wrap items-center gap-2">
        <Link to="/ontology" className="text-xs text-label-3 hover:text-accent">本体项目</Link>
        <span className="text-label-3">/</span>
        <h1 className="text-lg font-bold">{detail.data?.name ?? '—'} · 版本与评审</h1>
        <span className="badge b-green">v{currentVersion.replace(/^v/, '')} 已发布</span>
        {currentCs && (
          <button
            type="button"
            className="btn btn-g btn-sm ml-auto"
            onClick={() => setRollbackTarget(versions[1]?.version ?? 'v2.0')}
            data-testid="open-rollback"
          >
            <RotateCcw size={12} aria-hidden /> 回滚
          </button>
        )}
        <button type="button" className="btn btn-g btn-sm" data-testid="open-compare" onClick={() => setSearchParams({ compare: '1' })}>
          <GitCompare size={12} aria-hidden /> 对比
        </button>
      </div>

      {/* changeset 列表 */}
      <div className="card mt-4 !px-4 !py-2">
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
            {changesets.map(cs => (
              <tr key={cs.id} className={cs.id === activeCs ? 'sel' : undefined}>
                <td className="mono font-semibold">{cs.id}</td>
                <td>{cs.title}</td>
                <td className="text-dim text-[12px]">{cs.submitter}</td>
                <td><CsStatusBadge status={cs.status} /></td>
                <td className="text-[11px]">
                  <span className="text-green">+{cs.stats.add}</span>{' '}
                  <span className="text-red">−{cs.stats.del}</span>{' '}
                  <span className="text-orange">~{cs.stats.mod}</span>
                </td>
                <td className="text-dim text-[12px]">{relativeTime(cs.updated_at)}</td>
                <td className="text-right">
                  <button type="button" className="btn btn-g btn-sm" data-testid={`review-${cs.id}`} onClick={() => setSearchParams({ cs: cs.id })}>
                    {cs.status === 'in_review' ? '进入评审' : '查看 Diff'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Diff 逐条决策视图（IX-VR-02） */}
      {activeCs && currentCs && (
        <div className="card mt-4" data-testid="diff-view">
          <div className="card-h !mb-2">
            <h3 className="!text-[14px]">
              {currentCs.id} · {currentCs.title}
            </h3>
            <CsStatusBadge status={currentCs.status} />
            <span className="text-[11.5px] text-label-3">
              提交人 {currentCs.submitter} · {relativeTime(currentCs.updated_at)}
            </span>
            <span className="badge b-green ml-auto">SHACL 校验通过</span>
            <button type="button" className="btn btn-g btn-sm" onClick={() => setSearchParams({})}>
              关闭
            </button>
          </div>

          {/* 批量决策栏 */}
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

          {/* Diff 行：+绿/−红/~黄；决策后灰化打勾 */}
          <div className="mt-2 space-y-1.5">
            {rows.map(r => {
              const st = diffRowStyle(r.op)
              const d = decisions[r.id]
              return (
                <div
                  key={r.id}
                  className="flex items-center gap-2 rounded-lg px-3 py-2 text-[12px] transition-opacity"
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
                    ) : (
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

          {/* 决策操作条 */}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <input
              className="input h-8 max-w-[380px] flex-1 text-[12px]"
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
        </div>
      )}

      {/* 弹窗组 */}
      <CompareSelectorDialog
        open={compareOpen}
        versions={versions}
        onClose={() => setSearchParams({})}
        onCompare={(base, target) => {
          setSearchParams({ cs: activeCs ?? changesets[0]?.id ?? '' })
          toast.success(`对比视图：${base} → ${target}`, { description: 'Diff 按行决策（IX-VR-02）' })
        }}
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
