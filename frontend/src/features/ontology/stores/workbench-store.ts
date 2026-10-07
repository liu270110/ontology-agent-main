import { create } from 'zustand'
import type { OntoClassNode } from '../api'

/** 本体工作台编辑草稿态（03 篇 §5.2 / 16 篇 §2：editor-draft 归 features/<域>/stores/）。
 *  编辑不直写 TBox——「应用修改/新建/连线」只进本地变更单草稿（候选非成品，宪法 3），
 *  dirty 时路由离开触发 IX-ON-07 拦截；保存草稿/提交评审后清零。
 *
 *  pending 层（新建/连线真上图的状态地基）：本地候选类/边/删除集三份数组，
 *  画布装配 = 服务端数据 ∪ pending（hooks.ts），IRI 幂等去重由 pruneSynced 兜底。
 *  dirtyCount 为派生字段 = pending 三数组长度和 + _manualDirty（过渡期 bumpDirty
 *  兼容偏移，调用点改造后归零）。撤销走快照式双栈（整包压栈，上限 50）。 */

/** pending 边：连线草稿（连线即对象属性是画板核心隐喻，subclass = subClassOf 层级边） */
export interface PendingEdge {
  source_id: string
  target_id: string
  /** property 边的 predicate（对象属性名，如 locatedIn）；subclass 边省略 */
  prop?: string
  kind: 'subclass' | 'property'
}

/** pending 类：OntoClassNode + 本地草稿扩展字段（不入服务端载荷，装配层消费） */
export interface PendingClassNode extends OntoClassNode {
  /** 引用上屏（类树拖入已有类的钉位引用，非新建）：pruneSynced 豁免同 IRI 服务端吞并 */
  refOnly?: boolean
  /** 入场落点（画布业务坐标，拖入时由屏幕坐标换算）；缺省走内置分层布局 */
  position?: { x: number; y: number }
}

/** 新建类入参：全字段可选，id/iri 缺省时按 local: 临时规则生成 */
export type PendingClassInput = Partial<PendingClassNode>

/** 撤销快照：pending 三数组整包（数组浅拷贝，元素本身不可变） */
interface PendingSnapshot {
  pendingClasses: PendingClassNode[]
  pendingEdges: PendingEdge[]
  pendingDeletes: string[]
}

/** 撤销栈上限（超出丢最旧） */
const UNDO_LIMIT = 50

/** pending 边稳定键（store 与 hooks 服务端边去重共用同一实现，防双影对账口径一致） */
export function pendingEdgeKey(e: Pick<PendingEdge, 'kind' | 'source_id' | 'target_id' | 'prop'>): string {
  return `${e.kind}|${e.source_id}|${e.target_id}|${e.prop ?? ''}`
}

/** 临时 IRI 生成规则：local: + crypto.randomUUID 前 8 位（无 randomUUID 环境降级随机串） */
export function localIriSuffix(): string {
  const c: Crypto | undefined = typeof crypto !== 'undefined' ? crypto : undefined
  if (c && typeof c.randomUUID === 'function') return c.randomUUID().slice(0, 8)
  return Math.random().toString(36).slice(2, 10)
}

/** 左树受控 Tab（26 篇 §6.2 IX-ON-03 四 Tab）：Inspector seg / URL ?tab= 深链均可驱动 */
export type WorkbenchLeftTab = 'classes' | 'properties' | 'axioms' | 'rules'

interface WorkbenchState {
  projectId: string | null
  /** 当前选中节点 IRI（类树 ↔ 画布双向联动事实源） */
  selectedIri: string | null
  /** 左树受控 Tab（Inspector 属性/公理 seg 跳转 + URL ?tab= 深链播种；运行期事实源） */
  leftTab: WorkbenchLeftTab
  /** 未保存修改计数（>0 即 dirty）——派生字段：pending 三数组长度和 + _manualDirty，
   *  由 commit 在每次 mutation 统一再导出（保留字段形态兼容 s.dirtyCount 订阅方） */
  dirtyCount: number
  /** 当前草稿变更单（changeset id；保存草稿后回填） */
  changesetId: string | null

  // ---- pending 层（本地候选，画布装配 ∪ 服务端数据；候选非成品，宪法 3） ----
  /** 本地新建类（临时 IRI local:xxx；服务端出现同 IRI 时被 pruneSynced 吞并；
   *  refOnly 引用上屏除外——那本就是服务端类的钉位引用） */
  pendingClasses: PendingClassNode[]
  /** 本地连线草稿（subClassOf / 对象属性边） */
  pendingEdges: PendingEdge[]
  /** 标记删除的服务端类 IRI 集（装配时隐藏，提交评审后才真删） */
  pendingDeletes: string[]
  /** 快照式撤销栈（每次 mutation 前整包压栈，上限 50） */
  undoStack: PendingSnapshot[]
  /** 快照式重做栈（undo 弹出压入，新编辑清空） */
  redoStack: PendingSnapshot[]
  /** @internal 过渡期 bumpDirty 手工偏移（pending 之外的历史计数，调用点改造后移除） */
  _manualDirty: number

