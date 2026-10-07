import { useState } from 'react'
import { Check, KeyRound, Lock, ShieldCheck, TriangleAlert } from 'lucide-react'
import { useAuthStore } from '@/stores/auth-store'
import { describeError } from '@/lib/errors'
import { OtpGroup, OtpInput, OtpSeparator, OtpSlot } from '@/components/otp-input'

/** 登录二步验证卡（26 篇 IX-ACC-01 / 28 篇 §2 / api/01 §5.9 X17 预登记）。
 *  步骤指示（1 密码·已完成 / 2 验证码·进行中）+ InputOTP 六位（粘贴自动分发）
 *  + 备份码 Tab 切换 +「信任此设备 30 天」勾选（M1 仅 localStorage 标记，
 *  device token 服务端化挂 R7，联动设置-设备列表）
 *  + 连续错误 3 次重锁提示（与密码步限速同池 5 次/10min，服务端兜底）。
 *  视觉基准 = 画板 ix-acc-01（玻璃卡 / 令牌色 / 主操作 tinted）。 */

const MAX_MFA_ATTEMPTS = 3

interface MfaStepCardProps {
  email: string
  /** 3 次重锁：回到密码步并提示（后端同池限速生效后以 1005 文案为准） */
  onBackToPassword: () => void
  onSuccess: () => void
}

