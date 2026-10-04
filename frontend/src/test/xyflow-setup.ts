import { beforeAll } from 'vitest'

// xyflow（React Flow v12）在 jsdom 下的浏览器 API 垫片（官方测试指引）：
// - ResizeObserver：节点尺寸测量（xyflow 给每个节点 wrapper 注册 RO）；
// - DOMMatrixReadOnly：d3-zoom 读取 transform 矩阵。
// 原先各测试文件私有复制（s4-ontology / fix-fe1-explore 等十余份），2026-10-05 收敛到本
// 全局 setup，经 vite.config.ts test.setupFiles 对所有测试文件生效。保留 ??= 语义：
// 真实浏览器 API（jsdom 未来内建）或用例私有垫片优先，不覆盖。
beforeAll(() => {
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= RO
  class DOMMatrixReadOnlyMock {
    m22 = 1
    m41 = 0
    m42 = 0
    constructor(transform?: string) {
      const scale = /scale\(([1-9.])\)/.exec(transform ?? '')
      if (scale) this.m22 = Number(scale[1])
    }
  }
  ;(globalThis as unknown as { DOMMatrixReadOnly: unknown }).DOMMatrixReadOnly ??= DOMMatrixReadOnlyMock
})
