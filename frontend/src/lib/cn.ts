import { clsx, type ClassValue } from 'clsx'

/** 条件类名合并（clsx 单依赖版；tailwind-merge 未入台账，嵌套覆盖场景手写优先级）。 */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs)
}
