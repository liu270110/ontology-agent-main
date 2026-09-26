import type { Config } from 'tailwindcss'

/** 令牌唯一事实源 = design-system/tokens/tokens.css（03 篇 §2.5）；
 *  这里只做语义色→Tailwind 类的桥接，禁止出现裸色值。 */
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
      borderRadius: { card: 'var(--r-card, 16px)' },
      fontFamily: {
        sans: ['var(--font-sans, -apple-system)', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        mono: ['var(--mono, ui-monospace)', 'SFMono-Regular', 'monospace'],
      },
    },
  },
  plugins: [],
} satisfies Config
