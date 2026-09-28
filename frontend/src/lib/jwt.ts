/** JWT claims 解码（08 篇 §2.1 权威 = services/gateway/security.py build_claims）：
 *  sub / tenant_id / roles / scopes / typ / jti / iat / exp（+iss/aud）。
 *  前端只解 payload 做展示与菜单过滤，不做验签（铁律：前端隐藏≠授权，服务端 PDP 兜底）。 */

/** access token claims（本前端消费的子集）。 */
export interface AccessClaims {
  sub: string
  tenant_id: string
  /** 显示名（R13：后端 claims 扩展预登记；缺失时前端回退邮箱前缀） */
  name?: string
  tenant_name?: string
  roles: string[]
  scopes: string[]
  typ: string
  /** 过期时间（秒级 Unix 时间戳） */
  exp: number
  iat?: number
  jti?: string
}

/** 令牌对响应（services/gateway/schemas/auth.py TokenPairResponse）。 */
export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  /** access 剩余秒数 */
  expires_in: number
}

function base64UrlDecode(segment: string): string {
  const normalized = segment.replace(/-/g, '+').replace(/_/g, '/')
  const padded = normalized + '='.repeat((4 - (normalized.length % 4)) % 4)
  return atob(padded)
}

/** 解 JWT payload；格式非法返回 null（不抛错——展示层容错）。 */
export function decodeJwtPayload(token: string | null | undefined): AccessClaims | null {
  if (!token) return null
  const parts = token.split('.')
  if (parts.length !== 3) return null
  try {
    const claims = JSON.parse(base64UrlDecode(parts[1])) as Partial<AccessClaims>
    if (!claims.sub || !Array.isArray(claims.roles) || !Array.isArray(claims.scopes)) return null
    return claims as AccessClaims
  } catch {
    return null
  }
}

/** access 是否已过期（30s 时钟偏移余量）。 */
export function isTokenExpired(claims: AccessClaims | null): boolean {
  if (!claims?.exp) return true
  return claims.exp * 1000 <= Date.now() + 30_000
}
