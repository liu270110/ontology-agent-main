import { FileUp, GitCompare, History, Save, ShieldCheck } from 'lucide-react'
import { TierBadge } from './shared'
import type { OntoTier, OntoVersion } from '../api'
import { Select } from '@/components/select'

/** 版本操作栏（模块化第一批自 WorkbenchPage 拆出，行为零变化）：
 *  项目名 / 版本徽标 / 版本下拉 / 草稿状态 / 导入、历史版本、对比、保存草稿、提交评审。
 *  props 只吃数据 + 回调，不发请求（02 篇 §4 纪律）。 */

export function VersionToolbar({
  projectName,
  tier,
  versions,
  version,
  onVersionChange,
  dirty,
  dirtyCount,
  draftSavedAt,
  namespace,
  canWrite,
  canReview,
  axiomMode,
  onImport,
  onHistory,
  onCompare,
  onBackToGraph,
  onSaveDraft,
  onSubmitReview,
}: {
  projectName: string
  tier: OntoTier
  versions: OntoVersion[]
  version: string
  onVersionChange: (v: string) => void
  dirty: boolean
  dirtyCount: number
  draftSavedAt: string | null
  namespace: string
  canWrite: boolean
  canReview: boolean
  axiomMode: boolean
  onImport: () => void
  onHistory: () => void
  onCompare: () => void
  onBackToGraph: () => void
  onSaveDraft: () => void
  onSubmitReview: () => void
}) {
  return (
    <div className="flex flex-none flex-wrap items-center gap-2 pb-2">
      <h1 className="truncate text-[15px] font-bold">{projectName}</h1>
      <TierBadge tier={tier} />
      <Select
        aria-label="版本选择"
        className="input h-7 w-40 text-xs"
        value={version}
        onChange={e => onVersionChange(e.target.value)}
      >
        {versions.map(v => (
          <option key={v.version} value={v.version}>
            {v.version} · {v.status === 'published' ? '已发布' : '草稿'}
          </option>
        ))}
      </Select>
      {dirty ? (
        <span className="badge b-orange">草稿 {version} · {dirtyCount} 处修改</span>
      ) : draftSavedAt ? (
        <span className="badge b-green">草稿已保存 {draftSavedAt}</span>
      ) : null}
      <span className="mono ml-2 hidden truncate text-[11px] text-label-3 xl:inline">{namespace}</span>
      <span className="ml-auto flex items-center gap-2">
        {canWrite && (
          <button type="button" className="btn btn-g btn-sm" onClick={onImport}>
            <FileUp size={12} aria-hidden /> 导入
          </button>
        )}
        {axiomMode ? (
          <button type="button" className="btn btn-g btn-sm" onClick={onBackToGraph}>
            ← 返回图谱
          </button>
        ) : (
          <>
            <button
              type="button"
              className="btn btn-g btn-sm"
              aria-label="历史版本"
              onClick={onHistory}
            >
              <History size={12} aria-hidden /> 历史版本
            </button>
            <button
              type="button"
              className="btn btn-g btn-sm"
              aria-label="对比版本"
              onClick={onCompare}
            >
              <GitCompare size={12} aria-hidden /> 对比
            </button>
          </>
        )}
        {canWrite && (
          <button
            type="button"
            className="btn btn-g btn-sm"
            data-testid="save-draft"
            onClick={onSaveDraft}
          >
            <Save size={12} aria-hidden /> 保存草稿
          </button>
        )}
        {canReview && canWrite && (
          <button type="button" className="btn btn-p btn-sm" data-testid="open-submit-review" onClick={onSubmitReview}>
            <ShieldCheck size={12} aria-hidden /> 提交评审
          </button>
        )}
      </span>
    </div>
  )
}
