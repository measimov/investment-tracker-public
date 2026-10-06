/** 图表字体与分类索引；所有颜色由 chartTheme 读取已解析的公共 CSS 变量。 */
export const CHART_SYSTEM_FONT_FAMILY =
  "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'SF Pro Display', 'Segoe UI', Roboto, 'Helvetica Neue', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', Arial, sans-serif"

export const CHART_FONT_FAMILY = `'Inter Variable', 'Noto Sans SC Variable', ${CHART_SYSTEM_FONT_FAMILY}`

/** 分类维度保持原市场及基准顺序；实际颜色由 chartTheme 读取公共 CSS 配色。 */
export const MARKET_CHART_INDICES = [0, 1, 3, 5, 6]
export const BENCHMARK_CHART_INDICES = [3, 5, 6]
