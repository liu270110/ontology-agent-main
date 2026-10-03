import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { RotateCcw, Save } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonRows } from '@/components/states'
import { getRoleMatrix, saveRoleMatrix } from '../api'

/** 角色 Tab（26 篇 §10.2 p-roles）：IX-ADM-04 RBAC 矩阵（角色 × 权限点勾选格）——
 *  勾选即时乐观更新高亮 + 底部变更摘要条（+N/−M 权限点 + 影响用户数）+ 保存 / 还原；
 *  保存后提示「菜单与路由即时生效」。矩阵读写为预登记端点（见 R 清单）。 */

interface Change { role: string; permission: string; granted: boolean }

export function RolesTab() {
  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ['admin', 'roles', 'matrix'], queryFn: getRoleMatrix })
  /** 乐观更新层：`role|perm` → 勾选值（保存成功后清空对齐服务端） */
  const [changes, setChanges] = useState<Record<string, boolean>>({})

  const roles = data?.roles ?? []
  const permissions = data?.permissions ?? []
  const matrix = data?.matrix

  const keyOf = (role: string, perm: string) => `${role}|${perm}`
  const valueOf = (role: string, perm: string) => changes[keyOf(role, perm)] ?? matrix?.[role]?.[perm] ?? false
  const isChanged = (role: string, perm: string) => {
    if (!matrix) return false
    const cur = changes[keyOf(role, perm)]
    return cur !== undefined && cur !== (matrix[role]?.[perm] ?? false)
  }

  const changeList = useMemo<Change[]>(() => {
    if (!matrix) return []
    return Object.entries(changes)
      .filter(([k, v]) => {
        const [role, perm] = k.split('|')
        return (matrix[role]?.[perm] ?? false) !== v
      })
      .map(([k, granted]) => {
        const [role, permission] = k.split('|')
        return { role, permission, granted }
      })
  }, [changes, matrix])

  const added = changeList.filter(c => c.granted).length
  const removed = changeList.filter(c => !c.granted).length
  const affectedRoles = new Set(changeList.map(c => c.role))
  const affectedUsers = roles.filter(r => affectedRoles.has(r.key)).reduce((sum, r) => sum + r.affected, 0)

  const mutation = useMutation({
    mutationFn: () => saveRoleMatrix(changeList),
    onSuccess: res => {
      toast.success(`已保存 ${res.applied} 项变更：菜单与路由即时生效（写审计）`)
      setChanges({})
      void refetch()
    },
    onError: e => toast.error(e.message),
  })

  // 状态完备（S8 切片同款）：失败 → ErrorState 可重试（原实现失败永锁「加载中」）；
  // 加载走 SkeletonRows 基元（.empty 是空态模式，不用于加载态）
  if (isError) {
    return (
      <div className="mt-3">
        <ErrorState
          title="权限矩阵加载失败"
          message={error instanceof Error ? error.message : undefined}
          code={error instanceof ApiError ? error.code : undefined}
          onRetry={() => void refetch()}
        />
      </div>
    )
  }
  if (isLoading || !matrix) {
    return <div className="card mt-3 px-4 py-3"><SkeletonRows rows={6} rowHeight={30} /></div>
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <b className="text-sm">RBAC 权限矩阵</b>
        <span className="badge b-gray">{roles.length} 角色 × {permissions.length} 权限点</span>
        <span className="ml-auto flex items-center gap-3 text-[11px] text-label-3">
          <span className="flex items-center gap-1"><i className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: 'var(--green)' }} />本次新增</span>
          <span className="flex items-center gap-1"><i className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: 'var(--red)' }} />本次移除</span>
        </span>
      </div>

      <div className="card mt-3 overflow-x-auto !p-0">
        <table className="w-full min-w-[760px] text-xs" data-testid="adm-matrix">
          <thead>
            <tr className="hairline-b text-left text-[11px] text-label-3">
              <th className="px-4 py-2.5 font-semibold">权限点 \ 角色</th>
              {roles.map(r => (
                <th key={r.key} className="px-3 py-2.5 text-center font-semibold">
                  {r.label}
                  <span className="mono block text-2xs font-normal text-label-3">{r.key} · {r.affected} 人</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {permissions.map(p => (
              <tr key={p.key} className="hairline-b">
                <td className="px-4 py-2">
                  {p.label}
                  <span className="mono block text-2xs text-label-3">{p.key}</span>
                </td>
                {roles.map(r => {
                  const changed = isChanged(r.key, p.key)
                  const granted = valueOf(r.key, p.key)
                  return (
                    <td key={r.key} className="perm px-3 py-2 text-center">
                      {/* 变更高亮改 box-shadow：outline 语义留给 focus-visible 焦点环（阶段1全局 :focus-visible），二者不再互相遮挡 */}
                      <input
                        type="checkbox"
                        aria-label={`${r.label} ${p.label}`}
                        data-testid={`adm-matrix-${r.key}-${p.key}`}
                        checked={granted}
                        onChange={e => {
                          // 乐观更新：勾选即时生效（高亮描边），底部摘要条 +N/−M，保存前可还原
                          setChanges(prev => ({ ...prev, [keyOf(r.key, p.key)]: e.target.checked }))
                        }}
                        style={changed
                          ? { boxShadow: `0 0 0 2px ${granted ? 'var(--green)' : 'var(--red)'}`, borderRadius: 4 }
                          : undefined}
                      />
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 底部变更摘要条（有变更时浮出） */}
      {changeList.length > 0 && (
        <div className="batchbar mt-4 !justify-between" data-testid="adm-matrix-bar">
          <span data-testid="adm-matrix-summary">
            变更摘要：+{added} / −{removed} 权限点 · 影响 {affectedUsers} 名用户
          </span>
          <span className="flex gap-2">
            <button type="button" className="btn btn-g btn-sm" data-testid="adm-matrix-reset" onClick={() => setChanges({})}>
              <RotateCcw size={12} aria-hidden /> 还原
            </button>
            <button type="button" className="btn btn-p btn-sm" data-testid="adm-matrix-save" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
              <Save size={12} aria-hidden /> 保存变更
            </button>
          </span>
        </div>
      )}
      <p className="mt-3 text-[11px] text-label-3">
        勾选即时乐观更新；保存后菜单与路由即时生效并写审计，还原则丢弃乐观态。权限点全集对齐 11 篇 §2 Permission 字典。
      </p>
    </div>
  )
}
