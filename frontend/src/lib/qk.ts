/** query key 工厂（16 篇 §2.1）：三元组 [模块, 资源, 参数?]；模块/资源必须取此常量，禁止散写。 */
export const qk = {
  auth: {
    root: ['auth'] as const,
    me: ['auth', 'me'] as const,
  },
  session: {
    root: ['session'] as const,
    list: (params?: { cursor?: string; limit?: number }) => ['session', 'list', params ?? {}] as const,
    messages: (sessionId: string) => ['session', 'messages', sessionId] as const,
  },
  tasks: {
    root: ['tasks'] as const,
    list: (params?: { status?: string; type?: string; limit?: number }) => ['tasks', 'list', params ?? {}] as const,
  },
  kb: {
    documents: (params?: { q?: string; cursor?: string }) => ['kb', 'documents', params ?? {}] as const,
  },
  ontology: {
    list: (params?: { q?: string; cursor?: string }) => ['ontology', 'list', params ?? {}] as const,
  },
  /** 工作台聚合计数（S8 dashboard 真数据接入；列表键复用 session/tasks 域键带参形态） */
  dashboard: {
    pendingReviews: ['dashboard', 'pending-reviews'] as const,
    ontologyCount: ['dashboard', 'ontology-count'] as const,
    todaySessions: ['dashboard', 'today-sessions'] as const,
  },
} as const
