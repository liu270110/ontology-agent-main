import { create } from 'zustand'

/** 本体工作台编辑草稿态（03 篇 §5.2 / 16 篇 §2：editor-draft 归 features/<域>/stores/）。
 *  编辑不直写 TBox——「应用修改/新建/连线」只进本地变更单草稿（候选非成品，宪法 3），
 *  dirty 时路由离开触发 IX-ON-07 拦截；保存草稿/提交评审后清零。 */

interface WorkbenchState {
  projectId: string | null
  /** 当前选中节点 IRI（类树 ↔ 画布双向联动事实源） */
  selectedIri: string | null
  /** 未保存修改计数（>0 即 dirty） */
  dirtyCount: number
  /** 当前草稿变更单（changeset id；保存草稿后回填） */
  changesetId: string | null
  enter: (projectId: string) => void
  select: (iri: string | null) => void
  bumpDirty: (n?: number) => void
  setChangeset: (id: string | null) => void
  resetDirty: () => void
}

export const useWorkbenchStore = create<WorkbenchState>()((set, get) => ({
  projectId: null,
  selectedIri: null,
  dirtyCount: 0,
  changesetId: null,

  enter(projectId) {
    if (get().projectId !== projectId) {
      set({ projectId, selectedIri: null, dirtyCount: 0, changesetId: null })
    }
  },
  select(iri) {
    set({ selectedIri: iri })
  },
  bumpDirty(n = 1) {
    set({ dirtyCount: Math.max(0, get().dirtyCount + n) })
  },
  setChangeset(id) {
    set({ changesetId: id })
  },
  resetDirty() {
    set({ dirtyCount: 0 })
  },
}))
