import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

/** 架构守卫（16 篇 §1 / 22 篇 CI 门禁的本地版）：features/* 域之间禁止横向 import，跨域只允许经 app/api/components。 */
const ROOT = join(__dirname, '../../src/features')

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap(name => {
    const p = join(dir, name)
    return statSync(p).isDirectory() ? walk(p) : p.endsWith('.tsx') || p.endsWith('.ts') ? [p] : []
  })
}

describe('feature 域边界', () => {
  it('features 域之间禁止横向 import', () => {
    const violations: string[] = []
    for (const domain of readdirSync(ROOT)) {
      const dir = join(ROOT, domain)
      if (!statSync(dir).isDirectory()) continue
      for (const file of walk(dir)) {
        const src = readFileSync(file, 'utf-8')
        const re = /from ['"]@\/features\/([a-z-]+)\//g
        for (const m of src.matchAll(re)) {
          if (m[1] !== domain) violations.push(`${file} → @/features/${m[1]}/`)
        }
      }
    }
    expect(violations).toEqual([])
  })
})
