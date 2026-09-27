import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  createColumnHelper,
  flexRender,
  tableFeatures,
  useTable,
} from '@tanstack/react-table'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Eye, Filter, RotateCcw, Search, Settings, Trash2, Upload } from 'lucide-react'
import { toast } from 'sonner'
import { useAuthStore } from '@/stores/auth-store'
import { listDocuments, type KbDocument } from '../api'
import { FileTypeBadge, StatusBadge, formatSize, relativeTime } from '../components/shared'
import { UploadDialog } from '../components/UploadDialog'
import { ChunkPreviewSheet } from '../components/ChunkPreviewSheet'
import { DeleteDocDialog } from '../components/DeleteDocDialog'
import { RetryDialog } from '../components/RetryDialog'

/** /kb 文档管理页（宿主画框 p-kb；26 篇 §5.1 IX-KB-01~04 + 30 篇 §2 S3-A）：
 *  规模统计带 + 文档表（@tanstack/react-table：名称/类型/大小/切片/流水线状态/更新/操作）
 *  + 筛选（类型/状态）+ 搜索；顶栏「库设置」「回收站」占位 Toast（F-12，随 X13 交付）。
 *  轮询：存在 pending/extracting 行时 1.5s 刷新（IX-KB-01 行内流水线推进）。 */

/** v9 新 API：features 静态声明（tableFeatures）+ helper 双泛型；模块级声明避免每帧重建 */
const kbFeatures = tableFeatures({})
const col = createColumnHelper<typeof kbFeatures, KbDocument>()

const STATUS_FILTERS = [
  { v: 'all', label: '全部状态' },
  { v: 'indexed', label: '已入库' },
  { v: 'extracting', label: '抽取中' },
  { v: 'pending', label: '待抽取' },
  { v: 'failed', label: '失败' },
] as const

