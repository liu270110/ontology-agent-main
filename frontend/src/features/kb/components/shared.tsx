import type { CandidateType, KbDocument } from '../api'

/** kb 域共享小件：状态徽标（IX-KB 四态）/ 格式徽标 / 置信度徽标 / 七步流水线步骤条。
 *  颜色只走令牌（03 篇铁律）；文案禁存储名词（26 篇口径）。 */

/** 四态状态徽标（画板 p-kb 同款）：已入库 / 抽取中(进度) / 待抽取 / 失败 */
export function StatusBadge({ doc }: { doc: Pick<KbDocument, 'status' | 'progress' | 'job_id' | 'error' | 'chunk_count'> }) {
  if (doc.status === 'indexed') return <span className="badge b-green">已入库</span>
  if (doc.status === 'failed')
    return <span className="badge b-red">抽取失败{doc.error ? ` · ${doc.error}` : ''}</span>
  if (doc.status === 'extracting')
    return (
      <span className="badge b-orange">
        <span className="dot d-orange animate-pulse" style={{ width: 5, height: 5 }} aria-hidden />
        抽取中{doc.job_id ? ` · JOB #${doc.job_id.replace(/^job-/, '')}` : ''} · {doc.progress}%
      </span>
    )
  return <span className="badge b-blue">待抽取</span>
}

const TYPE_LABEL: Record<KbDocument['doc_type'], string> = {
  PDF: 'PDF',
  Word: 'Word',
  Excel: 'Excel',
  CSV: 'CSV',
  文本: '文本',
  图片: '图片',
}

export function FileTypeBadge({ type }: { type: KbDocument['doc_type'] }) {
  return <span className="badge b-gray">{TYPE_LABEL[type]}</span>
}

/** 置信度徽标：≥0.9 绿 / ≥0.7 蓝 / <0.7 橙（低于 0.7 提示人工复核，26 篇 p-review 提示语） */
export function ConfidenceBadge({ value }: { value: number }) {
  const cls = value >= 0.9 ? 'b-green' : value >= 0.7 ? 'b-blue' : 'b-orange'
  return (
    <span className={`badge ${cls}`} title={value < 0.7 ? '置信度低于 0.7，建议人工复核' : undefined}>
      置信 {value.toFixed(2)}
    </span>
  )
}

export const CANDIDATE_TYPE_LABEL: Record<CandidateType, string> = {
  entity: '实体',
  relation: '关系',
  attribute: '属性',
  axiom: '公理',
}

/** 七步抽取流水线（设计系统 .steps 同源；简版=传当前步号，done=已过/cur=进行中） */
const PIPELINE_STEPS = ['文档解析', '结构清洗', '语义切片', '向量嵌入', '实体抽取', '关系抽取', '终审入库']

export function PipelineStepper({ current, className = '' }: { current: number; className?: string }) {
  return (
    <div className={`steps ${className}`} aria-label="七步抽取流水线">
      {PIPELINE_STEPS.map((name, i) => {
        const step = i + 1
        const state = step < current ? 'done' : step === current ? 'cur' : ''
        return (
          <div key={name} className={`step ${state}`}>
            <span className="s-dot">{state === 'done' ? '✓' : ''}</span>
            <span className="s-name">{name}</span>
          </div>
        )
      })}
    </div>
  )
}

/** 相对时间（列表更新列；画板「10 分钟前/昨天」口径）——单源 lib/reltime（36 §7 收编，
 *  kb/memory/ontology 三份域内同源实现合一；此处再导出兼容既有域内引用） */
export { relativeTime } from '@/lib/reltime'

export function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)}KB`
  return `${bytes}B`
}

/** 快捷键屏蔽（IX-REV-04）：输入类控件聚焦时不劫持按键 */
export function isTypingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null
  if (!el) return false
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable === true
}

/** 命中句高亮（span 偏移 → mark；IX-KB-02/IX-REV-01/IX-PG-01 共用） */
export function HighlightedQuote({ text, span }: { text: string; span: [number, number] | null }) {
  if (!span || span[0] >= text.length) return <>{text}</>
  const [s, e] = span
  return (
    <>
      {text.slice(0, s)}
      <mark className="rounded bg-accent-soft px-0.5 text-accent" style={{ background: 'var(--accent-soft)' }}>
        {text.slice(s, Math.min(e, text.length))}
      </mark>
      {text.slice(Math.min(e, text.length))}
    </>
  )
}
