import { create } from 'zustand'
import { persist } from 'zustand/middleware'

/** 全局 UI 态（16 篇 §2.2）：主题不在本 store——S1 收口后唯一事实源 = app/providers/theme-provider
 *  （localStorage 'oa-theme'，三态亮/暗/系统），此处仅保留壳布局与命令面板开关。 */
interface UiState {
  sidebarCollapsed: boolean
  commandOpen: boolean
  /** 侧栏宽度（S8 用户需求：边界线可拖拽拓展）；持久化记忆，范围 200~360px */
  sidebarWidth: number
  setSidebarWidth: (w: number) => void
  /** 已折叠的导航分组（S8 用户需求：一级菜单折叠）；持久化记忆 */
  collapsedGroups: string[]
  toggleSidebar: () => void
  setCommandOpen: (v: boolean) => void
  toggleGroup: (group: string) => void
}

export const useUiStore = create<UiState>()(
  persist(
    set => ({
      sidebarCollapsed: false,
      commandOpen: false,
      sidebarWidth: 240,
      setSidebarWidth: w => set({ sidebarWidth: Math.min(360, Math.max(200, Math.round(w))) }),
      collapsedGroups: [],
      toggleSidebar: () => set(s => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setCommandOpen: v => set({ commandOpen: v }),
      toggleGroup: group =>
        set(s => ({
          collapsedGroups: s.collapsedGroups.includes(group)
            ? s.collapsedGroups.filter(g => g !== group)
            : [...s.collapsedGroups, group],
        })),
    }),
    { name: 'oa-ui' },
  ),
)
