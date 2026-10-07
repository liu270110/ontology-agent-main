import { useEffect, useMemo, useState } from 'react'
import { useDropzone } from 'react-dropzone'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronDown, FileUp, Loader2, RotateCcw, Trash2, Upload } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { describeError } from '@/lib/errors'
import { listCollections, startPipeline, uploadDocumentText } from '../api'
import { formatSize } from './shared'
import { Select } from '@/components/select'

/** IX-KB-01 上传文档弹窗（Modal 600px；画板 p-kb / 26 篇 §5.1）：
 *  拖拽区（react-dropzone，拖入高亮 + 点击选择）→ 多文件队列（名/大小/格式徽标/移除/逐行状态）
 *  → 解析选项折叠区（切片策略/抽取开关/目标知识库）→「上传并抽取」（汇总大小校验）。
 *  提交=循环单文件调用（每文件一行进度：排队→上传中→已登记/失败）：
 *  ① 目标知识库 → collection_id（F4 live 化：下拉=GET /kb/collections 真列表（列表空/失败
 *    回落 KB_TARGETS 缺省），提交仍经 api.ensureCollectionId 按名查重+按需建库兜底）
 *  ② POST /kb/documents 登记（live 实测=M2 JSON 内容直传 {collection_id,title,content,mime_type}，
 *    checksum 幂等；二进制类文件文本语义降级直传，MinIO 预签名直传随 M3——R18）
 *  ③（抽取开关开启时）POST pipeline/start 建任务（live 202 {document_id,accepted}）。
 *  成功→关弹窗+列表失效刷新（行内状态轮询由 DocumentsPage 1.5s 条件轮询承接）；
 *  失败→错误横幅（lib/errors 映射文案）+ 该文件行标失败可单行重试。 */

const MAX_TOTAL = 100 * 1024 * 1024 // 汇总大小校验上限（100MB）
/** F4（联调 2026-10-06）：目标知识库下拉主源=GET /kb/collections 真列表（listCollections），
 *  本常量退役为「列表空/失败」回落缺省（提交仍经 ensureCollectionId 按名查重兜底建库，
 *  行为同前）。导出单源仅保留回落用途——设置入口已改集合选择器，不再挂 KB_TARGETS[0]。 */
export const KB_TARGETS = ['配网运检知识库', '停电分析知识库', '抢修工单知识库']
const SLICE_STRATEGIES = ['段落', '语义', '固定长度'] as const

type FileRowState = 'queued' | 'uploading' | 'done' | 'failed'

interface QueuedFile {
  key: string
  file: File
  state: FileRowState
  error?: string
}

/** 文件 → 文本（live 实测 M2 JSON 内容直传）。Blob.text() 优先；FileReader 兜底
 *  （jsdom 的 File 未实现 .text()，单测环境兼容）。 */
function readFileText(file: File): Promise<string> {
  if (typeof file.text === 'function') return file.text()
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => reject(reader.error ?? new Error('文件读取失败'))
    reader.readAsText(file)
  })
}

