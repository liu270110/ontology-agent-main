import { ShieldBan } from 'lucide-react'

/** 403 占位页（16 篇 §4.2 步骤 3 / §7 2008）：判定不足渲染 403，不重定向（防循环）。
 *  「申请权限」为占位入口——权限申请流未登记端点，随治理域（S6）切片接线。 */
export function ForbiddenPage({ permission }: { permission?: string }) {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center" role="alert">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-red/10 text-red">
        <ShieldBan size={26} aria-hidden />
      </div>
      <h1 className="mt-4 text-base font-bold">没有执行此操作的权限</h1>
      <p className="mt-1.5 max-w-sm text-xs leading-5 text-label-2">
        您当前的角色不具备访问此页面所需的权限
        {permission && <code className="mx-1 rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px]">{permission}</code>}
        。如需访问，请向租户管理员申请。
      </p>
      <button
        type="button"
        title="权限申请流随 S6 治理域切片开放（当前占位）"
        className="mt-5 h-9 rounded-lg bg-accent px-4 text-xs font-semibold text-white disabled:opacity-60"
        disabled
      >
        申请权限
      </button>
    </div>
  )
}
