import { OTPInput, OTPInputContext } from 'input-otp'
import { useContext, type ComponentProps } from 'react'
import { cn } from '@/lib/cn'

/** OTP 六位输入基元（input-otp 封装，样式基座=24 篇 §3 InputOTP 槽位形态）。
 *  注：按 S1 约束不触碰 src/design-system，暂居 components/；并入 design-system/ui 批次随 S8 收口迁移。
 *  jsdom 注意：单隐藏 input 承载输入，fireEvent.change(value:'123456') 即整段粘贴分发。 */

export function OtpInput({ containerClassName, className, ...props }: ComponentProps<typeof OTPInput>) {
  return (
    <OTPInput
      inputMode="numeric"
      containerClassName={cn('flex items-center gap-2 has-[:disabled]:opacity-50', containerClassName)}
      className={cn('disabled:cursor-not-allowed', className)}
      {...props}
    />
  )
}

export function OtpGroup({ className, ...props }: ComponentProps<'div'>) {
  return <div className={cn('flex items-center', className)} {...props} />
}

export function OtpSlot({ index, className }: { index: number; className?: string }) {
  const inputContext = useContext(OTPInputContext)
  const { char, hasFakeCaret, isActive } = inputContext.slots[index]

  return (
    <div
      className={cn(
        'relative flex h-12 w-10 items-center justify-center rounded-lg border text-base font-semibold transition-all',
        'border-separator bg-surface',
        isActive && 'border-accent ring-2 ring-accent/25',
        className,
      )}
      data-active={isActive || undefined}
    >
      {char}
      {hasFakeCaret && (
        <div className="pointer-events-none absolute inset-y-0 left-1/2 w-px animate-pulse bg-accent" />
      )}
    </div>
  )
}

export function OtpSeparator() {
  return <div className="mx-1 h-8 w-px self-center bg-separator" />
}
