import { useEffect, useRef, useState } from 'react'
import { Camera } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { useAuthStore } from '@/stores/auth-store'
import { getPreferences, putPreferences } from '../api'

/** IX-SET-01 资料 Tab（26 篇 §3）：头像上传（canvas 圆形裁剪简版）+ 姓名/邮箱（邮箱只读）
 *  + 语言/时区选择 + 保存按钮（脏态才可点）。姓名/部门自更新走 PUT /me/preferences
 *  扩展字段（§5.9 无自更新端点，预登记见 R 清单）。
 *  头像持久化：后端无头像端点（28 篇 §账号自助域预登记）→ localStorage `oa-avatar-<email>`
 *  （邮箱读 auth-store），页面加载时恢复；M4 账号域端点落地后换服务端存储。 */

/** 头像上限：5MB（非图片/超限 toast 拒绝） */
const AVATAR_MAX_BYTES = 5 * 1024 * 1024
/** 裁剪输出尺寸（px）：160px 圆形区域 */
const CROP_SIZE = 160

const avatarKey = (email: string) => `oa-avatar-${email}`

export function ProfileTab() {
  const [prefs, setPrefs] = useState<{ display_name: string; email: string; department: string; language: string; timezone: string } | null>(null)
  const [avatar, setAvatar] = useState<string | null>(null)
  /** 裁剪态：待裁剪图片 dataURL（非空时弹裁剪窗） */
  const [cropSrc, setCropSrc] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const email = useAuthStore(s => s.user?.email ?? '')

  useEffect(() => {
    void getPreferences().then(p =>
      setPrefs({ display_name: p.display_name, email: p.email, department: p.department, language: p.language, timezone: p.timezone }),
    )
  }, [])

  // 页面加载时恢复本地头像（M4 换头像端点后改拉服务端）
  useEffect(() => {
    if (!email) return
    try {
      setAvatar(localStorage.getItem(avatarKey(email)))
    } catch {
      /* localStorage 不可用（隐私模式等）：仅内存态 */
    }
  }, [email])

  if (!prefs) return <div className="empty"><div className="t">加载中…</div></div>

  const patch = (p: Partial<typeof prefs>) => { setPrefs({ ...prefs, ...p }); setDirty(true) }

  /** 选择文件 → 校验（类型/大小）→ 读 dataURL 进裁剪态 */
  const onPickFile = (f: File | undefined) => {
    if (!f) return
    if (!f.type.startsWith('image/')) {
      toast.error('仅支持图片文件（JPG/PNG）')
      return
    }
    if (f.size > AVATAR_MAX_BYTES) {
      toast.error('图片不能超过 5MB，请压缩后重试')
      return
    }
    const reader = new FileReader()
    reader.onload = () => setCropSrc(String(reader.result))
    reader.onerror = () => toast.error('图片读取失败，请重试')
    reader.readAsDataURL(f)
  }

  /** 裁剪确认：dataURL 作为头像预览并落 localStorage（M4 换端点） */
  const confirmAvatar = (dataUrl: string) => {
    setAvatar(dataUrl)
    try {
      if (email) localStorage.setItem(avatarKey(email), dataUrl)
    } catch {
      /* 存储失败不阻塞预览 */
    }
    setCropSrc(null)
    toast.success('头像已更新')
  }

  const save = async () => {
    setSaving(true)
    try {
      await putPreferences(prefs)
      toast.success('资料已保存')
      setDirty(false)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <b className="text-sm">资料</b>

      {/* 头像（选择本地文件 → 圆形裁剪 → dataURL 预览；服务端上传随 28 篇排期） */}
      <div className="mt-4 flex items-center gap-4">
        <span className="flex h-16 w-16 items-center justify-center overflow-hidden rounded-full bg-accent-soft text-xl font-semibold text-accent">
          {avatar
            ? <img src={avatar} alt="头像预览" data-testid="set-avatar-img" className="h-full w-full object-cover" />
            : (prefs.display_name || prefs.email)[0]?.toUpperCase()}
        </span>
        <div>
          <label className="btn btn-g btn-sm cursor-pointer">
            <Camera size={13} aria-hidden /> 上传头像
            <input
              type="file"
              accept="image/*"
              className="hidden"
              onChange={e => {
                onPickFile(e.target.files?.[0])
                e.target.value = ''
              }}
            />
          </label>
          <div className="fhint mt-1">支持 JPG/PNG，不超过 5MB，圆形裁剪后生效。</div>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-x-4 sm:grid-cols-2">
        <div className="field">
          <label className="field-label" htmlFor="set-profile-name">姓名</label>
          <input id="set-profile-name" data-testid="set-profile-name" className="input" value={prefs.display_name} onChange={e => patch({ display_name: e.target.value })} />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="set-profile-email">邮箱（只读）</label>
          <input id="set-profile-email" className="input text-label-3" value={prefs.email} readOnly aria-readonly />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="set-profile-lang">语言</label>
          <select id="set-profile-lang" className="input" value={prefs.language} onChange={e => patch({ language: e.target.value })}>
            <option value="zh-CN">简体中文</option>
            <option value="en-US">English (US)</option>
          </select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="set-profile-tz">时区</label>
          <select id="set-profile-tz" className="input" value={prefs.timezone} onChange={e => patch({ timezone: e.target.value })}>
            <option value="Asia/Shanghai">(GMT+8) 上海</option>
            <option value="UTC">(UTC) 协调世界时</option>
            <option value="America/New_York">(GMT-5) 纽约</option>
          </select>
        </div>
      </div>

      <div className="hairline-t mt-2 flex justify-end pt-3">
        <button type="button" className="btn btn-p" data-testid="set-profile-save" disabled={!dirty || saving} onClick={() => void save()}>
          保存{dirty ? '' : '（无修改）'}
        </button>
      </div>

      {cropSrc && (
        <AvatarCropper
          src={cropSrc}
          onConfirm={confirmAvatar}
          onCancel={() => setCropSrc(null)}
        />
      )}

      <AboutCard />
    </div>
  )
}

/** 头像圆形裁剪（canvas 简版）：160px 圆形遮罩 + 图片居中 cover 填充 + 缩放滑杆 50%~200%。
 *  确认 → canvas.toDataURL('image/png') 交回调（M4 换服务端上传）。 */
function AvatarCropper({ src, onConfirm, onCancel }: { src: string; onConfirm: (dataUrl: string) => void; onCancel: () => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [zoom, setZoom] = useState(1)

  // 加载原图（onload 后才可确认；jsdom 等无加载器环境由测试桩注入）
  useEffect(() => {
    let cancelled = false
    const img = new Image()
    img.onload = () => {
      if (cancelled) return
      imgRef.current = img
      setLoaded(true)
    }
    img.onerror = () => {
      if (!cancelled) toast.error('图片加载失败，请重试')
    }
    img.src = src
    return () => { cancelled = true }
  }, [src])

  // 绘制：圆形裁剪区（arc clip）内居中 cover 填充，随缩放滑杆重绘
  useEffect(() => {
    const canvas = canvasRef.current
    const img = imgRef.current
    if (!canvas || !img) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return // 无 2D 实现（如 jsdom 未装 canvas）：跳过绘制，不影响确认回调
    canvas.width = CROP_SIZE
    canvas.height = CROP_SIZE
    ctx.clearRect(0, 0, CROP_SIZE, CROP_SIZE)
    ctx.save()
    ctx.beginPath()
    ctx.arc(CROP_SIZE / 2, CROP_SIZE / 2, CROP_SIZE / 2, 0, Math.PI * 2)
    ctx.clip()
    const scale = Math.max(CROP_SIZE / img.naturalWidth, CROP_SIZE / img.naturalHeight) * zoom
    const w = img.naturalWidth * scale
    const h = img.naturalHeight * scale
    ctx.drawImage(img, (CROP_SIZE - w) / 2, (CROP_SIZE - h) / 2, w, h)
    ctx.restore()
  }, [zoom, loaded])

  const confirm = () => {
    try {
      const url = canvasRef.current?.toDataURL('image/png')
      if (!url || url === 'data:,') throw new Error('裁剪产物为空')
      onConfirm(url)
    } catch {
      toast.error('头像裁剪失败，请重试')
    }
  }

  return (
    <Modal
      open
      onClose={onCancel}
      title="裁剪头像"
      width={400}
      footer={
        <>
          <button type="button" className="btn btn-g" data-testid="set-avatar-cancel" onClick={onCancel}>取消</button>
          <button type="button" className="btn btn-p" data-testid="set-avatar-confirm" disabled={!loaded} onClick={confirm}>确认</button>
        </>
      }
    >
      {/* 圆形遮罩预览：canvas 内容即圆形（arc clip），圆角样式仅为容器兜底 */}
      <canvas
        ref={canvasRef}
        data-testid="set-avatar-canvas"
        className="mx-auto block rounded-full bg-surface-2"
        style={{ width: CROP_SIZE, height: CROP_SIZE }}
      />
      <div className="field mt-4 mb-0">
        <label className="field-label" htmlFor="set-avatar-zoom">
          缩放：<b className="text-accent">{Math.round(zoom * 100)}%</b>
        </label>
        <input
          id="set-avatar-zoom"
          data-testid="set-avatar-zoom"
          type="range"
          min={0.5}
          max={2}
          step={0.05}
          value={zoom}
          onChange={e => setZoom(Number(e.target.value))}
          className="w-full accent-[var(--accent)]"
        />
      </div>
    </Modal>
  )
}

/** 关于（版本单源=package.json，经 vite define 注入；桌面端经 preload appInfo 桥取壳版本对照）。 */
function AboutCard() {
  const [desktop, setDesktop] = useState<{ version: string; platform: string } | null>(null)
  useEffect(() => {
    // 非 Electron 环境 window.oaDesktop 不存在（types.d.ts ambient 可选桥）
    void (window as { oaDesktop?: { appInfo(): Promise<{ version: string; platform: string }> } }).oaDesktop
      ?.appInfo()
      .then(setDesktop)
      .catch(() => {})
  }, [])

  const buildTime = (() => {
    try {
      return new Date(__BUILD_TIME__).toLocaleString('zh-CN', { hour12: false })
    } catch {
      return __BUILD_TIME__
    }
  })()

  return (
    <div className="card mt-4 p-4" data-testid="about-card">
      <b className="text-sm">关于</b>
      <dl className="mt-2 text-[12px]">
        <div className="flex justify-between border-b border-separator py-1.5">
          <dt className="text-label-3">应用版本</dt>
          <dd className="mono" data-testid="about-version">{__APP_VERSION__}</dd>
        </div>
        <div className="flex justify-between border-b border-separator py-1.5">
          <dt className="text-label-3">构建时间</dt>
          <dd className="mono" data-testid="about-build-time">{buildTime}</dd>
        </div>
        <div className="flex justify-between py-1.5">
          <dt className="text-label-3">运行环境</dt>
          <dd className="mono" data-testid="about-runtime">
            {desktop ? `桌面端 · Electron ${desktop.version} · ${desktop.platform}` : 'Web'}
          </dd>
        </div>
      </dl>
    </div>
  )
}
