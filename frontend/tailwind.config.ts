import type { Config } from 'tailwindcss'

/** 刻度=主题令牌桥接（03 篇 §2.5 + board.css 实测 2026-09-28）；rounded-* 重定义对齐全站圆角三档。
 *  令牌唯一事实源 = design-system/tokens/tokens.css（与 docs/设计稿/assets/board.css 同名同源）；
 *  这里只做 语义色/圆角/阴影/字阶 →Tailwind 类的桥接，禁止出现裸色值。
 *  圆角三档：--r-ctl:10px 控件 / --r-panel:14px 面板 / --r-card:18px 卡片（radius 不分亮暗，px 回退=令牌值）。
 *  阴影 --sh-card/--sh-float 亮暗成对（tokens.css :root 与 .dark 各定义一份），var() 引用即自动跟随主题。
 *  字阶只增不改：既有 text-xs/sm/base 语义保持原值，新增 2xs/13px/15px 补齐画板六阶（10/11/12/13/14/15px）。 */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        accent: 'var(--accent)',
        'accent-soft': 'var(--accent-soft)',
        purple: 'var(--purple)',
        'purple-soft': 'var(--purple-soft)',
        teal: 'var(--teal)',
        'teal-soft': 'var(--teal-soft)',
        green: 'var(--green)',
        orange: 'var(--orange)',
        red: 'var(--red)',
        surface: 'var(--surface)',
        'surface-2': 'var(--surface-2)',
        bg: 'var(--bg)',
        label: 'var(--label)',
        'label-2': 'var(--label-2)',
        'label-3': 'var(--label-3)',
        separator: 'var(--separator)',
      },
      /* 圆角三档桥接：lg=控件档 / xl=面板档 / 2xl=卡片档（原 8/12/16px → 10/14/18px，全站既有类名自动获得主题值）；
         3xl=大容器 24px；full/sm/md 保持 Tailwind 默认。语义别名 ctl/panel/cardlg/card 与令牌同名可直接用。 */
      borderRadius: {
        lg: 'var(--r-ctl, 10px)',
        xl: 'var(--r-panel, 14px)',
        '2xl': 'var(--r-card, 18px)',
        '3xl': '24px',
        ctl: 'var(--r-ctl, 10px)',
        panel: 'var(--r-panel, 14px)',
        cardlg: 'var(--r-card, 18px)',
        card: 'var(--r-card, 18px)',
      },
      /* 阴影两键：card=卡片层、float=浮层层；值取 tokens.css 亮暗成对变量，跟随 .dark 自动切换 */
      boxShadow: {
        card: 'var(--sh-card)',
        float: 'var(--sh-float)',
      },
      /* 字阶只增不改：补齐画板六阶中 Tailwind 缺失的三档（10/13/15px），既有刻度不动避免全站字号漂移 */
      fontSize: {
        '2xs': '10px',
        '13px': '13px',
        '15px': '15px',
      },
      fontFamily: {
        sans: ['var(--font-sans, -apple-system)', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        mono: ['var(--mono, ui-monospace)', 'SFMono-Regular', 'monospace'],
      },
    },
  },
  plugins: [],
} satisfies Config