  enter: (projectId: string) => void
  select: (iri: string | null) => void
  setLeftTab: (t: WorkbenchLeftTab) => void
  /** @deprecated dirtyCount 已派生自 pending 层；过渡期保留可用（内部记 _manualDirty 偏移），
   *  InspectorPanel 调用点改造后移除 */
  bumpDirty: (n?: number) => void
  setChangeset: (id: string | null) => void
  /** 清零（保存草稿/提交评审/放弃修改）：pending 三数组 + 撤销栈一并清空 */
  resetDirty: () => void

  /** 新建类入 pending：缺省字段补全（id/iri 按 local: 临时规则），返回成型的类 */
  addPendingClass: (partial: PendingClassInput) => PendingClassNode
  /** 连线入 pending：完全相同的边重复添加为 no-op（不计 dirty 不进撤销栈） */
  addPendingEdge: (edge: PendingEdge) => void
  /** 画布右键「删除元素」：pending 类（含 refOnly 引用）直接移除；服务端类进
   *  pendingDeletes（草稿级标删，装配层虚线+降透明呈现，评审通过后才真删）；
   *  级联撤其 pending 边；重复标删 no-op */
  markDeleted: (iri: string) => void
  /** 撤销一项 pending：类 IRI → 连级撤其 pending 边（refOnly 引用不级联——节点
   *  回归布局位后既有 pending 边仍成立）；删除标记 IRI → 取消标删；均无 → no-op */
  removePending: (iri: string) => void
  /** IRI 幂等去重（hooks useEffect 在服务端数据变化时调用）：服务端已存在同 IRI 类 /
   *  同三元组边 → pending 项自动清除；已不在服务端类表的删除标记 → 自动清除。
   *  不进撤销栈——TBox 事实吞并本地候选，undo 不得复活已存在元素。无可清理时 no-op。 */
  pruneSynced: (serverClassIris: ReadonlySet<string>, serverEdgeKeys: ReadonlySet<string>) => void
  undo: () => void
  redo: () => void
  canUndo: () => boolean
  canRedo: () => boolean
}

