import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, Puzzle, Star, Upload } from 'lucide-react'
import { listPlugins, type MarketPlugin } from '../api'
import { PluginDetailSheet } from '../components/PluginDetailSheet'
import { InstallWizard } from '../components/InstallWizard'
import { SubmitPluginModal } from '../components/SubmitPluginModal'

/** /marketplace 插件市场（宿主画框 p-market；26 篇 §9.1）：
 *  卡片网格（图标/名称/评分/权限 scope 数）+ 分类 seg 过滤 + 搜索
 *  + IX-MKT-01 详情抽屉 + IX-MKT-02 安装向导 + IX-MKT-03 上架申请。
 *  roles=全员可见（routes meta.roles=AUTHED）；安装动作 admin/curator。 */

const FILTERS = ['全部', '官方认证', '社区', '已安装'] as const

export function MarketPage() {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>('全部')
  const [keyword, setKeyword] = useState('')
  const [detailId, setDetailId] = useState<string | null>(null)
  const [installPlugin, setInstallPlugin] = useState<MarketPlugin | null>(null)
  const [submitOpen, setSubmitOpen] = useState(false)

  const { data, isLoading } = useQuery({ queryKey: ['market', 'list'], queryFn: listPlugins })
  const plugins = useMemo(() => {
    let items = data?.items ?? []
    if (filter === '官方认证') items = items.filter(p => p.certified)
    if (filter === '社区') items = items.filter(p => !p.certified)
    if (filter === '已安装') items = items.filter(p => p.installed)
    if (keyword.trim()) {
      const k = keyword.trim().toLowerCase()
      items = items.filter(p => p.name.toLowerCase().includes(k) || p.summary.toLowerCase().includes(k))
    }
    return items
  }, [data, filter, keyword])

  return (
    <div className="mx-auto max-w-[1080px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">插件市场</h1>
        <span className="text-xs text-label-3">上架走 review_workflow · 安装走 scope 逐项授权（候选非成品）</span>
        <button type="button" className="btn btn-g btn-sm ml-auto" onClick={() => setSubmitOpen(true)} data-testid="mkt-submit-open">
          <Upload size={13} aria-hidden /> 上架新插件
        </button>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <div className="seg" role="tablist" aria-label="插件市场过滤">
          {FILTERS.map(f => (
            <button
              key={f}
              type="button"
              role="tab"
              aria-selected={filter === f}
              className={`seg-btn ${filter === f ? 'on' : ''}`}
              onClick={() => setFilter(f)}
            >
              {f}
            </button>
          ))}
        </div>
        <input
          className="input ml-auto !h-8 max-w-[220px]"
          placeholder="搜索插件、技能"
          aria-label="搜索插件"
          value={keyword}
          onChange={e => setKeyword(e.target.value)}
        />
      </div>

      {/* 卡片网格 */}
      <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {plugins.map(p => (
          <button
            type="button"
            key={p.id}
            className="card flex flex-col !p-4 text-left transition-colors hover:border-accent"
            data-testid={`plugin-card-${p.id}`}
            onClick={() => setDetailId(p.id)}
          >
            <div className="flex items-center gap-2">
              <span className="flex h-9 w-9 flex-none items-center justify-center rounded-xl text-[16px]" style={{ background: 'var(--accent-soft)' }}>
                🧩
              </span>
              <div className="min-w-0">
                <div className="flex items-center gap-1.5">
                  <b className="truncate text-[13.5px]">{p.name}</b>
                  {p.certified && <span className="badge b-blue">官方认证</span>}
                </div>
                <div className="truncate text-[10.5px] text-label-3">{p.developer}</div>
              </div>
            </div>
            <p className="mt-2 line-clamp-2 min-h-8 text-[11.5px] leading-4 text-label-2">{p.summary}</p>
            <div className="hairline-t mt-3 flex items-center gap-2 pt-2.5 text-[11px] text-label-3">
              <span className="flex items-center gap-1">
                <Star size={11} className="text-orange" aria-hidden />
                {p.rating.toFixed(1)}
              </span>
              <span>· {p.scopes.length} 项 scope</span>
              {p.installed && <span className="badge b-green">已安装</span>}
              <span className="ml-auto flex items-center gap-1 text-accent">
                <Download size={11} aria-hidden /> {p.installs}
              </span>
            </div>
          </button>
        ))}
      </div>

      {isLoading && <div className="empty mt-6"><div className="t">加载中…</div></div>}
      {!isLoading && plugins.length === 0 && (
        <div className="empty mt-6">
          <Puzzle size={28} aria-hidden />
          <div className="t">没有匹配的插件</div>
          <div className="d">换个分类或关键词，或「上架新插件」提交审核。</div>
        </div>
      )}

      <PluginDetailSheet
        pluginId={detailId}
        onClose={() => setDetailId(null)}
        onInstall={p => {
          setDetailId(null)
          setInstallPlugin(p)
        }}
      />
      <InstallWizard plugin={installPlugin} onClose={() => setInstallPlugin(null)} />
      <SubmitPluginModal open={submitOpen} onClose={() => setSubmitOpen(false)} />
    </div>
  )
}
