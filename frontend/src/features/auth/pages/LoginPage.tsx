import { FormEvent, useState } from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuthStore, isTokenPair } from '@/stores/auth-store'
import { describeError } from '@/lib/errors'
import { MfaStepCard } from '../components/MfaStepCard'

/** 登录页（16 篇 §5.1 + 28 篇 §2 v1.8）：玻璃卡 + 双色光晕；zod 级校验先以内联规则实现。
 *  流程：zod 前置校验 → POST /auth/login → 成功写 auth-store → 按 ?next= 回跳（缺省 /）；
 *  返回 200 {mfa_required:true, mfa_token}（X17 预登记，后端 M1 未实现，live 不触发）→ MfaStepCard 二步。
 *  错误：1002 统一「邮箱或密码错误」防枚举；1005/429 限速提示含剩余时间（lib/errors 单点映射）。 */

/** 版本脚注（画板 p-login 基线同款；随 release 出版手动同步 package.json version） */
const VERSION_FOOTER = 'v0.1.0-m1 · build 20260926'

export function LoginPage() {
  const status = useAuthStore(s => s.status)
  const login = useAuthStore(s => s.login)
  const [params] = useSearchParams()
  const navigate = useNavigate()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  /** X17 两段式：密码步通过后挂起的一次性 mfa_token（null=密码步） */
  const [mfaToken, setMfaToken] = useState<string | null>(null)

  const nextPath = params.get('next') ?? params.get('redirect') ?? '/'

  if (status === 'authenticated' && !mfaToken) {
    return <Navigate to={nextPath} replace />
  }

  // 内联 zod 级校验（16 篇 §5.1：校验错误就近回填字段；MFA 契约模拟按密码 ≥6 位成功）
  const emailValid = /.+@.+\..+/.test(email)
  const passwordValid = password.length >= 6
  const valid = emailValid && passwordValid

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!valid || busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await login(email, password)
      if (isTokenPair(result)) {
        navigate(nextPath, { replace: true })
      } else {
        // 200 {mfa_required, mfa_token}：进入二步验证步（mfa_token 一次性）
        setMfaToken(result.mfa_token)
      }
    } catch (err) {
      setError(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  if (mfaToken) {
    return (
      <div className="login-stage relative flex min-h-screen items-center justify-center overflow-hidden bg-bg">
        <div className="login-wash lw1 blob-drift" />
        <div className="login-wash lw2 blob-drift" style={{ animationDelay: '-7s' }} />
        <MfaStepCard
          email={email}
          onSuccess={() => navigate(nextPath, { replace: true })}
          onBackToPassword={() => {
            setMfaToken(null)
            setError(null)
          }}
        />
      </div>
    )
  }

  return (
    <div className="login-stage relative flex min-h-screen items-center justify-center overflow-hidden bg-bg">
      <div className="login-wash lw1 blob-drift" />
      <div className="login-wash lw2 blob-drift" style={{ animationDelay: '-7s' }} />
      <form onSubmit={onSubmit} className="login-card glass glass-sheen-loop w-[360px] rounded-3xl p-8">
        <div className="login-logo flex h-10 w-10 items-center justify-center rounded-xl bg-accent-soft text-accent">◆</div>
        <h1 className="mt-4 text-lg font-bold">ontology-agent</h1>
        <p className="lsub mb-5 text-xs text-label-3">以本体为语义基座的智能体平台</p>
        {error && (
          <div role="alert" className="err-banner mb-4 flex items-center gap-2 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs text-red">
            ⚠ {error}
          </div>
        )}
        <div className="field mb-3">
          <label className="field-label mb-1 block text-xs text-label-2" htmlFor="email">邮箱</label>
          <input
            id="email"
            type="email"
            className="input w-full rounded-lg border border-separator bg-surface px-3 py-2 text-sm"
            placeholder="name@company.com"
            autoComplete="username"
            value={email}
            onChange={e => setEmail(e.target.value)}
          />
        </div>
        <div className="field mb-4">
          <label className="field-label mb-1 block text-xs text-label-2" htmlFor="password">密码</label>
          <input
            id="password"
            type="password"
            className="input w-full rounded-lg border border-separator bg-surface px-3 py-2 text-sm"
            autoComplete="current-password"
            value={password}
            onChange={e => setPassword(e.target.value)}
          />
          {password.length > 0 && password.length < 6 && (
            <p className="mt-1 text-[11px] text-orange">密码至少 6 位</p>
          )}
        </div>
        <button
          type="submit"
          disabled={!valid || busy}
          className="btn btn-p h-10 w-full rounded-lg bg-accent text-sm font-semibold text-white disabled:opacity-50"
        >
          {busy ? '登录中…' : '登 录'}
        </button>
        <p className="mt-4 text-center text-[11px] leading-5 text-label-3">
          账号由管理员邀请创建 · <span className="cursor-pointer text-label-2 hover:text-accent">忘记密码？</span>
          <br />
          登录即代表同意平台使用条款与审计策略
        </p>
        <p className="mt-3 text-center font-mono text-2xs text-label-3">{VERSION_FOOTER}</p>
      </form>
    </div>
  )
}
