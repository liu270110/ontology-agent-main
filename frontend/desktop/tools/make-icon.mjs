/**
 * 生成桌面壳图标资产（零依赖，纯 Node 内置 zlib 手写 PNG/ICO 容器）。
 * 产物：
 *   build/icon.png            256×256 应用图标（electron-builder buildResources）
 *   build/icon.ico            256×256 ICO（PNG 压缩条目，NSIS/任务栏用）
 *   electron/assets/icon.png  256×256（esbuild dataurl 内联 → BrowserWindow.icon）
 *   electron/assets/tray.png  16×16 托盘图标
 * 用法：node tools/make-icon.mjs
 */
import { deflateSync } from 'node:zlib'
import { writeFileSync, mkdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// ---------- PNG ----------
const CRC_TABLE = new Uint32Array(256)
for (let n = 0; n < 256; n++) {
  let c = n
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
  CRC_TABLE[n] = c >>> 0
}
function crc32(buf) {
  let c = 0xffffffff
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}
function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length, 0)
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body), 0)
  return Buffer.concat([len, body, crc])
}
/** pixelFn(x, y) → [r, g, b, a] */
function makePng(size, pixelFn) {
  const raw = Buffer.alloc((size * 4 + 1) * size)
  for (let y = 0; y < size; y++) {
    const row = y * (size * 4 + 1)
    raw[row] = 0 // filter: none
    for (let x = 0; x < size; x++) {
      const [r, g, b, a] = pixelFn(x, y)
      const o = row + 1 + x * 4
      raw[o] = r; raw[o + 1] = g; raw[o + 2] = b; raw[o + 3] = a
    }
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(size, 0)
  ihdr.writeUInt32BE(size, 4)
  ihdr[8] = 8   // bit depth
  ihdr[9] = 6   // color type: RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

// ---------- 图形：圆角方块底 + 白色同心圆（ontology 节点意象） ----------
const BG = [59, 130, 246]      // blue-500
const FG = [255, 255, 255]
function draw(size) {
  const r = size * 0.22          // 圆角半径
  const cx = size / 2, cy = size / 2
  const outer = size * 0.30      // 外环半径
  const inner = size * 0.13      // 内点半径
  const ring = size * 0.075      // 环厚
  return (x, y) => {
    // 圆角矩形 alpha
    const dx = Math.max(r - x, x - (size - 1 - r), 0)
    const dy = Math.max(r - y, y - (size - 1 - r), 0)
    if (Math.hypot(dx, dy) > r) return [0, 0, 0, 0]
    const d = Math.hypot(x - cx, y - cy)
    if (d <= inner) return [...FG, 255]
    if (Math.abs(d - outer) <= ring) return [...FG, 255]
    return [...BG, 255]
  }
}

// ---------- ICO（PNG 压缩条目包装） ----------
function icoFromPng(pngBuf) {
  const header = Buffer.alloc(6)
  header.writeUInt16LE(0, 0); header.writeUInt16LE(1, 2); header.writeUInt16LE(1, 4) // ICONDIR: 1 张图
  const entry = Buffer.alloc(16)
  entry[0] = 0; entry[1] = 0            // 宽/高 256 → 0 表示 256
  entry[2] = 0; entry[3] = 0            // 调色板/保留
  entry.writeUInt16LE(1, 4)             // planes
  entry.writeUInt16LE(32, 6)            // bpp
  entry.writeUInt32LE(pngBuf.length, 8) // 数据长度
  entry.writeUInt32LE(22, 12)           // 数据偏移 = 6 + 16
  return Buffer.concat([header, entry, pngBuf])
}

const icon256 = makePng(256, draw(256))
const tray16 = makePng(16, draw(16))

mkdirSync(path.join(root, 'build'), { recursive: true })
mkdirSync(path.join(root, 'electron', 'assets'), { recursive: true })
writeFileSync(path.join(root, 'build', 'icon.png'), icon256)
writeFileSync(path.join(root, 'build', 'icon.ico'), icoFromPng(icon256))
writeFileSync(path.join(root, 'electron', 'assets', 'icon.png'), icon256)
writeFileSync(path.join(root, 'electron', 'assets', 'tray.png'), tray16)
console.log('[make-icon] build/icon.png build/icon.ico electron/assets/icon.png electron/assets/tray.png 已生成')
