import {
  BookOpen,
  Bot,
  Box,
  FlaskConical,
  GitBranch,
  Layers,
  LayoutGrid,
  ListTodo,
  MessageSquare,
  Plug,
  Puzzle,
  Settings,
  ShieldCheck,
  User,
  Workflow,
  Wrench,
  Sparkles,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

/** 路由图标键（routes.tsx meta.icon）→ lucide-react 映射：侧栏与 ⌘K 面板共用单一映射（22 篇 sprite 纪律）；
 *  新增路由必须同步登记，未知键回退 Sparkles 并 dev 告警（缺图标回归=本守卫）。 */
const ICONS: Record<string, LucideIcon> = {
  grid: LayoutGrid,
  chat: MessageSquare,
  list: ListTodo,
  cube: Box,
  branch: GitBranch,
  book: BookOpen,
  flask: FlaskConical,
  layers: Layers,
  bot: Bot,
  puzzle: Puzzle,
  wrench: Wrench,
  plug: Plug,
  gear: Settings,
  user: User,
  workflow: Workflow,
  shield: ShieldCheck,
  spark: Sparkles,
}

export function RouteGlyph({ icon, size = 15 }: { icon: string; size?: number }) {
  const Icon = ICONS[icon]
  // 缺图标回归守卫：未知键渲染占位块（保留布局宽）+ dev 告警提示登记映射
  if (!Icon) {
    if (import.meta.env.DEV) console.warn(`[RouteGlyph] 未登记图标键: "${icon}" — 请在 route-glyph.tsx ICONS 补映射`)
    return <span className="inline-block w-[15px]" aria-hidden data-missing-icon={icon} />
  }
  return <Icon size={size} aria-hidden />
}
