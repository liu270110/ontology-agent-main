import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { Toaster } from 'sonner'
import { http, HttpResponse } from 'msw'
import { ApiError } from '@/api/client'
import { App } from '@/app/App'
import { isUnimplemented } from '@/lib/errors'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { ImportTurtleDialog } from '../components/ImportTurtleDialog'
import type { OntoProject } from '../api'

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** fe-s1 断供收敛 · 本体域（P-001/P-002）：后端元素读模型 / 导入管线端点未上线（404 裸态，
 *  非信封 {detail:"Not Found"}）→ 前端统一「功能建设中」语义，不炸不造数据。
 *  ① 四读全 404 → WorkbenchPage 主区 ErrorState 1004 特化（注记随 B8 后端批交付）；
 *  ② 混合态（类/属性成功，公理/规则 404）→ 成功 Tab 正常渲染，断供 Tab 显建设中占位；
 *  ③ ImportTurtle 预校验/提交 404 → toast「导入管线端点未上线（建设中）」+ 按钮态恢复。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

/** 裸 404（真实网关无路由形态，无业务码）——isUnimplemented 经 httpStatus===404 命中 */
const bare404 = () => HttpResponse.json({ detail: 'Not Found' }, { status: 404 })

describe('fe-s1 断供收敛 · WorkbenchPage 四元素读', () => {
  it('① 四读全 404 → 主区「功能建设中」+ B8 交付注记（非错误码非重试钮）', async () => {
    server.use(
      http.get('*/api/v1/ontologies/onto-outage/classes', () => bare404()),
      http.get('*/api/v1/ontologies/onto-outage/properties', () => bare404()),
      http.get('*/api/v1/ontologies/onto-outage/axioms', () => bare404()),
      http.get('*/api/v1/ontologies/onto-outage/rules', () => bare404()),
    )
    await loginAndGo('/ontology/onto-outage')

    const st = await screen.findByTestId('unimplemented-state', {}, { timeout: 15_000 })
    expect(st).toHaveTextContent('功能建设中')
    expect(screen.getByTestId('unimplemented-note')).toHaveTextContent('随 B8 后端批交付')
    // 1004 特化态：无错误码行、无重试钮（重试必 404 无意义）
    expect(screen.queryByTestId('error-code')).not.toBeInTheDocument()
    expect(screen.queryByTestId('error-retry')).not.toBeInTheDocument()
    // 非真故障：不得出现常规错误态
    expect(screen.queryByTestId('error-state')).not.toBeInTheDocument()
  }, 30_000)

  it('② 混合态：类/属性成功 · 公理/规则 404 → 类树正常 + 公理 Tab 显建设中占位', async () => {
    server.use(
      http.get('*/api/v1/ontologies/onto-outage/axioms', () => bare404()),
      http.get('*/api/v1/ontologies/onto-outage/rules', () => bare404()),
    )
    await loginAndGo('/ontology/onto-outage')

    // 成功读：类 Tab 正常渲染（搜索框 + 树面板在位），无整页拦截（无 B8 注记）
    expect(await screen.findByLabelText('搜索类', {}, { timeout: 15_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('unimplemented-note')).not.toBeInTheDocument()

    // 断供读：切公理 Tab（左树面板内 Tab 钮——Inspector seg 同名钮已排除）→ 内容区显「功能建设中」
    const panel = screen.getByTestId('onto-tree-panel')
    fireEvent.click(within(panel).getByRole('button', { name: '公理' }))
    const blocked = await screen.findByTestId('unimplemented-state', {}, { timeout: 10_000 })
    expect(blocked).toHaveTextContent('功能建设中')
    expect(screen.queryByTestId('axiom-editor')).not.toBeInTheDocument()

    // 切规则 Tab → 同样建设中占位
    fireEvent.click(within(panel).getByRole('button', { name: '规则' }))
    expect((await screen.findAllByTestId('unimplemented-state')).length).toBeGreaterThan(0)
  }, 30_000)
})

describe('fe-s1 断供收敛 · isUnimplemented 共享判定（收口补齐：信封 1004 形态此前无直接断言）', () => {
  it('裸 404（httpStatus 命中）与信封 code=1004（含缺 status 形态）→ true；网络错/权限/非 ApiError → false', () => {
    // 裸 404：无业务码，仅 httpStatus（用例 ①②⑤ 的线上形态）
    expect(isUnimplemented(new ApiError(-1, 'HTTP 404', 404))).toBe(true)
    // 信封 1004：网关信封 code 命中（带/不带 httpStatus 两形态）
    expect(isUnimplemented(new ApiError(1004, '后端未实装：该功能接口待交付', 404))).toBe(true)
    expect(isUnimplemented(new ApiError(1004, '后端未实装：该功能接口待交付'))).toBe(true)
    // 非断供：权限、网络错、500、裸 Error、非 Error
    expect(isUnimplemented(new ApiError(2001, '权限不足', 403))).toBe(false)
    expect(isUnimplemented(new ApiError(-1, '网络连接不可用，请检查后端服务'))).toBe(false)
    expect(isUnimplemented(new ApiError(5001, '上游模型服务异常', 500))).toBe(false)
    expect(isUnimplemented(new Error('boom'))).toBe(false)
    expect(isUnimplemented(null)).toBe(false)
  })
})

describe('fe-s1 断供收敛 · ImportTurtleDialog（P-002）', () => {
  const project: OntoProject = {
    id: 'onto-outage',
    name: '配网停电分析本体',
    namespace: 'http://example.org/outage#',
    tier: 'heavy',
    description: '',
    head_version: 'v1.0',
    draft_version: 'v1.1-draft',
    status: 'draft',
    class_count: 3,
    entity_count: 30,
    updated_at: '2026-10-01T00:00:00Z',
  }

  function renderDialog() {
    render(
      <>
        <ImportTurtleDialog open project={project} onClose={() => {}} onQueued={() => {}} />
        <Toaster position="top-center" />
      </>,
    )
  }

  function chooseFile() {
    fireEvent.change(screen.getByLabelText('选择 .ttl 文件'), {
      target: {
        files: [new File(['out:Feeder a owl:Class .'], 'feeder.ttl', { type: 'text/turtle' })],
      },
    })
    // react-dropzone 校验链路异步回调 onDrop——等「已选择」行出现再继续，防竞态空 file
    return screen.findByText('feeder.ttl', {}, { timeout: 5_000 })
  }

  it('③ 端点状态 fhint 在位（弹窗顶部注明导入管线未上线口径）', () => {
    renderDialog()
    expect(screen.getByTestId('import-endpoint-hint')).toHaveTextContent('导入管线')
    expect(screen.getByTestId('import-endpoint-hint')).toHaveTextContent('建设中')
  })

  it('④ 预校验 404 → toast 建设中文案 + 预校验按钮恢复可用', async () => {
    server.use(http.post('*/api/v1/ontologies/onto-outage/import/preflight', () => bare404()))
    renderDialog()
    await chooseFile()

    const btn = screen.getByRole('button', { name: '预校验' })
    fireEvent.click(btn)
    expect(btn).toBeDisabled() // 提交态先行
    expect(await screen.findByText('导入管线端点未上线（建设中）', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(btn).toBeEnabled()) // finally 恢复按钮态
    expect(screen.queryByTestId('preflight-result')).not.toBeInTheDocument() // 不伪造校验结果
  }, 30_000)

  it('⑤ 提交导入 404 → toast 建设中文案 + 提交按钮恢复 + 弹窗不关闭', async () => {
    // 预校验改注成功（无 error 行）解锁「开始导入」；导入端点裸 404
    server.use(
      http.post('*/api/v1/ontologies/onto-outage/import/preflight', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: { ok: true, rows: [{ level: 'warning', line: 7, text: 'IRI 风格提示' }] },
        }),
      ),
      http.post('*/api/v1/ontologies/onto-outage/import', () => bare404()),
    )
    renderDialog()
    await chooseFile()

    fireEvent.click(screen.getByRole('button', { name: '预校验' }))
    const submit = await waitFor(() => {
      const b = screen.getByTestId('import-submit')
      expect(b).not.toBeDisabled()
      return b
    }, { timeout: 10_000 })

    fireEvent.click(submit)
    expect(submit).toBeDisabled()
    expect(await screen.findByText('导入管线端点未上线（建设中）', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(submit).toBeEnabled())
    // 弹窗未关闭（onQueued 未触发 → 不走成功关窗路径）
    expect(screen.getByTestId('import-submit')).toBeInTheDocument()
  }, 30_000)
})
