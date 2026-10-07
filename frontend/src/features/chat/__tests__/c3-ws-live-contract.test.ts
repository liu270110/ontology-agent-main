import { afterEach, describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { workspaceApi } from '../workspace'
import { server } from '@/mocks/node'

/** C3 契约测试：Agent 工作区面板四端点 **live 投影字段名级**断言。
 *  权威=worktree 后端实装（services/agent/api/workspace.py + schemas/workspace.py，31 篇 M1；
 *  openapi 快照 b82a76c 显式确认），mock（mocks/handlers.ts 工作区段）已改写为 live 同构投影：
 *  信封=裸 DTO/{items}（无 code 字段，apiFetch 双形态兼容直取）——
 *  ① GET tree：{recycle_in_minutes(null), root}，文件节点 size/updated_at(ISO)、路径 /workspace 前缀；
 *  ② GET file：{path,language,content}；二进制 415/3004 拒读；缺文件 404；
 *  ③ POST exec：{command,exit_code,lines}；白名单外 4001 结构化拒绝（TERMINAL_CMD_NOT_ALLOWED）；
 *  ④ GET resources：{items:[{id=ws-<sha12>,type='artifact',...,uploaded_by='sandbox'}]}，
 *     无 ocr_status/extract_status（artifacts 表批回归）。
 *  直调 workspaceApi 消费面（经 MSW node 拦截默认 live 投影 mock），形态=fe-exec 同款 api 级。 */
afterEach(() => {
  server.resetHandlers()
})

describe('C3 工作区四端点 live 投影契约（字段名级）', () => {
  it('tree：裸 DTO；recycle_in_minutes 恒 null（不放假倒计时）；节点名级与 WsNodeOut 一致', async () => {
    const t = await workspaceApi.tree('s-2481')
    expect(t.recycle_in_minutes).toBeNull()
    expect(t.root).toMatchObject({ name: '/workspace/', path: '/', type: 'dir' })
    const artifacts = t.root.children?.find(c => c.name === 'artifacts')
    expect(artifacts?.type).toBe('dir')
    const file = t.root.children?.find(c => c.name === '台账导出.xlsx')
    expect(file).toMatchObject({ path: '/workspace/台账导出.xlsx', type: 'file', size: 860288 })
    expect(typeof file?.updated_at).toBe('string')
    // ISO8601（可解析；live 为 datetime.fromtimestamp(...).isoformat() 形态）
    expect(Number.isNaN(new Date(file?.updated_at ?? '').getTime())).toBe(false)
    // live v1 无改动追踪：树节点不携带 dirty（WS 批接入）
    expect(JSON.stringify(t)).not.toContain('"dirty"')
  })

  it('file：{path,language,content} 裸 DTO；二进制 415/3004；缺文件 404', async () => {
    const f = await workspaceApi.file('s-2481', '/workspace/artifacts/台账数据.json')
    expect(Object.keys(f).sort()).toEqual(['content', 'language', 'path'])
    expect(f).toMatchObject({ path: '/workspace/artifacts/台账数据.json', language: 'json' })
    expect(f.content).toContain('defects')

    await expect(workspaceApi.file('s-2481', '/workspace/台账导出.xlsx')).rejects.toMatchObject({
      code: 3004,
      httpStatus: 415,
    })
    await expect(workspaceApi.file('s-2481', '/workspace/不存在.md')).rejects.toMatchObject({
      code: 404,
      httpStatus: 404,
    })
  })

  it('exec：{command,exit_code,lines} 裸 DTO；pwd 内建仿真；白名单外 4001 拒绝', async () => {
    const pwd = await workspaceApi.exec('s-2481', 'pwd')
    expect(pwd).toEqual({ command: 'pwd', exit_code: 0, lines: ['/workspace'] })

    const ls = await workspaceApi.exec('s-2481', 'ls')
    expect(ls.exit_code).toBe(0)
    expect(ls.lines).toContain('台账导出.xlsx')

    try {
      await workspaceApi.exec('s-2481', 'rm -rf /')
      expect.unreachable('应抛 ApiError 4001')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      expect((e as ApiError).code).toBe(4001)
      expect((e as ApiError).httpStatus).toBe(400)
      expect((e as ApiError).message).toContain("命令 'rm' 不在只读白名单")
    }

    // 参数工作区锁定：绝对路径 / .. 穿越同 4001（live _confine_arg 同口径）
    await expect(workspaceApi.exec('s-2481', 'cat /etc/passwd')).rejects.toMatchObject({ code: 4001 })
    await expect(workspaceApi.exec('s-2481', 'cat ../secrets.txt')).rejects.toMatchObject({ code: 4001 })
  })

  it('resources：{items} 裸列表；M1 顶层产出=artifact/sandbox/ws-<sha12>；无 ocr/extract 附属键', async () => {
    const r = await workspaceApi.resources('s-2481')
    expect(Array.isArray(r.items)).toBe(true)
    expect(r.items.length).toBeGreaterThan(0)
    for (const item of r.items) {
      expect(item).toMatchObject({ type: 'artifact', status: 'ready', uploaded_by: 'sandbox' })
      expect(item.id).toMatch(/^ws-[0-9a-f]{12}$/)
      expect(typeof item.size).toBe('number')
      expect(Number.isNaN(new Date(item.created_at).getTime())).toBe(false)
      expect('ocr_status' in item).toBe(false)
      expect('extract_status' in item).toBe(false)
    }
    // mtime 降序（live 扫描序）
    const times = r.items.map(i => new Date(i.created_at).getTime())
    expect([...times].sort((a, b) => b - a)).toEqual(times)
  })
})
