/**
 * 设计令牌 TS 映射（三件套之一）——图表 / React Flow / ECharts 主题取色用。
 * 唯一事实源仍是 tokens.css；本文件为派生常量，改令牌先改 tokens.css 再同步此处。
 * 亮暗取值成对，运行时按 document.documentElement.classList.contains('dark') 选择。
 */

export const palette = {
  light: {
    bg: "#f5f5f7", surface: "#ffffff", surface2: "#fbfbfd",
    label: "#1d1d1f", label2: "#6e6e73", label3: "#aeaeb2",
    separator: "rgba(0,0,0,0.08)",
    accent: "#0071e3", onAccent: "#ffffff",
    green: "#34c759", red: "#ff3b30", orange: "#ff9500",
    purple: "#af52de", teal: "#30b0c7", indigo: "#5856d6",
    edge: "rgba(0,0,0,0.18)",
  },
  dark: {
    bg: "#000000", surface: "#1c1c1e", surface2: "#2c2c2e",
    label: "#f5f5f7", label2: "#a1a1a6", label3: "#636366",
    separator: "rgba(255,255,255,0.12)",
    accent: "#0a84ff", onAccent: "#ffffff",
    green: "#30d158", red: "#ff453a", orange: "#ff9f0a",
    purple: "#bf5af2", teal: "#40c8e0", indigo: "#7d7aff",
    edge: "rgba(255,255,255,0.22)",
  },
} as const;

/** 轨迹/审计/通知三处共用的来源着色（24 篇 §4.5） */
export const sourceColor = {
  system: "indigo", user: "accent", tool: "teal",
  subagent: "purple", context: "orange",
} as const;

export const radius = { card: 18, panel: 14, control: 10, pill: 999 } as const;
export const duration = { d1: 150, d2: 250, d3: 400, d4: 700 } as const;
export const ease = { apple: "cubic-bezier(0.25, 0.1, 0.25, 1)", spring: "cubic-bezier(0.34, 1.3, 0.64, 1)" } as const;

export type ThemeMode = "light" | "dark";
export const token = (mode: ThemeMode) => palette[mode];

/** 图谱节点分类色板（03 篇 §2.2，紫/靛/青为功能分类色，非氛围色） */
export const graphCategoryColor = (mode: ThemeMode) => ({
  标准: token(mode).accent, 对象: token(mode).purple, 特性: token(mode).teal,
  约束: token(mode).orange, 行动: token(mode).green, 机构: token(mode).indigo,
});