export const useWorkbenchStore = create<WorkbenchState>()((set, get) => {
  /** dirty 派生：pending 三数组长度和 + 手工偏移（单一出口，任何 mutation 后重导出） */
  const dirtyOf = (s: Pick<WorkbenchState, 'pendingClasses' | 'pendingEdges' | 'pendingDeletes' | '_manualDirty'>) =>
    s.pendingClasses.length + s.pendingEdges.length + s.pendingDeletes.length + s._manualDirty

  /** 统一提交：合并 partial 后重导出 dirtyCount（非 dirty 相关字段原样透传） */
  const commit = (partial: Partial<WorkbenchState> | ((state: WorkbenchState) => Partial<WorkbenchState>)) =>
    set(state => {
      const p = typeof partial === 'function' ? partial(state) : partial
      return { ...p, dirtyCount: dirtyOf({ ...state, ...p }) }
    })

  const snapshotOf = (s: Pick<WorkbenchState, 'pendingClasses' | 'pendingEdges' | 'pendingDeletes'>): PendingSnapshot => ({
    pendingClasses: [...s.pendingClasses],
    pendingEdges: [...s.pendingEdges],
    pendingDeletes: [...s.pendingDeletes],
  })

  /** mutation 前置：当前 pending 整包压撤销栈（上限 50 丢最旧）+ 清重做栈 */
  const pushUndo = (s: WorkbenchState): Pick<WorkbenchState, 'undoStack' | 'redoStack'> => ({
    undoStack: [...s.undoStack, snapshotOf(s)].slice(-UNDO_LIMIT),
    redoStack: [],
  })

  return {
    projectId: null,
    selectedIri: null,
    leftTab: 'classes',
    dirtyCount: 0,
    changesetId: null,
    pendingClasses: [],
    pendingEdges: [],
    pendingDeletes: [],
    undoStack: [],
    redoStack: [],
    _manualDirty: 0,

    enter(projectId) {
      if (get().projectId !== projectId) {
        commit({
          projectId,
          selectedIri: null,
          changesetId: null,
          _manualDirty: 0,
          pendingClasses: [],
          pendingEdges: [],
          pendingDeletes: [],
          undoStack: [],
          redoStack: [],
        })
      }
    },
    select(iri) {
      set({ selectedIri: iri })
    },
    setLeftTab(t) {
      set({ leftTab: t })
    },
    bumpDirty(n = 1) {
      commit({ _manualDirty: Math.max(0, get()._manualDirty + n) })
    },
    setChangeset(id) {
      set({ changesetId: id })
    },
    resetDirty() {
      commit({
        _manualDirty: 0,
        pendingClasses: [],
        pendingEdges: [],
        pendingDeletes: [],
        undoStack: [],
        redoStack: [],
      })
    },

    addPendingClass(partial) {
      const suffix = localIriSuffix()
      const cls: PendingClassNode = {
        id: partial.id ?? `local-${suffix}`,
        iri: partial.iri ?? `local:${suffix}`,
        name: partial.name ?? `Class_${suffix}`,
        label: partial.label ?? partial.name ?? '新建类',
        parent_id: partial.parent_id ?? null,
        abstract: partial.abstract,
        synonyms: partial.synonyms,
        definition: partial.definition,
        instance_count: partial.instance_count ?? 0,
        ...(partial.refOnly ? { refOnly: true } : {}),
        ...(partial.position ? { position: partial.position } : {}),
      }
      commit(state => ({ ...pushUndo(state), pendingClasses: [...state.pendingClasses, cls] }))
      return cls
    },

    addPendingEdge(edge) {
      const s = get()
      const dup = s.pendingEdges.some(
        e => e.kind === edge.kind && e.source_id === edge.source_id && e.target_id === edge.target_id && e.prop === edge.prop,
      )
      if (dup) return
      commit(state => ({ ...pushUndo(state), pendingEdges: [...state.pendingEdges, edge] }))
    },

    markDeleted(iri) {
      const s = get()
      // pending 类（新建/引用上屏）= 本地草稿，直接移除（复用 removePending 语义）
      if (s.pendingClasses.some(c => c.iri === iri)) {
        get().removePending(iri)
        return
      }
      if (s.pendingDeletes.includes(iri)) return
      commit(state => ({
        ...pushUndo(state),
        pendingDeletes: [...state.pendingDeletes, iri],
        // 标删节点在装配层降透明呈现但仍在图，其 pending 边一并撤（草稿级级联）
        pendingEdges: state.pendingEdges.filter(e => e.source_id !== iri && e.target_id !== iri),
      }))
    },

    removePending(iri) {
      const s = get()
      const target = s.pendingClasses.find(c => c.iri === iri)
      const hasDel = s.pendingDeletes.includes(iri)
      if (!target && !hasDel) return
      // 撤新建类连级撤其关联 pending 边（装配层不产生悬空边）；
      // refOnly 引用例外：节点只是回归布局位，既有 pending 边仍成立，不级联
      const cascadeEdges = !!target && !target.refOnly
      commit(state => ({
        ...pushUndo(state),
        pendingClasses: target ? state.pendingClasses.filter(c => c.iri !== iri) : state.pendingClasses,
        pendingEdges: cascadeEdges ? state.pendingEdges.filter(e => e.source_id !== iri && e.target_id !== iri) : state.pendingEdges,
        pendingDeletes: hasDel ? state.pendingDeletes.filter(d => d !== iri) : state.pendingDeletes,
      }))
    },

    pruneSynced(serverClassIris, serverEdgeKeys) {
      const s = get()
      // refOnly 引用上屏项豁免：它引用的就是服务端已有类，同 IRI 非双影
      const keptClasses = s.pendingClasses.filter(c => (c.refOnly ? true : !serverClassIris.has(c.iri)))
      const keptEdges = s.pendingEdges.filter(e => !serverEdgeKeys.has(pendingEdgeKey(e)))
      const keptDeletes = s.pendingDeletes.filter(iri => serverClassIris.has(iri))
      if (
        keptClasses.length === s.pendingClasses.length &&
        keptEdges.length === s.pendingEdges.length &&
        keptDeletes.length === s.pendingDeletes.length
      ) {
        return
      }
      // 去重 = 服务端事实吞并本地候选（幂等纠偏），不进撤销栈
      commit({ pendingClasses: keptClasses, pendingEdges: keptEdges, pendingDeletes: keptDeletes })
    },

    undo() {
      const s = get()
      if (!s.undoStack.length) return
      const prev = s.undoStack[s.undoStack.length - 1]
      commit(() => ({
        pendingClasses: prev.pendingClasses,
        pendingEdges: prev.pendingEdges,
        pendingDeletes: prev.pendingDeletes,
        undoStack: s.undoStack.slice(0, -1),
        redoStack: [...s.redoStack, snapshotOf(s)],
      }))
    },

    redo() {
      const s = get()
      if (!s.redoStack.length) return
      const next = s.redoStack[s.redoStack.length - 1]
      commit(() => ({
        pendingClasses: next.pendingClasses,
        pendingEdges: next.pendingEdges,
        pendingDeletes: next.pendingDeletes,
        redoStack: s.redoStack.slice(0, -1),
        undoStack: [...s.undoStack, snapshotOf(s)],
      }))
    },

    canUndo: () => get().undoStack.length > 0,
    canRedo: () => get().redoStack.length > 0,
  }
})
