import { useState } from 'react'
import { Info } from 'lucide-react'
import { FloatingCard } from '@/components/popover'

/** IX-PG-02 三模式对比说明（模式分段控件旁 ⓘ；26 篇 §5.3）：
 *  Local=实体邻域 / Global=社区摘要 / Drift=混合漂移；各配迷你示意图 + 适用场景一句。
 *  静态说明（OntRAG §5 语义），无端点。 */

const MODES = [
  { key: 'Local', title: 'Local · 实体邻域', desc: '从查询命中的实体出发，沿关系边扩展 1~2 跳邻域。', scene: '适合：明确设备/故障实体的定点问题', dot: 'var(--accent)' },
  { key: 'Global', title: 'Global · 社区摘要', desc: '对知识图谱社区做 map-reduce 摘要后检索。', scene: '适合：全库主题性、跨设备的问题', dot: 'var(--purple)' },
  { key: 'Drift', title: 'Drift · 混合漂移', desc: 'Local + Global 混合并检测意图与语料分布漂移。', scene: '适合：标准换版期的核对与预警', dot: 'var(--orange)' },
] as const

export function ModeInfoPopover() {
  const [anchor, setAnchor] = useState<DOMRect | null>(null)
  const open = !!anchor

  return (
    <>
      <button
        type="button"
        aria-label="三模式对比说明"
        aria-expanded={open}
        className="flex h-5 w-5 items-center justify-center rounded-full text-label-3 hover:bg-surface-2 hover:text-accent"
        onClick={e => setAnchor(open ? null : (e.currentTarget as HTMLButtonElement).getBoundingClientRect())}
      >
        <Info size={13} aria-hidden />
      </button>
      <FloatingCard open={open} anchor={anchor} onClose={() => setAnchor(null)} width={320}>
        <h4 className="mb-2 text-[13px] font-bold">检索三模式</h4>
        <ul className="space-y-2.5">
          {MODES.map(m => (
            <li key={m.key} className="flex items-start gap-2.5">
              {/* 迷你示意图：单点邻域 / 三簇社区 / 双源漂移 */}
              <svg width="34" height="26" viewBox="0 0 34 26" aria-hidden className="mt-0.5 flex-none">
                {m.key === 'Local' && (
                  <>
                    <circle cx="17" cy="13" r="3" fill={m.dot} />
                    <circle cx="6" cy="6" r="2" fill="var(--separator)" /><line x1="15" y1="12" x2="8" y2="7" stroke="var(--separator)" />
                    <circle cx="28" cy="7" r="2" fill="var(--separator)" /><line x1="19" y1="12" x2="26" y2="8" stroke="var(--separator)" />
                    <circle cx="27" cy="20" r="2" fill="var(--separator)" /><line x1="19" y1="15" x2="25" y2="19" stroke="var(--separator)" />
                  </>
                )}
                {m.key === 'Global' && (
                  <>
                    <circle cx="8" cy="8" r="2" fill={m.dot} /><circle cx="14" cy="5" r="2" fill={m.dot} opacity=".55" /><circle cx="11" cy="13" r="2" fill={m.dot} opacity=".55" />
                    <circle cx="25" cy="9" r="2" fill={m.dot} opacity=".8" /><circle cx="29" cy="15" r="2" fill={m.dot} opacity=".4" />
                    <circle cx="16" cy="21" r="2" fill={m.dot} opacity=".65" /><circle cx="23" cy="22" r="2" fill={m.dot} opacity=".45" />
                  </>
                )}
                {m.key === 'Drift' && (
                  <>
                    <path d="M4 20 C 12 20, 12 6, 19 6 L 30 6" fill="none" stroke={m.dot} strokeWidth="1.6" />
                    <path d="M4 23 C 14 23, 16 16, 30 14" fill="none" stroke="var(--separator)" strokeWidth="1.2" />
                    <circle cx="30" cy="6" r="2.4" fill={m.dot} />
                  </>
                )}
              </svg>
              <span>
                <b className="block text-xs">{m.title}</b>
                <span className="block text-[11px] leading-5 text-label-2">{m.desc}</span>
                <span className="block text-[11px] text-label-3">{m.scene}</span>
              </span>
            </li>
          ))}
        </ul>
      </FloatingCard>
    </>
  )
}
