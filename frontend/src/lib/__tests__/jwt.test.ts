import { describe, expect, it } from 'vitest'
import { decodeJwtPayload, isTokenExpired } from '../jwt'

/** 无签名 JWT（与 mocks/handlers.ts makeJwt 同构：UTF-8 安全 base64url） */
function makeJwt(claims: Record<string, unknown>): string {
  const b64 = (o: unknown) =>
    btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(o))))
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '')
  return `${b64({ alg: 'none', typ: 'JWT' })}.${b64(claims)}.mock`
}

const BASE_CLAIMS = {
  sub: 'u-1',
  tenant_id: 't-1',
  roles: ['admin'],
  scopes: ['kb:read'],
  typ: 'access',
  exp: Math.floor(Date.now() / 1000) + 600,
}

describe('decodeJwtPayload', () => {
  it('中文 claims（name/tenant_name）按 UTF-8 还原不乱码（UI 问候语/用户卡事实源）', () => {
    const claims = decodeJwtPayload(makeJwt({ ...BASE_CLAIMS, name: '刘以在', tenant_name: '默认租户' }))
    expect(claims?.name).toBe('刘以在')
    expect(claims?.tenant_name).toBe('默认租户')
  })

  it('URL-safe base64（-_）与缺省 padding 可解', () => {
    // "刘以在" UTF-8 base64url 含 4Foi5ZGo 类片段；这里以长中文触发 padding 缺省路径
    const claims = decodeJwtPayload(makeJwt({ ...BASE_CLAIMS, name: '配电自动化知识工程师样例账户' }))
    expect(claims?.name).toBe('配电自动化知识工程师样例账户')
  })

  it('空/残缺/非法段返回 null 不抛错（展示层容错）', () => {
    expect(decodeJwtPayload(null)).toBeNull()
    expect(decodeJwtPayload('')).toBeNull()
    expect(decodeJwtPayload('a.b')).toBeNull()
    expect(decodeJwtPayload('h.not-base64!!broken.z')).toBeNull()
  })

  it('缺 sub/roles/scopes 的结构不合法返回 null', () => {
    expect(decodeJwtPayload(makeJwt({ typ: 'access' }))).toBeNull()
  })
})

describe('isTokenExpired', () => {
  it('未过期 false，已过期 true，缺 exp 视为过期', () => {
    expect(isTokenExpired(decodeJwtPayload(makeJwt(BASE_CLAIMS)))).toBe(false)
    expect(
      isTokenExpired(decodeJwtPayload(makeJwt({ ...BASE_CLAIMS, exp: Math.floor(Date.now() / 1000) - 60 }))),
    ).toBe(true)
    expect(isTokenExpired(null)).toBe(true)
  })
})