export function DocumentsPage() {
  const qc = useQueryClient()
  const can = useAuthStore(s => s.can)
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('all')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [uploadOpen, setUploadOpen] = useState(false)
  const [previewDoc, setPreviewDoc] = useState<KbDocument | null>(null)
  const [deleteDoc, setDeleteDoc] = useState<KbDocument | null>(null)
  const [retryDoc, setRetryDoc] = useState<KbDocument | null>(null)

  const docsQuery = useQuery({
    queryKey: ['kb', 'documents'],
    queryFn: listDocuments,
    refetchInterval: query => {
      const items = (query.state.data as { items: KbDocument[] } | undefined)?.items ?? []
      return items.some(d => d.status === 'pending' || d.status === 'extracting') ? 1500 : false
    },
  })
  const docs = docsQuery.data?.items ?? []

  const filtered = useMemo(
    () =>
      docs.filter(
        d =>
          (typeFilter === 'all' || d.doc_type === typeFilter) &&
          (statusFilter === 'all' || d.status === statusFilter) &&
          d.name.toLowerCase().includes(search.trim().toLowerCase()),
      ),
    [docs, typeFilter, statusFilter, search],
  )

  // v9：col.columns 保留逐列 TValue 推断，勿手注 ColumnDef 泛型（避免 RowData 退化）
  const columns = col.columns([
    col.accessor('name', {
      header: '文档名称',
      cell: info => <span className="block max-w-[280px] truncate font-semibold">{info.getValue()}</span>,
    }),
    col.accessor('doc_type', { header: '类型', cell: info => <FileTypeBadge type={info.getValue()} /> }),
    col.accessor('size_bytes', { header: '大小', cell: info => <span className="mono dim">{formatSize(info.getValue())}</span> }),
    col.accessor('chunk_count', { header: '切片', cell: info => <span className="mono dim">{info.getValue() || '—'}</span> }),
    col.display({
      id: 'pipeline',
      header: '流水线',
      cell: ({ row }) => <StatusBadge doc={row.original} />,
    }),
    col.accessor('updated_at', { header: '更新', cell: info => <span className="text-dim text-[12px]">{relativeTime(info.getValue())}</span> }),
    col.display({
      id: 'actions',
      header: '',
      cell: ({ row }) => (
        <div className="flex items-center gap-1.5 whitespace-nowrap">
          {row.original.chunk_count > 0 && (
            <button type="button" className="btn btn-g btn-sm" aria-label={`预览 ${row.original.name}`} onClick={() => setPreviewDoc(row.original)}>
              <Eye size={12} aria-hidden /> 预览
            </button>
          )}
          {(row.original.status === 'pending' || row.original.status === 'failed') && (
            <button type="button" className="btn btn-s btn-sm" onClick={() => setRetryDoc(row.original)}>
              <RotateCcw size={12} aria-hidden /> 重抽
            </button>
          )}
          <button
            type="button"
            className="btn btn-g btn-sm"
            aria-label={`删除 ${row.original.name}`}
            onClick={() => setDeleteDoc(row.original)}
          >
            <Trash2 size={12} aria-hidden />
          </button>
        </div>
      ),
    }),
  ])

  const table = useTable({
    features: kbFeatures,
    data: filtered,
    columns,
    getRowId: d => d.id,
  })

  // 规模统计带（画板 grid4 口径，由列表聚合；向量≈切片 1:1，后端聚合端点随 X13 一起登记）
  const stats = useMemo(
    () => ({
      docs: docs.length,
      chunks: docs.reduce((s, d) => s + d.chunk_count, 0),
      today: docs.filter(d => d.indexed_today).length,
    }),
    [docs],
  )

  return (
    <div className="mx-auto max-w-[1080px]">
      {/* 页头（画板 page-h：标题 + 筛选/上传；F-12 库设置/回收站占位） */}
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">知识库文档</h1>
        <span className="ml-auto flex items-center gap-2">
          <button
            type="button"
            className="btn btn-g btn-sm"
            onClick={() => toast.info('库设置随 X13 文档预览服务一起交付（F-12 占位）')}
          >
            <Settings size={12} aria-hidden /> 库设置
          </button>
          <button
            type="button"
            className="btn btn-g btn-sm"
            onClick={() => toast.info('回收站随 X13 文档预览服务一起交付（F-12 占位）')}
          >
            <Trash2 size={12} aria-hidden /> 回收站
          </button>
        </span>
      </div>
      <p className="mt-1 text-xs text-label-3">文档为中心的知识库视图；七步流水线进度在抽取审核台展开。</p>

      {/* 规模统计带 */}
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          { num: String(stats.docs), label: '文档' },
          { num: stats.chunks.toLocaleString(), label: '切片' },
          { num: stats.chunks.toLocaleString(), label: '向量（Milvus）' },
          { num: `+${stats.today}`, label: '今日入库', accent: true },
        ].map(s => (
          <div key={s.label} className="card px-4 py-3" style={s.accent ? { border: '1.5px solid var(--accent)' } : undefined}>
            <div className={`text-2xl font-bold ${s.accent ? 'text-accent' : ''}`}>{s.num}</div>
            <div className="mt-0.5 text-[11px] text-label-3">{s.label}</div>
          </div>
        ))}
      </div>

      {/* 筛选 / 搜索 / 上传 */}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-label-3" aria-hidden />
          <input
            className="input h-8 w-56 pl-8 text-[12.5px]"
            placeholder="搜索文档名…"
            aria-label="搜索文档"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <span className="badge b-blue">
          <Filter size={11} aria-hidden />
          <select
            className="bg-transparent text-[11px] font-semibold outline-none"
            aria-label="按类型筛选"
            value={typeFilter}
            onChange={e => setTypeFilter(e.target.value)}
          >
            {['all', 'PDF', 'Word', 'Excel', 'CSV', '图片'].map(t => (
              <option key={t} value={t}>
                {t === 'all' ? '全部类型' : t}
              </option>
            ))}
          </select>
        </span>
        <span className="badge b-blue">
          <select
            className="bg-transparent text-[11px] font-semibold outline-none"
            aria-label="按状态筛选"
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}
          >
            {STATUS_FILTERS.map(s => (
              <option key={s.v} value={s.v}>
                {s.label}
              </option>
            ))}
          </select>
        </span>
        {can('kb:write') && (
          <button type="button" className="btn btn-p btn-sm ml-auto" onClick={() => setUploadOpen(true)}>
            <Upload size={12} aria-hidden /> 上传文档
          </button>
        )}
      </div>

      {/* 文档表 */}
      <div className="card mt-4 overflow-x-auto px-4 py-2">
        <table className="tbl w-full">
          <thead>
            {table.getHeaderGroups().map(hg => (
              <tr key={hg.id}>
                {hg.headers.map(h => (
                  <th key={h.id}>{h.isPlaceholder ? null : flexRender(h.column.columnDef.header, h.getContext())}</th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map(row => (
              <tr key={row.id}>
                {row.getAllCells().map(cell => (
                  <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                ))}
              </tr>
            ))}
            {table.getRowModel().rows.length === 0 && (
              <tr>
                <td colSpan={7}>
                  <div className="empty">
                    <div className="t">{docsQuery.isLoading ? '加载中…' : '没有匹配的文档'}</div>
                    <div className="d">调整筛选条件，或上传新文档开始抽取。</div>
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* 跳转 chips（26 篇矩阵联动） */}
      <div className="mt-3 flex gap-2 text-[11.5px]">
        <Link to="/kb/review" className="rounded-full border border-separator px-3 py-1 text-label-2 hover:border-accent hover:text-accent">
          流水线进行中 → 抽取审核台
        </Link>
      </div>

      {/* IX-KB-01~04 态宿主 */}
      <UploadDialog open={uploadOpen} onClose={() => setUploadOpen(false)} onUploaded={() => void qc.invalidateQueries({ queryKey: ['kb', 'documents'] })} />
      <ChunkPreviewSheet doc={previewDoc} onClose={() => setPreviewDoc(null)} />
      <DeleteDocDialog doc={deleteDoc} onClose={() => setDeleteDoc(null)} onDeleted={() => void qc.invalidateQueries({ queryKey: ['kb', 'documents'] })} />
      <RetryDialog doc={retryDoc} onClose={() => setRetryDoc(null)} onQueued={() => void qc.invalidateQueries({ queryKey: ['kb', 'documents'] })} />
    </div>
  )
}
