import { beforeEach, describe, expect, it } from 'vitest'
import { useSessionStore, type ChatMessage } from '@/stores/session-store'
import { useGroupStreamStore, type GroupMessage } from '@/features/group/group-store'

/** F1（联调缺陷 2026-10-06）消息时序倒置回归：GET messages 返回 seq 降序，seed 原样直塞
 *  → 界面「用户问在助手答下方」。修复=seed 内按 seq 升序排序（session-store 与群聊
 *  group-store 同款）；无 seq 的实时残缺消息垫后（对齐 backfill「历史升序在前、实时在后」口径）。 */

function resetStores() {
  useSessionStore.setState({ messages: [], lastSeq: 0 })
  useGroupStreamStore.setState({ messages: [], lastSeq: 0 })
}

describe('F1 seed 消息时序（seq 升序）', () => {
  beforeEach(resetStores)

  it('session-store：降序历史入参 → 存储升序（用户问在助手答上方）', () => {
    // GET /sessions/{id}/messages 实测 seq 降序返回（缺陷台账 F1）
    const history: ChatMessage[] = [
      { id: 'm3', role: 'assistant', content: '答', seq: 3 },
      { id: 'm1', role: 'user', content: '问', seq: 1 },
      { id: 'm2', role: 'assistant', content: '中', seq: 2 },
    ]
    useSessionStore.getState().seed(history, 3)
    const msgs = useSessionStore.getState().messages
    expect(msgs.map(m => m.seq)).toEqual([1, 2, 3])
    expect(msgs[0]).toMatchObject({ role: 'user', content: '问' })
    // lastSeq 语义不变
    expect(useSessionStore.getState().lastSeq).toBe(3)
  })

  it('session-store：无 seq 实时残缺消息垫后（对齐 backfill 口径），入参数组不被原地改写', () => {
    const live: ChatMessage = { id: 'live-1', role: 'assistant', content: '直播中半条' }
    const history: ChatMessage[] = [live, { id: 'm2', role: 'user', content: '先问', seq: 2 }]
    useSessionStore.getState().seed(history, 2)
    const msgs = useSessionStore.getState().messages
    expect(msgs.map(m => m.id)).toEqual(['m2', 'live-1'])
    // 排序作用于副本——调用方数组顺序不动
    expect(history.map(m => m.id)).toEqual(['live-1', 'm2'])
  })

  it('group-store：群聊 seed 同款升序（features/group 同修）', () => {
    const history: GroupMessage[] = [
      { id: 'g3', role: 'assistant', content: '成员乙答', seq: 3, agent_id: 'a-2' },
      { id: 'g1', role: 'user', content: '发起提问', seq: 1 },
      { id: 'g2', role: 'assistant', content: '成员甲答', seq: 2, agent_id: 'a-1' },
    ]
    useGroupStreamStore.getState().seed(history)
    const msgs = useGroupStreamStore.getState().messages
    expect(msgs.map(m => m.seq)).toEqual([1, 2, 3])
    expect(msgs[0]).toMatchObject({ role: 'user', content: '发起提问' })
    expect(useGroupStreamStore.getState().lastSeq).toBe(3)
  })
})
