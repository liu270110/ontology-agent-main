import { useEffect, useState } from 'react'
import { Download, ShieldCheck, ShieldOff } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { OtpGroup, OtpInput, OtpSeparator, OtpSlot } from '@/components/otp-input'
import { getPreferences, totpBackupCodes, totpDisable, totpEnable, totpSetup } from '../api'

/** IX-SET-02 安全 Tab · 2FA（26 篇 §3）：状态卡两态——
 *  未启用 →「启用 2FA」向导（①扫码/密钥 ②六位验证码[复用 S1 InputOTP] ③备份码一次性展示）；
 *  已启用 → 查看备份码（需密码）/ 停用（需密码）。端点=api/01 §5.9 totp 三端点。 */

export function SecurityTab() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [wizardOpen, setWizardOpen] = useState(false)
  const [backupOpen, setBackupOpen] = useState(false)
  const [disableOpen, setDisableOpen] = useState(false)

  useEffect(() => {
    void getPreferences().then(p => setEnabled(p.totp_enabled))
  }, [])

  if (enabled === null) return <div className="empty"><div className="t">加载中…</div></div>

  return (
    <div>
      <b className="text-[14px]">安全</b>

      <div className="mt-3 flex items-center gap-3 rounded-xl border border-separator p-4" data-testid="set-2fa-card">
        <span className={`flex h-10 w-10 flex-none items-center justify-center rounded-xl ${enabled ? 'bg-[var(--green-soft)]' : 'bg-surface-2'}`} style={{ color: enabled ? 'var(--green)' : 'var(--label-3)' }}>
          {enabled ? <ShieldCheck size={18} aria-hidden /> : <ShieldOff size={18} aria-hidden />}
        </span>
        <div className="min-w-0 flex-1">
          <b className="text-[13px]">两步验证（TOTP）</b>
          <p className="mt-0.5 text-[11.5px] text-label-2">
            {enabled ? '已启用：登录密码校验通过后需输入 6 位动态码；备份码可离线应急。' : '未启用：启用后登录需两步验证，防止口令泄露导致的越权。'}
          </p>
        </div>
        {!enabled ? (
          <button type="button" className="btn btn-p" data-testid="set-2fa-enable" onClick={() => setWizardOpen(true)}>启用 2FA</button>
        ) : (
          <span className="flex gap-2">
            <button type="button" className="btn btn-g btn-sm" data-testid="set-2fa-backup" onClick={() => setBackupOpen(true)}>查看备份码</button>
            <button type="button" className="btn btn-d btn-sm" data-testid="set-2fa-disable" onClick={() => setDisableOpen(true)}>停用</button>
          </span>
        )}
      </div>

      {wizardOpen && <EnableWizard onClose={() => setWizardOpen(false)} onDone={() => { setWizardOpen(false); setEnabled(true) }} />}
      {backupOpen && <BackupCodesModal onClose={() => setBackupOpen(false)} />}
      {disableOpen && (
        <DisableModal
          onClose={() => setDisableOpen(false)}
          onDone={() => { setDisableOpen(false); setEnabled(false) }}
        />
      )}
    </div>
  )
}

