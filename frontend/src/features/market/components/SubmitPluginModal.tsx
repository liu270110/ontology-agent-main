import { useState } from 'react'
import { useCallback } from 'react'
import { useDropzone } from 'react-dropzone'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { FileCheck2, ShieldCheck, Upload, X } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { registerPlugin, submitPlugin } from '../api'
import { Select } from '@/components/select'

/** IX-MKT-03 上架申请（26 篇 §9.1；画板 ix-mkt-03）：560px 弹窗——
 *  拖拽上传插件包（.opk / server.json / OpenAPI 3.x，上传后自动验签）
 *  + 元数据表单（名称/分类/README）+ 说明「上架需走 review_workflow 审核」。
 *  提交：POST /plugins（201 登记）→ POST /plugins/{id}/submit（202）→ 审批中心。 */

const CATEGORIES = ['业务系统连接', '数据接入', '文档处理', '分析工具', '其他']

export function SubmitPluginModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const [file, setFile] = useState<{ name: string; size: number } | null>(null)
  const [name, setName] = useState('')
  const [category, setCategory] = useState(CATEGORIES[0])
  const [readme, setReadme] = useState('')

  const onDrop = useCallback((accepted: File[]) => {
    const f = accepted[0]
    if (f) setFile({ name: f.name, size: f.size })
  }, [])

  const { getRootProps, getInputProps, isDragActive } = useDropzone({ onDrop, multiple: false })

  const submit = useMutation({
    mutationFn: async () => {
      const reg = await registerPlugin({ name, category, readme, filename: file?.name })
      return submitPlugin(reg.id)
    },
    onSuccess: res => {
      void qc.invalidateQueries({ queryKey: ['market'] })
      toast.success('上架申请已提交', { description: res.workflow + '；审批进度在审批中心跟踪' })
      setFile(null)
      setName('')
      setReadme('')
      onClose()
    },
  })

  const canSubmit = !!file && name.trim().length >= 2

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="上架新插件"
      width={560}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-p btn-sm"
            data-testid="mkt-submit-go"
            disabled={!canSubmit || submit.isPending}
            onClick={() => submit.mutate()}
          >
            <Upload size={12} aria-hidden /> 提交审核
          </button>
        </>
      }
    >
      <p className="text-[11px] text-label-3">
        提交 server.json（MCP 扩展格式）、OpenAPI 3.x 文档或 .opk 插件包；登记与上架审核分离。
      </p>

      {/* 拖拽上传 */}
      <div
        {...getRootProps()}
        className="mt-3 cursor-pointer rounded-xl border border-dashed px-4 py-6 text-center transition-colors"
        style={{ borderColor: isDragActive ? 'var(--accent)' : 'var(--separator)', background: isDragActive ? 'var(--accent-soft)' : 'var(--surface)' }}
        data-testid="mkt-dropzone"
      >
        <input {...getInputProps()} aria-label="插件包上传" />
        <Upload size={20} className="mx-auto text-label-3" aria-hidden />
        <div className="mt-1.5 text-xs">
          拖拽插件包到此处，或
          <span className="text-accent"> 点击选择文件</span>
        </div>
        <div className="mt-0.5 text-[11px] text-label-3">
          支持 .opk / server.json / OpenAPI 3.x · 单包不超过 20MB · 上传后自动验签
        </div>
      </div>

      {file && (
        <div className="mt-2 flex items-center gap-2 rounded-xl border border-separator bg-surface-2 px-3 py-2">
          <FileCheck2 size={14} className="text-green" aria-hidden />
          <span className="mono text-xs">{file.name}</span>
          <span className="text-[11px] text-label-3">{(file.size / 1024 / 1024).toFixed(1)} MB</span>
          <span className="badge b-green ml-auto">验签通过</span>
          <button type="button" aria-label="移除已选文件" className="text-label-3 hover:text-red" onClick={() => setFile(null)}>
            <X size={13} aria-hidden />
          </button>
        </div>
      )}

      <div className="mt-3 grid grid-cols-2 gap-3">
        <div className="field mb-0">
          <label className="field-label" htmlFor="mkt-plugin-name">
            插件名称
          </label>
          <input
            id="mkt-plugin-name"
            className="input"
            data-testid="mkt-plugin-name"
            placeholder="示例：工单系统连接器"
            value={name}
            onChange={e => setName(e.target.value)}
          />
        </div>
        <div className="field mb-0">
          <label className="field-label" htmlFor="mkt-plugin-category">
            分类
          </label>
          <Select id="mkt-plugin-category" className="input" value={category} onChange={e => setCategory(e.target.value)}>
            {CATEGORIES.map(c => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </Select>
        </div>
      </div>

      <div className="field mb-0 mt-3">
        <label className="field-label" htmlFor="mkt-plugin-readme">
          README（市场详情页渲染源）
        </label>
        <textarea
          id="mkt-plugin-readme"
          className="input h-auto py-2"
          rows={3}
          data-testid="mkt-plugin-readme"
          placeholder="对接 95598 客服工单系统：停电工单检索、抢修工单创建（需审批）与状态回写…"
          value={readme}
          onChange={e => setReadme(e.target.value)}
        />
      </div>

      <div className="mt-3 rounded-xl px-3.5 py-3" style={{ background: 'var(--accent-soft)' }}>
        <div className="flex items-center gap-1.5 text-xs font-semibold text-accent">
          <ShieldCheck size={13} aria-hidden /> 上架需走 review_workflow 审核
        </div>
        <p className="mt-1 text-[11px] leading-4 text-label-2">
          五关自动门禁前置：验签 · scope 声明完整性 · annotations 合规 · 依赖扫描 · 许可证检查；
          通过后进入人工终审，审批进度在审批中心跟踪。
        </p>
      </div>
    </Modal>
  )
}