export function UploadDialog({ open, onClose, onUploaded }: { open: boolean; onClose: () => void; onUploaded: () => void }) {
  const [queue, setQueue] = useState<QueuedFile[]>([])
  const [strategy, setStrategy] = useState<(typeof SLICE_STRATEGIES)[number]>('段落')
  const [extract, setExtract] = useState(true)
  const [kbTarget, setKbTarget] = useState(KB_TARGETS[0])
  const [optionsOpen, setOptionsOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [banner, setBanner] = useState<string | null>(null)

  // F4：目标知识库下拉 live 化——GET /kb/collections 真列表为主源；列表空/失败回落
  // KB_TARGETS（提交路径 ensureCollectionId 按名查重+按需建库兜底不变）
  const colsQuery = useQuery({
    queryKey: ['kb', 'collections'],
    queryFn: listCollections,
    enabled: open,
    staleTime: 30_000,
  })
  const kbOptions = useMemo(() => {
    const names = (colsQuery.data?.data ?? []).map(c => c.name)
    return names.length > 0 ? names : KB_TARGETS
  }, [colsQuery.data])
  // 列表加载后所选目标不在选项内（初值回落名已被真库取代等）→ 自愈到首个可选
  useEffect(() => {
    if (!kbOptions.includes(kbTarget)) setKbTarget(kbOptions[0])
  }, [kbOptions, kbTarget])

  const totalSize = useMemo(() => queue.reduce((s, q) => s + q.file.size, 0), [queue])
  const overSize = totalSize > MAX_TOTAL

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    multiple: true,
    // 接受：PDF / Word / Excel / CSV / 图片（与 mock doc_type 推断一致）
    accept: {
      'application/pdf': ['.pdf'],
      'application/msword': ['.doc'],
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document': ['.docx'],
      'application/vnd.ms-excel': ['.xls'],
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'],
      'text/csv': ['.csv'],
      'image/png': ['.png'],
      'image/jpeg': ['.jpg', '.jpeg'],
    },
    onDrop: accepted => {
      setQueue(q => [
        ...q,
        ...accepted.map(f => ({
          key: `${f.name}-${f.size}-${Math.random().toString(36).slice(2, 7)}`,
          file: f,
          state: 'queued' as const,
        })),
      ])
      setBanner(null)
    },
  })

  function patchRow(key: string, patch: Partial<Omit<QueuedFile, 'key' | 'file'>>) {
    setQueue(list => list.map(q => (q.key === key ? { ...q, ...patch } : q)))
  }

  /** 上传一批（首提=全部排队行；行内重试=单个失败行）。循环单文件调用，逐行推进状态；
   *  有成功即 onUploaded()（列表失效刷新 → pending/extracting 行由页面 1.5s 轮询推进）；
   *  全部成功→清队列关弹窗；存在失败→横幅汇总 + 失败行留弹窗内可重试。 */
  async function runUpload(targets: QueuedFile[]) {
    if (targets.length === 0 || overSize || busy) return
    setBusy(true)
    setBanner(null)
    targets.forEach(t => patchRow(t.key, { state: 'uploading', error: undefined }))
    let ok = 0
    const failures: { name: string; message: string }[] = []
    for (const t of targets) {
      try {
        // live 实测 M2 JSON 内容直传：读文本上传（二进制类文件降级语义，见 api.ts R18 注）
        const text = await readFileText(t.file)
        if (!text.trim()) throw new Error('文件内容为空，无法登记')
        const doc = await uploadDocumentText({
          collectionName: kbTarget,
          title: t.file.name,
          content: text,
          mime_type: t.file.type || 'application/octet-stream',
        })
        // 建抽取任务（§5.2/§5.4 pipeline/start；不开抽取时仅登记；幂等命中 created=false 照常建任务）
        if (extract) await startPipeline(doc.id)
        ok++
        patchRow(t.key, { state: 'done', error: undefined })
      } catch (e) {
        const message = describeError(e)
        failures.push({ name: t.file.name, message })
        patchRow(t.key, { state: 'failed', error: message })
      }
    }
    setBusy(false)
    if (ok > 0) onUploaded()
    if (failures.length === 0) {
      toast.success(extract ? `已上传 ${ok} 个文档并开始抽取` : `已登记 ${ok} 个文档（未开抽取）`)
      setQueue([])
      onClose()
    } else {
      setBanner(`${failures.length} 个文件上传失败：${failures[0].message}${failures.length > 1 ? `（等 ${failures.length} 个）` : ''}`)
      if (ok > 0) toast.info(`已上传 ${ok} 个文档；其余 ${failures.length} 个失败，可在行内重试`)
    }
  }

  function onUploadAndExtract() {
    void runUpload(queue.filter(q => q.state === 'queued' || q.state === 'failed'))
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="上传文档"
      width={600}
      footer={
        <>
          <span className="mr-auto text-[11px] text-label-3">
            共 {queue.length} 个文件 · {formatSize(totalSize)}
            {overSize && <b className="ml-1 text-red">超出 100MB 上限</b>}
          </span>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            data-testid="upload-submit"
            className="btn btn-p btn-sm"
            disabled={queue.length === 0 || overSize || busy}
            onClick={onUploadAndExtract}
          >
            {busy ? <Loader2 size={13} className="animate-spin" aria-hidden /> : <Upload size={13} aria-hidden />}
            上传并抽取
          </button>
        </>
      }
    >
      {/* 错误横幅（lib/errors 映射文案；部分成功时保留弹窗供失败行重试） */}
      {banner && (
        <div
          role="alert"
          data-testid="upload-error"
          className="mt-3 flex items-start gap-1.5 rounded-lg bg-[var(--red-soft)] px-3 py-2 text-xs font-medium text-red"
        >
          <span>{banner}</span>
        </div>
      )}

      {/* 拖拽区（拖入高亮 + 点击选择） */}
      <div
        {...getRootProps()}
        className={`upzone mt-3 cursor-pointer ${isDragActive ? 'drag' : ''}`}
        aria-label="拖拽文件到此处或点击选择"
      >
        <input {...getInputProps()} data-testid="upload-input" />
        <FileUp size={22} className="mx-auto mb-1 text-label-3" aria-hidden />
        <p className="text-[13px] font-medium">拖拽文件到此处，或点击选择</p>
        <p className="mt-1 text-[11px] text-label-3">支持 PDF / Word / Excel / CSV / 图片 · 单次总量 ≤ 100MB</p>
      </div>

      {/* 多文件队列（逐行状态：排队 → 上传中 → 已登记 / 失败可重试） */}
      {queue.length > 0 && (
        <ul className="mt-3 space-y-1.5" aria-label="上传队列">
          {queue.map(q => (
            <li key={q.key} className="flex items-center gap-2 rounded-lg border border-separator bg-surface-2 px-3 py-2">
              <span className="badge b-gray">{(q.file.name.split('.').pop() ?? '?').toUpperCase()}</span>
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium" title={q.error}>
                {q.file.name}
                {q.state === 'failed' && <b className="ml-1.5 text-red">失败 · {q.error}</b>}
              </span>
              {q.state === 'uploading' && (
                <span className="flex flex-none items-center gap-1 text-[11px] text-label-2" role="status">
                  <Loader2 size={11} className="animate-spin" aria-hidden /> 上传中
                </span>
              )}
              {q.state === 'done' && (
                <span className="flex flex-none items-center gap-0.5 text-[11px] font-semibold text-accent">
                  <Check size={11} aria-hidden /> 已登记
                </span>
              )}
              {q.state === 'failed' && (
                <button
                  type="button"
                  aria-label={`重试上传 ${q.file.name}`}
                  title="重试上传"
                  disabled={busy}
                  className="btn btn-s btn-sm flex-none"
                  onClick={() => void runUpload([q])}
                >
                  <RotateCcw size={11} aria-hidden /> 重试
                </button>
              )}
              <button
                type="button"
                aria-label={`移除 ${q.file.name}`}
                disabled={q.state === 'uploading'}
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-label-3 hover:bg-[var(--red-soft)] hover:text-red disabled:opacity-40"
                onClick={() => setQueue(list => list.filter(x => x.key !== q.key))}
              >
                <Trash2 size={13} aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* 解析选项折叠区 */}
      <div className="mt-3 rounded-xl border border-separator">
        <button
          type="button"
          aria-expanded={optionsOpen}
          onClick={() => setOptionsOpen(v => !v)}
          className="flex w-full items-center gap-1.5 px-3 py-2.5 text-xs font-semibold text-label-2 hover:text-label"
        >
          <ChevronDown size={13} className={`transition-transform ${optionsOpen ? '' : '-rotate-90'}`} aria-hidden />
          解析选项
        </button>
        {optionsOpen && (
          <div className="hairline-t space-y-3 px-3 py-3 text-xs">
            <div className="flex items-center gap-2">
              <span className="text-label-2">切片策略</span>
              <span className="seg" role="radiogroup" aria-label="切片策略">
                {SLICE_STRATEGIES.map(s => (
                  <button
                    key={s}
                    type="button"
                    role="radio"
                    aria-checked={strategy === s}
                    className={`seg-btn ${strategy === s ? 'on' : ''}`}
                    onClick={() => setStrategy(s)}
                  >
                    {s}
                  </button>
                ))}
              </span>
            </div>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={extract} onChange={e => setExtract(e.target.checked)} />
              <span className="text-label-2">上传后立即抽取（建七步流水线任务）</span>
            </label>
            <label className="flex items-center gap-2">
              <span className="text-label-2">目标知识库</span>
              <Select
                className="input h-8 w-52 text-xs"
                aria-label="目标知识库"
                data-testid="upload-kb-target"
                value={kbTarget}
                onChange={e => setKbTarget(e.target.value)}
              >
                {kbOptions.map(k => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </Select>
            </label>
          </div>
        )}
      </div>
    </Modal>
  )
}
