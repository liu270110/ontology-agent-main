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
  User,
  Workflow,
  Wrench,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

/** 路由图标键（routes.tsx meta.icon）→ lucide-react 映射：侧栏与 ⌘K 面板共用单一映射（22 篇 sprite 纪律）。 */
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
}

export function RouteGlyph({ icon, size = 15 }: { icon: string; size?: number }) {
  const Icon = ICONS[icon]
  if (!Icon) return <span className="inline-block w-[15px]" aria-hidden />
  return <Icon size={size} aria-hidden />
}