export function MfaStepCard({ email, onBackToPassword, onSuccess }: MfaStepCardProps) {
  const confirmMfa = useAuthStore(s => s.confirmMfa)
  const resetMfa = useAuthStore(s => s.resetMfa)
  const [mode, setMode] = useState<'totp' | 'backup'>('totp')
  const [otp, setOtp] = useState('')
  const [backupCode, setBackupCode] = useState('')
  const [trustDevice, setTrustDevice] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [failCount, setFailCount] = useState(0)
  const [busy, setBusy] = useState(false)

  const codeReady = mode === 'totp' ? otp.length === 6 : backupCode.trim().length >= 6
  const locked = failCount >= MAX_MFA_ATTEMPTS

  async function submit() {
    if (!codeReady || busy) return
    setBusy(true)
    setError(null)
    try {
      await confirmMfa(mode === 'totp' ? otp : backupCode.trim(), trustDevice)
      onSuccess()
    } catch (err) {
      const next = failCount + 1
      setFailCount(next)
      setOtp('')
      setBackupCode('')
      if (next >= MAX_MFA_ATTEMPTS) {
        // 3 次重锁整个登录（与密码步限速同池，28 篇 §2 红线）
        setError('连续错误 3 次，登录已被临时锁定，请稍后再试或联系管理员')
        setTimeout(() => {
          resetMfa()
          onBackToPassword()
        }, 1200)
      } else {
        setError(describeError(err))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      onSubmit={e => {
        e.preventDefault()
        void submit()
      }}
      className="glass w-[360px] max-w-[92vw] rounded-3xl p-8"
      aria-label="两步验证"
    >
      <div className="flex flex-col items-center text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-accent text-white">
          <Lock size={22} aria-hidden />
        </div>
        <h2 className="mt-3 text-base font-bold">两步验证</h2>
        <p className="mt-1 text-xs leading-5 text-label-2">
          账号 {email} · 密码已通过，输入认证器动态码
        </p>
      </div>

      {/* 步骤指示：1 密码·已完成 → 2 验证码·进行中（ix-acc-01 同款） */}
      <ol className="my-5 flex items-center justify-center gap-2 text-[11px]" aria-label="登录步骤">
        <li className="flex items-center gap-1.5 text-green" aria-current="false">
          <span className="flex h-4 w-4 items-center justify-center rounded-full bg-green text-white">
            <Check size={11} strokeWidth={3} aria-hidden />
          </span>
          1 密码 · 已完成
        </li>
        <li className="h-px w-8 bg-separator" aria-hidden />
        <li className="flex items-center gap-1.5 font-semibold text-accent" aria-current="step">
          <span className="flex h-4 w-4 items-center justify-center rounded-full border-2 border-accent">
            <span className="h-1.5 w-1.5 rounded-full bg-accent" />
          </span>
          2 验证码 · 进行中
        </li>
      </ol>

      {/* 动态码 / 备份码 Tab（ix-acc-01：备份码 剩 8） */}
      <div className="mx-auto mb-4 flex w-fit rounded-lg bg-surface-2 p-1 text-xs" role="tablist" aria-label="验证方式">
        {(
          [
            ['totp', '动态码 (TOTP)'],
            ['backup', '备份码 · 剩 8'],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={mode === key}
            onClick={() => setMode(key)}
            className={`rounded-md px-3 py-1.5 font-medium transition-colors ${
              mode === key ? 'bg-surface text-label shadow-sm' : 'text-label-2 hover:text-label'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {mode === 'totp' ? (
        <div className="flex flex-col items-center">
          <OtpInput maxLength={6} value={otp} onChange={setOtp} disabled={locked} aria-label="六位动态验证码">
            <OtpGroup>
              <OtpSlot index={0} />
              <OtpSlot index={1} />
              <OtpSlot index={2} />
            </OtpGroup>
            <OtpSeparator />
            <OtpGroup>
              <OtpSlot index={3} />
              <OtpSlot index={4} />
              <OtpSlot index={5} />
            </OtpGroup>
          </OtpInput>
          <p className="mt-3 text-center text-[11px] text-label-3">
            支持粘贴 6 位验证码自动分发至各格；动态码 5 分钟内有效
          </p>
        </div>
      ) : (
        <div className="flex flex-col items-center">
          <input
            value={backupCode}
            onChange={e => setBackupCode(e.target.value)}
            disabled={locked}
            aria-label="八位备份码"
            placeholder="输入 8 位一次性备份码"
            maxLength={8}
            className="h-12 w-full rounded-lg border border-separator bg-surface px-3 text-center font-mono text-sm uppercase tracking-widest"
          />
          <p className="mt-3 text-center text-[11px] text-label-3">备份码一次性使用，成功后即作废</p>
        </div>
      )}

      {/* 信任此设备 30 天：M1 仅 localStorage 标记，device token 服务端化挂 R7 */}
      <label className="mt-5 flex items-center justify-center gap-2 text-xs text-label-2">
        <input
          type="checkbox"
          checked={trustDevice}
          onChange={e => setTrustDevice(e.target.checked)}
          className="h-3.5 w-3.5 accent-[var(--accent)]"
        />
        信任此设备 30 天（可在设置-会话设备吊销）
      </label>

      {/* 统一 1002 文案（不区分「账号不存在 / 密码错误 / 验证码错误」，防撞库） */}
      {error && (
        <div
          role="alert"
          className="mt-4 rounded-lg border border-red/40 bg-red/10 px-3 py-2.5 text-xs leading-5 text-red"
        >
          <b className="mb-0.5 flex items-center gap-1.5">
            <TriangleAlert size={13} aria-hidden /> 账号或验证码错误
          </b>
          {error}
        </div>
      )}

      {/* 连续错误计数（2/3 起以橙色提醒，ix-acc-01 同款） */}
      {failCount > 0 && !locked && (
        <div className="mt-3 rounded-lg border border-orange/40 bg-orange/10 px-3 py-2.5 text-xs leading-5 text-orange">
          <b className="mb-0.5 flex items-center gap-1.5">
            <KeyRound size={13} aria-hidden /> 连续错误 {MAX_MFA_ATTEMPTS} 次将重锁登录
          </b>
          当前已失败 {failCount}/{MAX_MFA_ATTEMPTS} 次；二步验证失败与密码步限速同池计数（5 次 / 10 分钟）。
        </div>
      )}

      <button
        type="submit"
        disabled={!codeReady || busy || locked}
        className="mt-5 flex h-11 w-full items-center justify-center gap-2 rounded-lg bg-accent text-sm font-semibold text-white transition-opacity disabled:opacity-50"
      >
        <ShieldCheck size={15} aria-hidden />
        {busy ? '验证中…' : '验证并登录'}
      </button>

      <div className="mt-4 flex items-center justify-between text-[11px]">
        <button
          type="button"
          className="text-accent hover:underline"
          onClick={() => {
            resetMfa()
            onBackToPassword()
          }}
        >
          返回上一步重新输入密码
        </button>
        <span className="text-label-3">邀请制注册 · 无账号请联系管理员</span>
      </div>
    </form>
  )
}
