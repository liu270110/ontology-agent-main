import { useState } from 'react'
import { useDropzone } from 'react-dropzone'
import { FileUp, TriangleAlert } from 'lucide-react'
import { Modal } from '@/components/modal'
import { importTurtle, preflightImport, type ImportPreflightRow, type OntoProject } from '../api'

/** 导入 Turtle 弹窗（26 篇 §6.1 IX-OL-02；画板 ix-ol-02）：拖拽 .ttl + 目标版本单选
 *  + 冲突策略 + 预校验结果行（错误按行号就地定位）。导入本身走异步任务（任务中心跟踪）。
 *  端点为预登记待回填（26 篇 §14 #10，见交付报告 R 清单）。 */

export function ImportTurtleDialog({
  open,
  project,
  onClose,
  onQueued,
}: {
  open: boolean
  project: OntoProject | null
  onClose: () => void
  onQueued: (jobId: string) => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [target, setTarget] = useState<'new' | 'append'>('new')
  const [strategy, setStrategy] = useState<'skip' | 'overwrite'>('skip')
  const [rows, setRows] = useState<ImportPreflightRow[] | null>(null)
  const [checking, setChecking] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    accept: { 'text/turtle': ['.ttl'], 'application/octet-stream': ['.ttl'] },
    maxFiles: 1,
    onDrop: files => {
      const f = files[0] ?? null
      setFile(f)
      setRows(null)
    },
  })

  const errors = rows?.filter(r => r.level === 'error') ?? []
  const warnings = rows?.filter(r => r.level === 'warning') ?? []

  async function runPreflight() {
    if (!file || !project) return
    setChecking(true)
    try {
      const res = await preflightImport(project.id, { filename: file.name, strategy })
      setRows(res.rows)
    } finally {
      setChecking(false)
    }
  }

  async function submit() {
    if (!project || !file) return
    setSubmitting(true)
    try {
      const res = await importTurtle(project.id)
      onQueued(res.job_id)
      onClose()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="导入 Turtle 文档"
      width={560}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn-g btn-sm" disabled={!file || checking} onClick={() => void runPreflight()}>
            {checking ? '预校验中…' : '预校验'}
          </button>
          <button
            type="button"
            className="btn btn-p btn-sm"
            data-testid="import-submit"
            disabled={!file || !rows || errors.length > 0 || submitting}
            onClick={() => void submit()}
          >
            开始导入
          </button>
        </>
      }
    >
      <p className="text-xs text-label-3">
        目标：{project ? `${project.name} · ${project.namespace}` : '—'} · Turtle 1.1
      </p>

      {/* 拖拽区 */}
      <div
        {...getRootProps()}
        className={`mt-3 cursor-pointer rounded-xl border border-dashed px-4 py-6 text-center transition-colors ${
          isDragActive ? 'border-accent bg-accent-soft' : 'border-separator hover:border-accent'
        }`}
      >
        <input {...getInputProps()} aria-label="选择 .ttl 文件" />
        <FileUp size={18} className="mx-auto text-label-3" aria-hidden />
        <div className="mt-1.5 text-xs text-label-2">拖拽 .ttl 文件到此处，或点击选择 · 单件 ≤ 20 MB · UTF-8</div>
      </div>

      {file && (
        <div className="mt-2 flex items-center gap-2 rounded-lg border border-separator px-3 py-2 text-xs">
          <span className="mono truncate">{file.name}</span>
          <span className="text-label-3">{(file.size / 1024).toFixed(1)} KB</span>
          <span className="badge b-blue ml-auto">已选择</span>
          <button
            type="button"
            aria-label={`移除 ${file.name}`}
            className="text-label-3 hover:text-red"
            onClick={() => {
              setFile(null)
              setRows(null)
            }}
          >
            ×
          </button>
        </div>
      )}

      {/* 目标版本 */}
      <fieldset className="mt-3">
        <legend className="field-label">目标版本</legend>
        <label className="flex cursor-pointer items-start gap-2 py-1 text-xs">
          <input type="radio" name="imp-target" checked={target === 'new'} onChange={() => setTarget('new')} />
          <span>
            <b>新建版本 v1.0</b>
            <span className="text-label-3">（当前无草稿，导入即建首版草稿）</span>
          </span>
        </label>
        <label className="flex cursor-pointer items-start gap-2 py-1 text-xs">
          <input type="radio" name="imp-target" checked={target === 'append'} onChange={() => setTarget('append')} />
          <span>
            <b>追加至草稿 {project?.draft_version ?? 'v2.2-draft'}</b>
            <span className="text-label-3">（已有未提交修改，导入内容并入变更单 cs_01K）</span>
          </span>
        </label>
      </fieldset>

      {/* 冲突策略 */}
      <fieldset className="mt-2">
        <legend className="field-label">导入选项</legend>
        <label className="flex cursor-pointer items-start gap-2 py-1 text-xs">
          <input type="checkbox" checked={strategy === 'skip'} onChange={() => setStrategy('skip')} />
          <span>跳过已有类（IRI 冲突时保留现状，并在导入报告中列出）</span>
        </label>
        <label className="flex cursor-pointer items-start gap-2 py-1 text-xs">
          <input type="checkbox" checked={strategy === 'overwrite'} onChange={() => setStrategy('overwrite')} />
          <span className="text-red">全量覆盖（先删后建，不可逆；提交前需第二次确认并写入审计日志）</span>
        </label>
      </fieldset>

      {/* 预校验结果（IX-OL-02：错误按行号就地定位，不通过不允许导入） */}
      {rows && (
        <div
          className={`mt-3 rounded-xl border px-3.5 py-3 text-xs ${errors.length > 0 ? 'border-[var(--red-soft)] bg-[var(--red-soft)]' : 'border-[var(--green-soft)] bg-[var(--green-soft)]'}`}
          data-testid="preflight-result"
        >
          <div className="flex items-center gap-2 font-semibold">
            <TriangleAlert size={13} className={errors.length > 0 ? 'text-red' : 'text-green'} aria-hidden />
            预校验 · {errors.length > 0 ? '未通过' : '通过'}
            <span className="ml-auto flex gap-1.5">
              {errors.length > 0 && <span className="badge b-red">{errors.length} 错误</span>}
              {warnings.length > 0 && <span className="badge b-orange">{warnings.length} 警告</span>}
            </span>
          </div>
          <ul className="mt-2 space-y-1">
            {rows.map(r => (
              <li key={`${r.level}-${r.line}`} className="leading-5">
                <span className={`mono font-semibold ${r.level === 'error' ? 'text-red' : 'text-orange'}`}>L{r.line}</span>{' '}
                {r.text}
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-3 text-[11px] text-label-3">修复语法错误后可导入 · 导入为异步任务，进度在任务中心跟踪。</p>
    </Modal>
  )
}
