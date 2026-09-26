/** query key 工厂（16 篇 §2.1）：三元组 [模块, 资源, 参数?]；模块/资源必须取此常量，禁止散写。 */
export const qk = {
  auth: {
    root: ['auth'] as const,
    me: ['auth', 'me'] as const,
  },
  session: {
    root: ['session'] as const,
    list: (params?: { cursor?: string }) => ['session', 'list', params ?? {}] as const,
    messages: (sessionId: string) => ['session', 'messages', sessionId] as const,
  },
  kb: {
    documents: (params?: { q?: string; cursor?: string }) => ['kb', 'documents', params ?? {}] as const,
  },
  ontology: {
    list: (params?: { q?: string; cursor?: string }) => ['ontology', 'list', params ?? {}] as const,
  },
} as const
