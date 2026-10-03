/** 群聊发言编排词表（契约=api/01 §5.2 sessions 群聊扩展，X15 预登记）。
 *  group 域（路由选择器/建群）与设置页对话偏好 Tab（group_routing_default 默认值）
 *  跨域共用 → 按 02 篇 §4 下沉共享层 src/lib，features/group/api 经再导出保持域内
 *  引用面不变（16 篇 §1 架构守卫：features 域间禁止横向 import）。 */

export type RoutingMode = 'mention' | 'round_robin' | 'all' | 'orchestrator'

export const ROUTING_LABEL: Record<RoutingMode, string> = {
  mention: '@点名', round_robin: '轮询', all: '多答对比', orchestrator: '协调者',
}
