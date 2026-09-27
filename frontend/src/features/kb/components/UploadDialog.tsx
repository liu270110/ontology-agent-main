import { useMemo, useState } from 'react'
import { useDropzone } from 'react-dropzone'
import { ChevronDown, FileUp, Loader2, Trash2, Upload } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { registerDocument, startPipeline } from '../api'
import { formatSize } from './shared'

/** IX-KB-01 上传文档弹窗（Modal 600px；画板 p-kb / 26 篇 §5.1）：
 *  拖拽区（react-dropzone，拖入高亮 + 点击选择）→ 多文件队列（名/大小/格式徽标/移除）
 *  → 解析选项折叠区（切片策略/抽取开关/目标知识库）→「上传并抽取」（汇总大小校验）。
 *  提交=POST /kb/documents 登记 →（抽取开关开启时）POST pipeline/start 建任务；
 *  live 直传 MinIO 预签名地址步骤随 X13 预览服务一起接线（R18 建议）。 */

const MAX_TOTAL = 100 * 1024 * 1024 // 汇总大小校验上限（100MB）
const KB_TARGETS = ['配网运检知识库', '停电分析知识库', '抢修工单知识库']
const SLICE_STRATEGIES = ['段落', '语义', '固定长度'] as const

interface QueuedFile {
  key: string
  file: File
}

export function UploadDialog({ open, onClose, onUploaded }: { open: boolean; onClose: () => void; onUploaded: () => void }) {
  const [queue, setQueue] = useState<QueuedFile[]>([])
  const [strategy, setStrategy] = useState<(typeof SLICE_STRATEGIES)[number]>('段落')
  const [extract, setExtract] = useState(true)
  const [kbTarget, setKbTarget] = useState(KB_TARGETS[0])
  const [optionsOpen, setOptionsOpen] = useState(false)
  const [busy, setBusy] = useState(false)

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
      setQueue(q => [...q, ...accepted.map(f => ({ key: `${f.name}-${f.size}-${Math.random().toString(36).slice(2, 7)}`, file: f }))])
    },
  })

  async function onUploadAndExtract() {
    if (queue.length === 0 || overSize || busy) return
    setBusy(true)
    try {
      const jobs: string[] = []
      for (const q of queue) {
        // ① 登记文档（§5.4 POST /kb/documents；mock 不落对象存储）
        const doc = await registerDocument({ name: q.file.name, size_bytes: q.file.size, content_type: q.file.type || 'application/octet-stream' })
        // ② 建抽取任务（§5.2/§5.4 pipeline/start；不开抽取时仅登记）
        if (extract) {
          const { job_id } = await startPipeline(doc.id)
          jobs.push(job_id)
        }
      }
      toast.success(extract ? `已上传 ${queue.length} 个文档并建抽取任务（JOB #${jobs.map(j => j.replace(/^job-/, '')).join('、')}）` : `已登记 ${queue.length} 个文档（未开抽取）`)
      setQueue([])
      onUploaded()
      onClose()
    } catch (e) {
      toast.error(`上传失败：${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setBusy(false)
    }
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
            onClick={() => void onUploadAndExtract()}
          >
            {busy ? <Loader2 size={13} className="animate-spin" aria-hidden /> : <Upload size={13} aria-hidden />}
            上传并抽取
          </button>
        </>
      }
    >
      {/* 拖拽区（拖入高亮 + 点击选择） */}
      <div
        {...getRootProps()}
        className={`upzone cursor-pointer ${isDragActive ? 'drag' : ''}`}
        aria-label="拖拽文件到此处或点击选择"
      >
        <input {...getInputProps()} data-testid="upload-input" />
        <FileUp size={22} className="mx-auto mb-1 text-label-3" aria-hidden />
        <p className="text-[13px] font-medium">拖拽文件到此处，或点击选择</p>
        <p className="mt-1 text-[11px] text-label-3">支持 PDF / Word / Excel / CSV / 图片 · 单次总量 ≤ 100MB</p>
      </div>

      {/* 多文件队列 */}
      {queue.length > 0 && (
        <ul className="mt-3 space-y-1.5" aria-label="上传队列">
          {queue.map(q => (
            <li key={q.key} className="flex items-center gap-2 rounded-lg border border-separator bg-surface-2 px-3 py-2">
              <span className="badge b-gray">{(q.file.name.split('.').pop() ?? '?').toUpperCase()}</span>
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{q.file.name}</span>
              <span className="mono dim flex-none text-[11px]">{formatSize(q.file.size)}</span>
              <button
                type="button"
                aria-label={`移除 ${q.file.name}`}
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-label-3 hover:bg-[var(--red-soft)] hover:text-red"
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
          className="flex w-full items-center gap-1.5 px-3 py-2.5 text-[12.5px] font-semibold text-label-2 hover:text-label"
        >
          <ChevronDown size={13} className={`transition-transform ${optionsOpen ? '' : '-rotate-90'}`} aria-hidden />
          解析选项
        </button>
        {optionsOpen && (
          <div className="hairline-t space-y-3 px-3 py-3 text-[12.5px]">
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
              <select className="input h-8 w-52 text-[12.5px]" value={kbTarget} onChange={e => setKbTarget(e.target.value)}>
                {KB_TARGETS.map(k => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
      </div>
    </Modal>
  )
}
