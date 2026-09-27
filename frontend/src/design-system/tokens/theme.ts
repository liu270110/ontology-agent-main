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

/** ECharts 分类色板：序 1~6 = palette 六色稳定序（既有图表不变色），7~8 补 red / label-3 中性色；全部取自 tokens.css，亮暗成对 */
const echartsPalette: Record<ThemeMode, readonly string[]> = {
  light: ["#0071e3", "#30b0c7", "#af52de", "#34c759", "#ff9500", "#5856d6", "#ff3b30", "#aeaeb2"],
  dark: ["#0a84ff", "#40c8e0", "#bf5af2", "#30d158", "#ff9f0a", "#7d7aff", "#ff453a", "#636366"],
};

/** registerTheme 容器类型：键开放（放宽容器，不搬运 ECharts 全量 schema 进本文件） */
export type EChartsThemeConfig = Record<string, unknown>;

const ECHARTS_FONT = "-apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif";

/* 完整主题由构建器生成：轴/tooltip/图例/系列默认全部引用 palette 令牌，禁止出现体系外色值 */
const buildEChartsTheme = (mode: ThemeMode): EChartsThemeConfig => {
  const t = token(mode);
  const dark = mode === "dark";
  const axisLine = dark ? "rgba(255,255,255,0.22)" : "rgba(0,0,0,0.18)"; // = --edge
  const splitLine = dark ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.08)"; // = --separator
  const axisDefaults = {
    axisLine: { lineStyle: { color: axisLine } },
    axisTick: { lineStyle: { color: axisLine } },
    axisLabel: { color: t.label3, fontSize: 11 }, // 坐标轴字色 = label-3 层级
    splitLine: { lineStyle: { color: splitLine } },
    nameTextStyle: { color: t.label3 },
  };
  return {
    backgroundColor: "transparent",
    color: [...echartsPalette[mode]],
    textStyle: { fontFamily: ECHARTS_FONT, color: t.label2 },
    title: {
      textStyle: { color: t.label, fontSize: 14, fontWeight: 600 },
      subtextStyle: { color: t.label3, fontSize: 12 },
    },
    legend: {
      textStyle: { color: t.label2, fontSize: 12 },
      inactiveColor: t.label3,
      itemWidth: 14,
      itemHeight: 8,
      icon: "roundRect",
    },
    tooltip: {
      backgroundColor: dark ? "#2c2c2e" : "rgba(255,255,255,0.72)", // 暗=surface-2；亮=--material 玻璃白
      borderWidth: 0,
      borderColor: "transparent",
      padding: [8, 12],
      textStyle: { color: t.label, fontSize: 12 },
      extraCssText: `border:none;border-radius:10px;box-shadow:${
        dark
          ? "0 2px 8px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35)"
          : "0 2px 8px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.06)"
      }${dark ? "" : ";-webkit-backdrop-filter:blur(20px) saturate(180%);backdrop-filter:blur(20px) saturate(180%)"};`,
    },
    axisPointer: {
      lineStyle: { color: t.label3 },
      crossStyle: { color: t.label3 },
      label: { color: t.label2 },
    },
    categoryAxis: { ...axisDefaults },
    valueAxis: { ...axisDefaults },
    logAxis: { ...axisDefaults },
    timeAxis: { ...axisDefaults },
    /* 系列级默认：柱圆角 / 折线 2px 平滑 / 饼分块圆角 + 面底色描边 */
    line: { smooth: true, symbol: "circle", symbolSize: 4, lineStyle: { width: 2 } },
    bar: { itemStyle: { borderRadius: [4, 4, 0, 0] }, barMaxWidth: 28 },
    pie: { itemStyle: { borderRadius: 6, borderColor: t.surface, borderWidth: 2 } },
    /* 兼容旧便捷键（1.x 形状）：axis/grid 非注册键，ECharts 忽略，仅供调用方取色 */
    axis: { line: axisLine, label: t.label2 },
    grid: { dark: splitLine },
  };
};

/** ECharts Apple 主题（可直接 echarts.registerTheme("oa", echartsTheme)）——图表不引入第二套配色，出处 03 篇 §2 令牌 */
export const echartsTheme: EChartsThemeConfig = buildEChartsTheme("light");

/** 暗色版（切换主题时 registerTheme("oa-dark", ...) 或 setOption 覆盖） */
export const echartsThemeDark: EChartsThemeConfig = buildEChartsTheme("dark");
