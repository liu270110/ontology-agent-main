import { useAuthStore } from '@/stores/auth-store'

const STATS = [
  { num: '3', label: '本体项目', delta: 'v1.4 已发布', tone: 'text-green' },
  { num: '128', label: '知识文档', delta: '本周 +12', tone: 'text-green' },
  { num: '24', label: '今日对话', delta: '较昨日 +8', tone: 'text-green' },
  { num: '5', label: '待审批', delta: '需要您处理', tone: 'text-orange', alert: true },
] as const

const RECENT = [
  { icon: '💬', tone: 'bg-accent-soft text-accent', title: '对比 GB/T 31486 与 36276 循环寿命测试差异', sub: 'Claude · 8 分钟前 · 3 条证据引用', side: '●' },
  { icon: '◆', tone: 'bg-purple-soft text-purple', title: '为"电池包热失控"补充约束逻辑 CL-014', sub: '本体工作台 · 1 小时前 · 待终审', badge: '审核中' },
  { icon: '▤', tone: 'bg-teal-soft text-teal', title: '动力电池标准汇编 v3.pdf 抽取完成', sub: '流水线 · 2 小时前 · 候选 41 条', action: '去审核' },
] as const

/** 工作台（16 篇 §5.3 + 画框02）：问候 + StatCard×4 + 最近会话 + 抽取流水线。
 *  数据接接口前先以静态样张占位；`/me`、统计端点就绪后切 use*Query。 */
export function DashboardPage() {
  const user = useAuthStore(s => s.user)
  return (
    <div>
      <div className="flex items-center">
        <h1 className="text-xl font-bold">{greeting()}，{user?.displayName ?? '用户'}</h1>
        <span className="ml-auto flex gap-2">
          <button className="btn btn-s rounded-lg border border-separator px-3 py-1.5 text-xs">上传文档</button>
          <button className="btn btn-p rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-white">＋新建本体项目</button>
        </span>
      </div>
      <p className="sub mt-1 text-xs text-label-3">今天有 5 项审批待处理，抽取流水线 2 个任务运行中。</p>

      <div className="mt-4 grid grid-cols-4 gap-4">
        {STATS.map(s => (
          <div key={s.label} className={`card rounded-xl border border-separator bg-surface p-4 ${'alert' in s && s.alert ? 'border-orange' : ''}`}>
            <div className={`text-2xl font-bold ${'alert' in s && s.alert ? 'text-orange' : ''}`}>{s.num}</div>
            <div className="mt-0.5 text-xs text-label-3">{s.label}</div>
            <div className={`mt-2 text-[11px] ${s.tone}`}>{s.delta}</div>
          </div>
        ))}
      </div>

      <div className="mt-4 grid grid-cols-[1.35fr_1fr] gap-4">
        <div className="card rounded-xl border border-separator bg-surface p-4">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold">最近会话</h3>
            <span className="text-[11px] text-label-3">查看全部</span>
          </div>
          {RECENT.map(r => (
            <div key={r.title} className="flex items-center gap-3 border-b border-separator py-2.5 last:border-0">
              <span className={`flex h-8 w-8 items-center justify-center rounded-lg text-sm ${r.tone}`}>{r.icon}</span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px]">{r.title}</div>
                <div className="text-[11px] text-label-3">{r.sub}</div>
              </div>
              {'badge' in r && r.badge && <span className="badge rounded-full border border-orange px-2 py-0.5 text-[10px] text-orange">{r.badge}</span>}
              {'action' in r && r.action && <button className="btn btn-s rounded-lg border border-separator px-2.5 py-1 text-[11px]">{r.action}</button>}
            </div>
          ))}
        </div>
        <div className="card rounded-xl border border-separator bg-surface p-4">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold">抽取流水线</h3>
            <span className="font-mono text-[10px] text-label-3">JOB #218</span>
          </div>
          <ol className="my-3 space-y-2 text-xs">
            {['预处理', '批量抽取', '术语对齐', '一致性', 'SHACL'].map(s => (
              <li key={s} className="flex items-center gap-2 text-label-2">
                <span className="flex h-4 w-4 items-center justify-center rounded-full bg-green/15 text-[9px] text-green">✓</span>
                {s}
              </li>
            ))}
            <li className="flex items-center gap-2">
              <span className="h-4 w-4 animate-pulse rounded-full bg-accent/20" />
              人工归档（当前）
            </li>
          </ol>
          <p className="text-xs leading-6 text-label-2">候选实例 41 条等待终审。通过后将写入 Neo4j 图谱并建立向量索引。</p>
          <button className="btn btn-p mt-3 rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-white">进入审核台 →</button>
        </div>
      </div>
    </div>
  )
}

function greeting() {
  const h = new Date().getHours()
  if (h < 6) return '夜深了'
  if (h < 12) return '早上好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
}
