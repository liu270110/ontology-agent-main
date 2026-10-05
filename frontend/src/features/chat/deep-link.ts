/**
 * E7/E9 实体深链 → chat 侧 @提及预填（41 篇 §2 V3.6/7「chat 侧输入预填@提及，最小通道」）。
 *
 * 参数口径（生产端=explore 域，消费端=ChatPage，本模块=两端共享的纯解析）：
 * - `entity`={iri}（E7 画布级引用/实体抽屉「去对话」，单实体）；
 * - `entities`=iri1,iri2（E9 框选子图「发送 N 个实体到对话」，逐个 encodeURIComponent）；
 * - `labels`=名1,名2（可选，与「entities…随后 entity」顺序逐位对齐的展示名）。
 *
 * labels 为何随链携带：chat 侧无 IRI→名称解析端点（B2 实体详情聚合未上线，
 * graphSearch 关键词联想不保证逐 IRI 命中），而生产端 explore 手里就有 label——
 * 由生产端随链带来是最小通道；缺位/位数不齐（外部手拼链、名称含逗号被切分）
 * 回退 IRI 尾段（#/ 分隔末段，IRI 自身的一部分，不虚构名称）。
 * 提及格式与工作区资源 @引用（WorkspacePanel `@${r.name}`）同款裸文本草稿语义，
 * M1-M3 无 @解析后端，仅作输入预填；请求体仍为纯文本 content。
 */

/** IRI 尾段：#/:// 分隔的末个非空段（http://example.org/grid#comp-A102 → comp-A102、
 *  urn:x:y → y）；全空（理论不可达）回退原 IRI。 */
export function iriTail(iri: string): string {
  return iri.split(/[#/:]/).filter(Boolean).pop() || iri
}

/** 解析深链参数 → @提及预填文本列表（无实体参数返回 null）。导出供回归单测守卫。 */
export function parseEntityDeepLink(params: URLSearchParams): string[] | null {
  const single = params.get('entity')?.trim() ?? ''
  const iris = [
    ...(params.get('entities') ?? '')
      .split(',')
      .map(s => s.trim())
      .filter(Boolean),
    ...(single ? [single] : []),
  ]
  if (iris.length === 0) return null
  const labels = (params.get('labels') ?? '')
    .split(',')
    .map(s => s.trim())
  return iris.map((iri, i) => `@${labels[i] || iriTail(iri)}`)
}
