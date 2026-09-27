import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, Star } from 'lucide-react'
import { Sheet } from '@/components/sheet'
import { useAuthStore } from '@/stores/auth-store'
import { getPlugin, type MarketPlugin } from '../api'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** IX-MKT-01 插件详情抽屉（26 篇 §9.1；画板 ix-mkt-01）：520px 右侧——
 *  图标+名称+开发者+评分；Tab 内：README 渲染 / 版本列表（semver + 更新日志）
 *  / 权限清单（scope 分组，高危 danger 徽标）；底部「安装」（admin/curator 可见，
 *  member 显示「联系管理员」）。数据 GET /plugins/{id}（§5.6）。 */

const TABS = ['README', '版本', '权限清单'] as const

export function PluginDetailSheet({
  pluginId,
  onClose,
  onInstall,
}: {
  pluginId: string | null
  onClose: () => void
  onInstall: (p: MarketPlugin) => void
}) {
  const canInstall = useAuthStore(s => s.hasAnyRole(['admin', 'curator', 'super_admin']))
  const [tab, setTab] = useState<(typeof TABS)[number]>('README')

  const { data: plugin } = useQuery({
    queryKey: ['market', 'detail', pluginId],
    queryFn: () => getPlugin(pluginId!),
    enabled: !!pluginId,
  })

  const latest = useMemo(() => plugin?.versions.find(v => v.latest) ?? null, [plugin])

  if (!pluginId || !plugin) return null
  const scopeGroups = useMemoGroup(plugin)

  return (
    <Sheet open onClose={onClose} title={`${plugin.name}`} width={520}>
      <div className="px-5 py-4">
        <div className="flex items-start gap-3">
          <span className="flex h-11 w-11 flex-none items-center justify-center rounded-xl text-[18px]" style={{ background: 'var(--accent-soft)' }}>
            🧩
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <b className="text-[15px]">{plugin.name}</b>
              {plugin.certified && <span className="badge b-blue">官方认证</span>}
            </div>
            <div className="mt-0.5 text-[11px] text-label-3">开发者：{plugin.developer} · 分类：{plugin.category}</div>
            <div className="mt-1 flex items-center gap-1 text-[11.5px]">
              <Star size={12} className="text-orange" aria-hidden />
              <b>{plugin.rating.toFixed(1)}</b>
              <span className="text-label-3">· {plugin.ratings_count} 评价 · {plugin.installs} 安装</span>
            </div>
          </div>
        </div>

        {/* 三 Tab */}
        <div className="hairline-b mt-3 flex gap-4">
          {TABS.map(t => (
            <button
              key={t}
              type="button"
              role="tab"
              aria-selected={tab === t}
              className={`-mb-px border-b-2 pb-2 text-[12.5px] ${tab === t ? 'border-accent font-semibold text-accent' : 'border-transparent text-label-2'}`}
              data-testid={`mkt-detail-tab-${t}`}
              onClick={() => setTab(t)}
            >
              {t}
              {t === '版本' && plugin.versions.length > 0 && `（${plugin.versions.length}）`}
              {t === '权限清单' && `（${plugin.scopes.length}）`}
            </button>
          ))}
        </div>

        <div className="mt-3 min-h-[180px]">
          {tab === 'README' && (
            <div className="markdown text-[12px] leading-6" data-testid="mkt-readme">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{plugin.readme}</ReactMarkdown>
            </div>
          )}

          {tab === '版本' && (
            <div className="space-y-2" data-testid="mkt-versions">
              {plugin.versions.map(v => (
                <div key={v.version} className="rounded-xl border border-separator px-3 py-2.5">
                  <div className="flex items-center gap-2">
                    <b className="mono text-[12.5px]">{v.version}</b>
                    {v.latest && <span className="badge b-green">最新</span>}
                    <span className="ml-auto text-[10.5px] text-label-3">{v.released_at}</span>
                  </div>
                  <div className="mt-1 text-[11px] text-label-2">{v.note}</div>
                </div>
              ))}
            </div>
          )}

          {tab === '权限清单' && (
            <div className="space-y-3" data-testid="mkt-scopes">
              {scopeGroups.map(([group, rows]) => (
                <div key={group}>
                  <div className="text-[11px] font-semibold text-label-3">{group}</div>
                  <div className="mt-1.5 space-y-1.5">
                    {rows.map(s => (
                      <div
                        key={s.scope}
                        className="rounded-xl border px-3 py-2"
                        style={s.danger ? { background: 'var(--red-soft)', borderColor: 'var(--red)' } : { borderColor: 'var(--separator)' }}
                      >
                        <div className="flex items-center gap-2">
                          <span className="mono text-[12px] font-semibold">{s.scope}</span>
                          <span className={`badge ${s.access === '只读' ? 'b-gray' : s.danger ? 'b-red' : 'b-orange'}`}>
                            {s.access}
                          </span>
                          {s.danger && <span className="badge b-red">高危</span>}
                        </div>
                        <div className="mt-0.5 text-[10.5px] text-label-2">{s.desc}</div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 底部安装条 */}
      <div className="hairline-t sticky bottom-0 px-5 py-3" style={{ background: 'var(--surface)' }}>
        {canInstall ? (
          <button
            type="button"
            className="btn btn-p w-full"
            data-testid="mkt-install-open"
            onClick={() => onInstall(plugin)}
          >
            <Download size={13} aria-hidden /> 安装 {latest ? latest.version : ''}
          </button>
        ) : (
          <div className="rounded-xl border border-separator bg-surface-2 px-3 py-2.5 text-center text-[11.5px] text-label-2">
            <b>member 视角：</b>按钮替换为「联系管理员」——向租户管理员发送安装申请（附带插件与所选版本），安装动作仍由管理员在 IX-MKT-02 向导中完成。
            <button type="button" className="btn btn-g btn-sm ml-2" onClick={() => onClose()}>
              联系管理员
            </button>
          </div>
        )}
      </div>
    </Sheet>
  )
}

/** scope 按资源段分组（kb.* → 知识库；mcp.* → 工具调用；与画板 Tab 3 同构） */
function useMemoGroup(plugin: MarketPlugin): [string, MarketPlugin['scopes']][] {
  const map = new Map<string, MarketPlugin['scopes']>()
  for (const s of plugin.scopes) {
    const group = s.scope.split('.')[0] === 'mcp' ? '工具调用' : s.scope.startsWith('kb') ? '知识库' : '其他'
    if (!map.has(group)) map.set(group, [])
    map.get(group)!.push(s)
  }
  return [...map.entries()]
}
