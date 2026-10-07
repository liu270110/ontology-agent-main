import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { Sheet } from '@/components/sheet'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/states'
import { getCollectionSettings, listCollections, updateCollectionSettings, type KbCollectionSettings } from '../api'
import { Select } from '@/components/select'

/** 库设置抽屉（B3-Q 占位转实；画板 p-kb 顶栏入口）：
 *  分片大小（300–2000）/ 分片重叠（0–500）/ 抽取深度（standard|deep 两档 seg）
 *  / 上传后自动抽取开关（role=switch，ServerDetailSheet 同款）。GET 载入 → 脏态解锁保存
 *  → PUT 全量对象 → toast + 关抽屉。端点=api/01 §5.4 追加行。
 *  F4（联调 2026-10-06）：设置入口改**集合选择器**——抽屉内置「目标知识库」下拉，选项=
 *  GET /kb/collections 真列表（listCollections），settings 对所选集合 id 直读直写。
 *  旧实现硬挂 KB_TARGETS[0] 经 ensureCollectionId 解析（查重未命中即 POST 建库）——
 *  打开设置会凭空创建「配网运检知识库」，已废：设置只读既有集合，列表为空出空态引导，
 *  不再自动建库。 */
export function CollectionSettingsSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  // ① 集合真列表（设置目标的事实源；staleTime 内不重复发列表请求）
  const colsQuery = useQuery({
    queryKey: ['kb', 'collections'],
    queryFn: listCollections,
    enabled: open,
    staleTime: 5 * 60_000,
  })
  const cols = colsQuery.data?.data ?? []
  const [colId, setColId] = useState<string | null>(null)
  // 生效集合：本地选择仍在列表内则用之；否则回落首列（列表刷新后选择失效自愈）
  const activeId = colId && cols.some(c => c.id === colId) ? colId : (cols[0]?.id ?? null)
  // ② settings 读/写（真 id 直读——列表即事实源；未知 id 404 走错误态）
  const query = useQuery({
    queryKey: ['kb', 'collection-settings', activeId],
    queryFn: () => getCollectionSettings(activeId!),
    enabled: open && !!activeId,
  })
  const [form, setForm] = useState<KbCollectionSettings | null>(null)
  const [saving, setSaving] = useState(false)

  // 服务端值 → 表单（载入/保存/切换集合后回填均走这里，脏态随之复位）
  useEffect(() => {
    if (query.data) setForm({ ...query.data })
  }, [query.data])

  const server = query.data
  const dirty =
    !!form &&
    !!server &&
    (form.chunk_size !== server.chunk_size ||
      form.chunk_overlap !== server.chunk_overlap ||
      form.extract_prompt_level !== server.extract_prompt_level ||
      form.auto_extract !== server.auto_extract)
  const valid = !!form && form.chunk_size >= 300 && form.chunk_size <= 2000 && form.chunk_overlap >= 0 && form.chunk_overlap <= 500

  // 两段链路的加载/错误归一：列表加载失败与 settings 读取失败同走错误态
  const loading = colsQuery.isPending || (!!activeId && query.isPending)
  const error = colsQuery.isError ? colsQuery.error : query.isError ? query.error : null
  function retry() {
    if (colsQuery.isError) void colsQuery.refetch()
    else void query.refetch()
  }

  async function onSave() {
    if (!form || !dirty || !valid || saving || !activeId) return
    setSaving(true)
    try {
      await updateCollectionSettings(activeId, form)
      toast.success('库设置已保存')
      await qc.invalidateQueries({ queryKey: ['kb', 'collection-settings'] })
      onClose()
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Sheet open={open} onClose={onClose} title="库设置" width={480}>
      {loading && (
        <div className="p-5">
          <SkeletonRows rows={4} rowHeight={40} />
        </div>
      )}
      {error && (
        <div className="p-5">
          <ErrorState
            message={error instanceof Error ? error.message : undefined}
            code={error instanceof ApiError ? error.code : undefined}
            onRetry={retry}
          />
        </div>
      )}
      {/* F4：无集合时出空态引导（不再自动建库兜底——先去上传链路创建目标库） */}
      {!loading && !error && cols.length === 0 && (
        <div className="p-5">
          <EmptyState compact title="暂无知识库" desc="尚无知识库集合；上传文档时选择目标知识库即可创建，之后再回到这里配置。" />
        </div>
      )}
      {!loading && !error && cols.length > 0 && (
        <div className="space-y-4 p-5">
          {/* 目标知识库选择器（真列表；settings 随所选集合走） */}
          <div>
            <label htmlFor="cs-collection" className="text-xs font-semibold text-label-2">
              目标知识库
            </label>
            <Select
              id="cs-collection"
              className="input mt-1.5 h-8 w-full text-xs"
              aria-label="目标知识库"
              data-testid="cs-collection"
              value={activeId ?? ''}
              onChange={e => setColId(e.target.value)}
            >
              {cols.map(c => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </Select>
            <p className="mt-1 text-[11px] text-label-3">设置随所选库走；切换后未保存的修改即丢弃。</p>
          </div>
          {/* 设置表单（settings 载入后才渲染；切换集合即复位） */}
          {form && (
            <>
          <div>
            <label htmlFor="cs-chunk-size" className="text-xs font-semibold text-label-2">
              分片大小（tokens）
            </label>
            <input
              id="cs-chunk-size"
              type="number"
              min={300}
              max={2000}
              className="input mt-1.5 h-8 w-full text-xs"
              value={form.chunk_size}
              aria-label="分片大小（tokens）"
              onChange={e => setForm(f => f && { ...f, chunk_size: Number(e.target.value) })}
            />
            <p className="mt-1 text-[11px] text-label-3">300–2000；越大语义越完整，越小召回越细。</p>
          </div>
          <div>
            <label htmlFor="cs-chunk-overlap" className="text-xs font-semibold text-label-2">
              分片重叠（tokens）
            </label>
            <input
              id="cs-chunk-overlap"
              type="number"
              min={0}
              max={500}
              className="input mt-1.5 h-8 w-full text-xs"
              value={form.chunk_overlap}
              aria-label="分片重叠（tokens）"
              onChange={e => setForm(f => f && { ...f, chunk_overlap: Number(e.target.value) })}
            />
            <p className="mt-1 text-[11px] text-label-3">0–500；相邻分片重叠可避免语义截断。</p>
          </div>
          <div>
            <span className="text-xs font-semibold text-label-2">抽取深度</span>
            <div className="mt-1.5">
              <span className="seg" role="radiogroup" aria-label="抽取深度">
                <button
                  type="button"
                  role="radio"
                  aria-checked={form.extract_prompt_level === 'standard'}
                  className={`seg-btn ${form.extract_prompt_level === 'standard' ? 'on' : ''}`}
                  onClick={() => setForm(f => f && { ...f, extract_prompt_level: 'standard' })}
                >
                  标准
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={form.extract_prompt_level === 'deep'}
                  className={`seg-btn ${form.extract_prompt_level === 'deep' ? 'on' : ''}`}
                  onClick={() => setForm(f => f && { ...f, extract_prompt_level: 'deep' })}
                >
                  深度
                </button>
              </span>
            </div>
            <p className="mt-1 text-[11px] text-label-3">标准=实体/关系主链路；深度=追加属性与公理抽取（更耗时耗额度）。</p>
          </div>
          <div className="flex items-center justify-between rounded-xl border border-separator px-3 py-2.5">
            <span className="min-w-0 pr-3">
              <span className="block text-xs font-semibold">上传后自动抽取</span>
              <span className="mt-0.5 block text-[11px] text-label-3">新文档登记后自动启动七步流水线</span>
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={form.auto_extract}
              aria-label="自动抽取"
              data-testid="auto-extract-switch"
              className="relative h-5 w-9 flex-none rounded-full transition-colors"
              style={{ background: form.auto_extract ? 'var(--green)' : 'var(--surface-2)', border: '1px solid var(--separator)' }}
              onClick={() => setForm(f => f && { ...f, auto_extract: !f.auto_extract })}
            >
              <span className="absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white shadow transition-all" style={{ left: form.auto_extract ? 18 : 3 }} />
            </button>
          </div>
          <div className="hairline-t flex justify-end gap-2 pt-2">
            <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
              取消
            </button>
            <button type="button" className="btn btn-p btn-sm" data-testid="settings-save" disabled={!dirty || !valid || saving} onClick={() => void onSave()}>
              保存
            </button>
          </div>
            </>
          )}
        </div>
      )}
    </Sheet>
  )
}