/** 启用向导三步（Modal 容器 + steps；强制步骤不可跳过） */
function EnableWizard({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [step, setStep] = useState(1)
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null)
  const [code, setCode] = useState('')
  const [backupCodes, setBackupCodes] = useState<string[]>([])
  const [err, setErr] = useState('')

  // 第①步进入即生成 secret（POST /auth/totp/setup；一次性展示）
  useEffect(() => {
    totpSetup().then(setSetup).catch(e => toast.error((e as Error).message))
  }, [])

  const verify = async () => {
    if (code.length !== 6) {
      setErr('请输入 6 位验证码')
      return
    }
    try {
      const res = await totpEnable(code)
      setBackupCodes(res.backup_codes)
      setStep(3)
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  const downloadCodes = () => {
    const blob = new Blob([`ontology-agent 备份码（一次性）\n${backupCodes.join('\n')}\n`], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'ontology-agent-backup-codes.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="启用两步验证"
      width={520}
      footer={
        <>
          {step > 1 && <button type="button" className="btn btn-g" onClick={() => setStep(step - 1)}>上一步</button>}
          <span className="flex-1" />
          {step === 1 && (
            <button type="button" className="btn btn-p" data-testid="set-2fa-wiz-next" disabled={!setup} onClick={() => setStep(2)}>
              下一步
            </button>
          )}
          {step === 2 && (
            <button type="button" className="btn btn-p" data-testid="set-2fa-wiz-verify" disabled={code.length !== 6} onClick={() => void verify()}>
              验证并启用
            </button>
          )}
          {step === 3 && (
            <button type="button" className="btn btn-p" data-testid="set-2fa-wiz-done" onClick={onDone}>完成</button>
          )}
        </>
      }
    >
      {/* 步骤条 */}
      <div className="mb-4 flex items-center gap-2 text-[11px] text-label-3">
        {['扫码 / 密钥', '输入验证码', '备份码（一次性）'].map((s, i) => (
          <span key={s} className={`flex items-center gap-1.5 ${step === i + 1 ? 'font-semibold text-accent' : ''}`}>
            <span className={`flex h-5 w-5 items-center justify-center rounded-full ${step > i ? 'bg-accent text-white' : 'bg-surface-2'}`} style={step > i ? undefined : { color: 'var(--label-3)' }}>
              {i + 1}
            </span>
            {s}
            {i < 2 && <span className="mx-1 h-px w-6 bg-separator" aria-hidden />}
          </span>
        ))}
      </div>

      {step === 1 && setup && (
        <div>
          <div className="flex gap-4">
            {/* 二维码占位（qr 渲染随 28 篇接入；otpauth URI 已可手抄/扫码枪） */}
            <div className="flex h-32 w-32 flex-none items-center justify-center rounded-xl border border-dashed border-separator bg-surface-2 text-[10px] text-label-3" aria-label="二维码占位">
              二维码占位
            </div>
            <div className="min-w-0">
              <div className="field-label">无法扫码？手动输入密钥</div>
              <div className="keymask break-all" data-testid="set-2fa-secret">{setup.secret}</div>
              <div className="mt-2 break-all text-[10.5px] text-label-3">otpauth://totp/…secret={setup.secret}</div>
            </div>
          </div>
          <div className="fhint mt-2">请使用认证器 App（如 Microsoft Authenticator）添加后进入下一步。</div>
        </div>
      )}

      {step === 2 && (
        <div>
          <div className="field-label">输入认证器中的 6 位验证码</div>
          <OtpInput maxLength={6} value={code} onChange={v => { setCode(v); setErr('') }}>
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
          {err && <div className="field-err mt-2">{err}</div>}
        </div>
      )}

      {step === 3 && (
        <div>
          <div className="al-warn alert">
            <div><b>备份码仅本次展示</b>关闭弹窗后不再显示，请复制/下载并保存到密码管理器。</div>
          </div>
          <div className="mono mt-3 grid grid-cols-4 gap-2" data-testid="set-2fa-backup-codes">
            {backupCodes.map(c => <span key={c} className="keymask text-center">{c}</span>)}
          </div>
          <div className="mt-3 flex gap-2">
            <button type="button" className="btn btn-g btn-sm" onClick={() => { void navigator.clipboard?.writeText(backupCodes.join('\n')).catch(() => {}); toast.success('备份码已复制') }}>
              复制
            </button>
            <button type="button" className="btn btn-g btn-sm" onClick={downloadCodes}>
              <Download size={12} aria-hidden /> 下载
            </button>
          </div>
        </div>
      )}
    </Modal>
  )
}

/** 已启用：查看备份码（重新生成需密码；预登记端点） */
function BackupCodesModal({ onClose }: { onClose: () => void }) {
  const [password, setPassword] = useState('')
  const [codes, setCodes] = useState<string[] | null>(null)
  const [err, setErr] = useState('')

  const regen = async () => {
    try {
      setCodes((await totpBackupCodes(password)).backup_codes)
      setErr('')
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="查看备份码"
      width={440}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>关闭</button>
          {!codes && (
            <button type="button" className="btn btn-p" data-testid="set-2fa-backup-regen" disabled={password.length < 6} onClick={() => void regen()}>
              验证并重新生成
            </button>
          )}
        </>
      }
    >
      {codes ? (
        <div>
          <div className="al-warn alert"><div><b>重新生成成功 · 旧备份码已全部失效</b>请保存新码（仅本次展示）。</div></div>
          <div className="mono mt-3 grid grid-cols-4 gap-2">
            {codes.map(c => <span key={c} className="keymask text-center">{c}</span>)}
          </div>
        </div>
      ) : (
        <div>
          <div className="field">
            <label className="field-label" htmlFor="set-2fa-pw">登录密码</label>
            <input id="set-2fa-pw" data-testid="set-2fa-backup-pw" className="input" type="password" value={password} onChange={e => setPassword(e.target.value)} />
            {err && <div className="field-err">{err}</div>}
          </div>
          <div className="fhint">重新生成会使旧备份码全部失效（审计留痕）。</div>
        </div>
      )}
    </Modal>
  )
}

/** 停用（danger）：需密码验证。 */
function DisableModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const mutation = async () => {
    try {
      await totpDisable(password)
      toast.success('两步验证已停用（写审计）')
      onDone()
    } catch (e) {
      setErr((e as Error).message)
    }
  }
  return (
    <Modal
      open
      danger
      onClose={onClose}
      title="停用两步验证"
      width={440}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-d" data-testid="set-2fa-disable-confirm" disabled={password.length < 6} onClick={() => void mutation()}>确认停用</button>
        </>
      }
    >
      <div className="al-err alert"><div><b>安全降级</b>停用后登录仅凭密码即可进入，建议仅在更换认证器时临时停用。</div></div>
      <div className="field mt-3">
        <label className="field-label" htmlFor="set-2fa-disable-pw">登录密码</label>
        <input id="set-2fa-disable-pw" data-testid="set-2fa-disable-pw" className="input" type="password" value={password} onChange={e => setPassword(e.target.value)} />
        {err && <div className="field-err">{err}</div>}
      </div>
    </Modal>
  )
}
