import { create } from 'zustand'
import { persist } from 'zustand/middleware'

/** 全局 UI 态（16 篇 §2.2）：主题不在本 store——S1 收口后唯一事实源 = app/providers/theme-provider
 *  （localStorage 'oa-theme'，三态亮/暗/系统），此处仅保留壳布局与命令面板开关。 */
interface UiState {
  sidebarCollapsed: boolean
  commandOpen: boolean
  toggleSidebar: () => void
  setCommandOpen: (v: boolean) => void
}

export const useUiStore = create<UiState>()(
  persist(
    set => ({
      sidebarCollapsed: false,
      commandOpen: false,
      toggleSidebar: () => set(s => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setCommandOpen: v => set({ commandOpen: v }),
    }),
    { name: 'oa-ui' },
  ),
)
