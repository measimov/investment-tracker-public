<template>
  <div class="statistics-page" v-loading="loading && !initialLoading">
    <template v-if="initialLoading">
      <el-row :gutter="20" class="performance-cards">
        <el-col :span="24">
          <el-card class="stat-card" shadow="never">
            <template #header>
              <el-skeleton animated>
                <template #template>
                  <el-skeleton-item variant="text" class="skeleton-heading" />
                </template>
              </el-skeleton>
            </template>
            <el-row :gutter="20">
              <el-col v-for="index in 3" :key="index" :xs="24" :sm="8">
                <div class="statistic-skeleton-block">
                  <el-skeleton animated>
                    <template #template>
                      <el-skeleton-item variant="text" class="skeleton-label" />
                      <el-skeleton-item variant="h3" class="skeleton-number" />
                    </template>
                  </el-skeleton>
                </div>
              </el-col>
            </el-row>
            <el-divider />
            <el-skeleton animated :rows="5" />
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="20" class="performance-cards">
        <el-col v-for="index in 2" :key="index" :xs="24" :md="12">
          <el-card class="stat-card" shadow="never">
            <el-skeleton animated>
              <template #template>
                <el-skeleton-item variant="text" class="skeleton-heading" />
                <div class="stat-card-skeleton-grid">
                  <el-skeleton-item variant="h3" />
                  <el-skeleton-item variant="h3" />
                </div>
                <el-skeleton-item v-for="row in 4" :key="row" variant="text" />
              </template>
            </el-skeleton>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="20">
        <el-col :xs="24" :md="12">
          <el-card class="stat-card">
            <el-skeleton animated>
              <template #template>
                <el-skeleton-item variant="text" class="skeleton-heading" />
                <el-skeleton-item variant="circle" class="chart-circle-skeleton" />
              </template>
            </el-skeleton>
          </el-card>
        </el-col>
        <el-col :xs="24" :md="12">
          <el-card class="stat-card">
            <el-skeleton animated :rows="8" />
          </el-card>
        </el-col>
      </el-row>
    </template>

    <template v-else>
      <el-alert
        v-for="warning in summaryWarnings"
        :key="warning"
        :title="warning"
        type="error"
        show-icon
        :closable="false"
        class="summary-warning"
      />

      <el-row :gutter="20" class="performance-cards">
        <el-col :span="24">
          <el-card class="stat-card" shadow="hover">
            <template #header>
              <div class="card-header">
                <div class="title-with-tag">
                  <span>证券持仓收益</span>
                  <el-tag type="info" effect="plain" size="small">权益仓口径</el-tag>
                </div>
              </div>
            </template>

            <el-row :gutter="20">
              <el-col :xs="24" :sm="8">
                <el-statistic
                  title="总收益"
                  :value="accountReturn.total_return"
                  :precision="2"
                  prefix="¥"
                  :value-style="{ color: getProfitColor(accountReturn.total_return) }"
                />
              </el-col>
              <el-col :xs="24" :sm="8">
                <el-statistic
                  title="总收益率"
                  :value="accountReturn.total_return_rate"
                  :precision="2"
                  suffix="%"
                  :value-style="{ color: getProfitColor(accountReturn.total_return) }"
                />
              </el-col>
              <el-col :xs="24" :sm="8">
                <el-statistic
                  v-if="accountReturn.annualized_return_rate != null"
                  title="权益仓 XIRR"
                  :value="accountReturn.annualized_return_rate"
                  :precision="2"
                  suffix="%"
                  :value-style="{
                    color: getProfitColor(accountReturn.annualized_return_rate)
                  }"
                />
                <div v-else class="empty-statistic">
                  <span>权益仓 XIRR</span>
                  <strong>—</strong>
                </div>
              </el-col>
            </el-row>

            <el-divider />

            <div class="stat-detail">
              <div class="stat-item">
                <span>净投入本金（权益仓）：</span>
                <span class="value">{{
                  formatCurrency(accountReturn.net_invested_principal_cny)
                }}</span>
              </div>
              <div class="stat-item">
                <span>当前市值：</span>
                <span class="value">{{
                  formatCurrency(accountReturn.current_market_value_cny)
                }}</span>
              </div>
              <div class="stat-item">
                <span>已平仓资本利得：</span>
                <span
                  class="value"
                  :style="{ color: getProfitColor(accountReturn.realized_trading_pnl_cny) }"
                >
                  {{ formatCurrency(accountReturn.realized_trading_pnl_cny) }}
                </span>
              </div>
              <div class="stat-item">
                <span>未实现盈亏：</span>
                <span
                  class="value"
                  :style="{ color: getProfitColor(accountReturn.unrealized_pnl_cny) }"
                >
                  {{ formatCurrency(accountReturn.unrealized_pnl_cny) }}
                </span>
              </div>
              <div class="stat-item">
                <span>税后股息：</span>
                <span class="value" :style="{ color: COLOR.success }">
                  {{ formatCurrency(accountReturn.net_dividend_income_cny) }}
                </span>
              </div>
              <div class="stat-note">
                总收益 = 已平仓资本利得 + 未实现盈亏 +
                税后股息。权益仓口径：仅统计投入证券的资金，账户闲置现金与外部出入金
                不计入、不稀释收益率；收益率及 XIRR 在此口径内精确，不代表全账户表现。
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="20" class="performance-cards">
        <el-col :span="24">
          <el-card class="stat-card" shadow="hover" v-loading="analyticsLoading">
            <template #header>
              <div class="chart-header">
                <div class="title-with-tag">
                  <span>证券组合 TTWR 与风险指标</span>
                  <el-tag type="warning" effect="plain" size="small">实验指标</el-tag>
                </div>
                <el-button
                  type="primary"
                  size="small"
                  :icon="Refresh"
                  :loading="historyRefreshing"
                  @click="refreshHistoryAndAnalytics"
                >
                  同步历史行情
                </el-button>
              </div>
            </template>

            <el-alert
              title="TTWR 与风险指标为实验指标，基于证券交易现金流估算，未包含账户现金和真实外部入出金；胜率/盈亏比按平仓日落在所选区间内的每笔平仓交易统计。"
              type="warning"
              :closable="false"
              show-icon
              class="methodology-alert"
            />

            <div class="range-toolbar">
              <el-radio-group v-model="analyticsRangePreset" size="small">
                <el-radio-button
                  v-for="preset in RANGE_PRESETS"
                  :key="preset.value"
                  :value="preset.value"
                >
                  {{ preset.label }}
                </el-radio-button>
              </el-radio-group>
              <el-date-picker
                v-if="analyticsRangePreset === 'custom'"
                v-model="analyticsCustomRange"
                type="daterange"
                size="small"
                value-format="YYYY-MM-DD"
                range-separator="~"
                start-placeholder="开始日期"
                end-placeholder="结束日期"
                class="range-picker"
              />
              <span v-if="effectiveRangeLabel" class="range-label">
                {{ effectiveRangeLabel }}
              </span>
              <el-select
                v-model="selectedBenchmarks"
                multiple
                collapse-tags
                :multiple-limit="3"
                size="small"
                placeholder="对比基准"
                class="benchmark-select"
                data-testid="benchmark-select"
              >
                <el-option
                  v-for="option in benchmarkOptions"
                  :key="option.code"
                  :label="option.name"
                  :value="option.code"
                />
                <template #empty>
                  <p class="benchmark-empty">
                    基准目录加载失败：后端版本过旧或请求失败，请重启后端后刷新页面
                  </p>
                </template>
              </el-select>
              <el-tag
                v-for="block in unavailableBenchmarks"
                :key="block.code"
                type="info"
                size="small"
                effect="plain"
              >
                {{ block.name }} 无基准数据
              </el-tag>
              <el-tooltip
                v-for="block in partialBenchmarks"
                :key="`partial-${block.code}`"
                content="基准数据晚于区间起点，计量区间不一致，不计算超额收益；可同步更早历史行情补齐"
              >
                <el-tag type="warning" size="small" effect="plain">
                  {{ block.name }} 起点晚于区间
                </el-tag>
              </el-tooltip>
            </div>

            <div class="analytics-summary-grid">
              <div class="analytics-metric">
                <span class="metric-label">年化收益率（TTWR估算）</span>
                <span
                  class="metric-value"
                  :style="{ color: getProfitColor(analyticsMetrics.annualized_return_rate || 0) }"
                >
                  {{ formatNullablePercent(analyticsMetrics.annualized_return_rate) }}
                </span>
              </div>
              <div class="analytics-metric">
                <span class="metric-label">夏普率</span>
                <span class="metric-value">{{
                  formatNullableNumber(analyticsMetrics.sharpe_ratio)
                }}</span>
              </div>
              <div class="analytics-metric">
                <span class="metric-label">索提诺率</span>
                <span class="metric-value">{{
                  formatNullableNumber(analyticsMetrics.sortino_ratio)
                }}</span>
              </div>
              <div class="analytics-metric">
                <span class="metric-label">最大回撤</span>
                <span class="metric-value danger">
                  {{ formatNullablePercent(analyticsMetrics.max_drawdown_rate) }}
                </span>
              </div>
              <div class="analytics-metric">
                <span class="metric-label">卡玛率</span>
                <span class="metric-value">{{
                  formatNullableNumber(analyticsMetrics.calmar_ratio)
                }}</span>
              </div>
              <div v-if="primaryBenchmarkComparison" class="analytics-metric">
                <span class="metric-label">
                  区间超额收益（vs {{ primaryBenchmarkComparison.name }}）
                  <el-tooltip
                    content="组合 TTWR 与基准累计收益的算术差（百分点）；基准为价格指数、不含股息、原币口径"
                  >
                    <el-icon class="label-help"><QuestionFilled /></el-icon>
                  </el-tooltip>
                </span>
                <span
                  class="metric-value"
                  :style="{ color: getProfitColor(primaryBenchmarkComparison.excess_return_rate) }"
                >
                  {{ formatNullablePercent(primaryBenchmarkComparison.excess_return_rate) }}
                </span>
              </div>
              <div class="analytics-metric">
                <span class="metric-label">标的胜率（实验）</span>
                <span class="metric-value">{{ formatNullablePercent(tradeSkill.win_rate) }}</span>
              </div>
              <div class="analytics-metric">
                <span class="metric-label">盈亏比</span>
                <span class="metric-value">{{
                  formatNullableNumber(tradeSkill.payoff_ratio)
                }}</span>
              </div>
              <div class="analytics-metric">
                <span class="metric-label">Profit Factor</span>
                <span class="metric-value">{{
                  formatNullableNumber(tradeSkill.profit_factor)
                }}</span>
              </div>
              <div class="analytics-metric">
                <span class="metric-label">区间已实现盈亏</span>
                <span
                  class="metric-value"
                  :style="{ color: getProfitColor(rangeSummary.realized_pnl_cny || 0) }"
                >
                  {{ formatCurrency(rangeSummary.realized_pnl_cny || 0) }}
                </span>
              </div>
              <div class="analytics-metric">
                <span class="metric-label">区间税后股息</span>
                <span class="metric-value">
                  {{ formatCurrency(rangeSummary.dividend_net_cny || 0) }}
                </span>
              </div>
              <div class="analytics-metric">
                <span class="metric-label">区间年化（XIRR·资金加权）</span>
                <span
                  class="metric-value"
                  :style="{ color: getProfitColor(rangeSummary.xirr_annualized_rate || 0) }"
                >
                  {{ formatNullablePercent(rangeSummary.xirr_annualized_rate) }}
                </span>
              </div>
            </div>

            <div v-if="analyticsWarnings.length" class="analytics-warnings">
              <el-tag
                v-for="warning in analyticsWarnings"
                :key="warning"
                type="warning"
                effect="plain"
              >
                {{ warning }}
              </el-tag>
            </div>

            <div v-if="historySyncJob" class="job-progress">
              <div class="job-progress-header">
                <span>{{ historySyncStatusText }}</span>
                <span>
                  {{ historySyncJob.completed || 0 }}/{{ historySyncJob.total || 0 }}
                  <template v-if="historySyncJob.current_symbol">
                    · {{ historySyncJob.current_symbol }} {{ historySyncJob.current_market }}
                  </template>
                </span>
              </div>
              <el-progress
                :percentage="historySyncPercent"
                :status="historySyncProgressStatus"
                :stroke-width="10"
              />
              <div class="job-progress-detail">
                成功 {{ historySyncJob.success_count || 0 }} · 跳过
                {{ historySyncJob.skipped_count || 0 }} · 失败
                {{ historySyncJob.failed_count || 0 }}
              </div>
              <div v-if="historySyncJob.error" class="job-progress-error">
                {{ historySyncJob.error }}
              </div>
            </div>

            <el-empty
              v-if="performanceCurve.length === 0"
              description="暂无收益率曲线数据"
              :image-size="88"
            />
            <v-chart
              v-else
              :option="analyticsChartOption"
              class="chart chart-performance"
              autoresize
            />
          </el-card>
        </el-col>
      </el-row>

      <!-- 新增：投资表现卡片 -->
      <el-row :gutter="20" class="performance-cards">
        <!-- 卡片1：当前持仓表现 -->
        <el-col :xs="24" :md="12">
          <el-card class="stat-card" shadow="hover">
            <template #header>
              <div class="card-header">
                <span>当前持仓表现（FIFO分析口径）</span>
                <div>
                  <el-button
                    type="success"
                    size="small"
                    :icon="Refresh"
                    @click="refreshPricesAndCalculate"
                    :loading="refreshing"
                    class="refresh-button"
                  >
                    一键刷新股价
                  </el-button>
                  <el-button type="primary" size="small" @click="showPriceDialog = true">
                    输入价格
                  </el-button>
                </div>
              </div>
            </template>

            <el-row :gutter="20">
              <el-col :xs="24" :sm="12">
                <el-statistic
                  title="未实现盈亏"
                  :value="currentPerformance.unrealized_pnl"
                  :precision="2"
                  prefix="¥"
                  :value-style="{ color: getProfitColor(currentPerformance.unrealized_pnl) }"
                />
              </el-col>
              <el-col :xs="24" :sm="12">
                <el-statistic
                  title="浮盈率"
                  :value="currentPerformance.unrealized_pnl_rate"
                  :precision="2"
                  suffix="%"
                  :value-style="{ color: getProfitColor(currentPerformance.unrealized_pnl) }"
                />
              </el-col>
            </el-row>

            <el-divider />

            <div class="stat-detail">
              <div class="stat-item">
                <span>当前持仓成本（FIFO）：</span>
                <span class="value">
                  {{
                    formatCurrency(
                      currentPerformance.current_holdings_cost ||
                        summaryStats.total_invested_cny ||
                        0
                    )
                  }}
                </span>
              </div>
              <div class="stat-item">
                <span>当前市值：</span>
                <span class="value">
                  {{ formatCurrency(currentPerformance.current_market_value) }}
                  <el-text v-if="!currentPerformance.current_market_value" type="info" size="small">
                    (需输入价格计算)
                  </el-text>
                </span>
              </div>
            </div>
          </el-card>
        </el-col>

        <!-- 卡片2：历史交易能力 -->
        <el-col :xs="24" :md="12">
          <el-card class="stat-card" shadow="hover">
            <template #header>
              <div class="card-header">
                <span>已平仓交易能力 (FIFO)</span>
              </div>
            </template>

            <el-row :gutter="20">
              <el-col :xs="24" :sm="12">
                <el-statistic
                  title="已平仓盈亏"
                  :value="realizedPnL.realized_pnl"
                  :precision="2"
                  prefix="¥"
                  :value-style="{ color: getProfitColor(realizedPnL.realized_pnl) }"
                />
              </el-col>
              <el-col :xs="24" :sm="12">
                <el-statistic
                  title="已平仓收益率"
                  :value="realizedPnL.realized_pnl_rate"
                  :precision="2"
                  suffix="%"
                  :value-style="{ color: getProfitColor(realizedPnL.realized_pnl) }"
                />
              </el-col>
            </el-row>

            <el-divider />

            <div class="stat-detail">
              <div class="stat-item">
                <span>含股息已实现收益：</span>
                <span
                  class="value"
                  :style="{ color: getProfitColor(totalRealizedReturn.total_realized_return) }"
                >
                  {{ formatCurrency(totalRealizedReturn.total_realized_return) }}
                </span>
              </div>
              <div class="stat-item">
                <span>含股息已实现收益率：</span>
                <span
                  class="value"
                  :style="{ color: getProfitColor(totalRealizedReturn.total_realized_return) }"
                >
                  {{ formatPercent(totalRealizedReturn.total_realized_return_rate) }}
                </span>
              </div>
              <div class="stat-item">
                <span>已卖出FIFO成本：</span>
                <span class="value">{{ formatCurrency(realizedPnL.sold_cost) }}</span>
              </div>
              <div class="stat-item">
                <span>资本利得：</span>
                <span class="value" :style="{ color: getProfitColor(realizedPnL.realized_pnl) }">
                  {{ formatCurrency(realizedPnL.realized_pnl) }}
                </span>
              </div>
              <div class="stat-item">
                <span>税后股息：</span>
                <span class="value" :style="{ color: COLOR.success }">
                  {{ formatCurrency(totalRealizedReturn.net_dividend_income_cny) }}
                </span>
              </div>
              <div class="stat-note">
                已平仓收益率只评价已卖出的交易；分母为被卖出部分的 FIFO 成本。
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 股息统计（独立卡片） -->
      <el-row :gutter="20" class="section-gap-bottom">
        <el-col :span="24">
          <el-card class="stat-card" shadow="hover">
            <template #header>
              <div class="card-header">
                <span>股息收入统计</span>
              </div>
            </template>

            <el-alert
              v-if="dividendSummary.missing_rate_currencies?.length"
              type="warning"
              :closable="false"
              show-icon
              class="methodology-alert"
              :title="`缺少 ${dividendSummary.missing_rate_currencies.join('/')} 汇率，对应股息未计入 CNY 折算总额，请先在汇率页补录`"
            />

            <el-row :gutter="20">
              <el-col :xs="24" :sm="8">
                <el-statistic
                  title="累计股息（税前）"
                  :value="dividendSummary.total_dividend_gross"
                  :precision="2"
                  prefix="¥"
                />
              </el-col>
              <el-col :xs="24" :sm="8">
                <el-statistic
                  title="累计税费"
                  :value="dividendSummary.total_tax"
                  :precision="2"
                  prefix="¥"
                />
              </el-col>
              <el-col :xs="24" :sm="8">
                <el-statistic
                  title="累计股息（税后）"
                  :value="dividendSummary.total_dividend_net"
                  :precision="2"
                  prefix="¥"
                  :value-style="{ color: COLOR.success }"
                />
              </el-col>
            </el-row>
          </el-card>
        </el-col>
      </el-row>

      <!-- 原有的图表和表格 -->
      <el-row :gutter="20">
        <!-- Market Distribution -->
        <el-col :xs="24" :md="12">
          <el-card class="stat-card">
            <template #header>
              <span>市场分布统计</span>
            </template>
            <el-empty
              v-if="marketStats.length === 0"
              description="暂无市场分布数据"
              :image-size="88"
            />
            <v-chart v-else :option="marketChartOption" class="chart" autoresize />
          </el-card>
        </el-col>

        <!-- Market Table -->
        <el-col :xs="24" :md="12">
          <el-card class="stat-card">
            <template #header>
              <span>市场详细数据</span>
            </template>
            <div class="responsive-table">
              <el-table :data="marketStats" stripe>
                <template #empty>
                  <el-empty description="暂无市场统计数据" :image-size="88" />
                </template>
                <el-table-column prop="market" label="市场" min-width="100" />
                <el-table-column
                  prop="holdings_count"
                  label="持仓数"
                  min-width="90"
                  align="right"
                />
                <el-table-column prop="total_cost" label="总成本" min-width="130" align="right">
                  <template #default="{ row }">
                    <span style="font-weight: bold">
                      {{ formatCurrency(row.total_cost) }}
                    </span>
                  </template>
                </el-table-column>
                <el-table-column label="占比" min-width="90" align="right">
                  <template #default="{ row }">
                    {{ formatNumber((row.total_cost / totalInvested) * 100, 2) }}%
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- Transaction Timeline -->
      <el-row :gutter="20" class="section-gap">
        <el-col :span="24">
          <el-card class="stat-card">
            <template #header>
              <div class="chart-header">
                <span>交易时间趋势</span>
                <el-radio-group v-model="timeGroupBy" @change="loadTimeStats">
                  <el-radio-button value="month">按月</el-radio-button>
                  <el-radio-button value="year">按年</el-radio-button>
                </el-radio-group>
              </div>
            </template>
            <el-empty
              v-if="timeStats.length === 0"
              description="暂无交易时间趋势"
              :image-size="88"
            />
            <v-chart v-else :option="timeChartOption" class="chart" autoresize />
          </el-card>
        </el-col>
      </el-row>

      <!-- Holdings Ranking -->
      <el-row :gutter="20" class="section-gap">
        <el-col :span="24">
          <el-card class="stat-card">
            <template #header>
              <span>持仓排行</span>
            </template>
            <div class="responsive-table">
              <el-table :data="profitLossData" stripe>
                <template #empty>
                  <el-empty description="暂无持仓排行数据" :image-size="88" />
                </template>
                <el-table-column type="index" label="排名" width="80" />
                <el-table-column prop="symbol" label="代码" min-width="100" />
                <el-table-column prop="name" label="名称" min-width="130" show-overflow-tooltip />
                <el-table-column prop="market" label="市场" width="80" />
                <el-table-column prop="quantity" label="数量" min-width="110" align="right">
                  <template #default="{ row }">
                    {{ formatNumber(row.quantity, 4) }}
                  </template>
                </el-table-column>
                <el-table-column prop="avg_cost" label="成本价" min-width="105" align="right">
                  <template #default="{ row }">
                    {{ formatNumber(row.avg_cost, 4) }}
                  </template>
                </el-table-column>
                <el-table-column prop="total_cost" label="总成本" min-width="130" align="right">
                  <template #default="{ row }">
                    <span :style="{ fontWeight: 'bold', color: COLOR.primary }">
                      {{ formatCurrency(row.total_cost, row.currency) }}
                    </span>
                    <div
                      v-if="row.currency !== 'CNY'"
                      style="font-size: 12px; color: var(--app-text-soft); margin-top: 2px"
                    >
                      ≈ {{ formatCurrency(convertToCNY(row.total_cost, row.currency)) }}
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="占比" width="100" align="right">
                  <template #default="{ row }">
                    {{
                      formatNumber(
                        (convertToCNY(row.total_cost, row.currency) / totalInvestedCNY) * 100,
                        2
                      )
                    }}%
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 价格输入对话框 -->
      <el-dialog v-model="showPriceDialog" title="输入当前价格" width="720px">
        <div class="responsive-table">
          <el-table :data="priceInputData" max-height="560" stripe v-loading="loading">
            <el-table-column prop="symbol" label="代码" width="100" />
            <el-table-column prop="name" label="名称" min-width="130" show-overflow-tooltip />
            <el-table-column prop="market" label="市场" width="100" />
            <el-table-column label="持仓数量" width="120" align="right">
              <template #default="{ row }">
                {{ formatNumber(row.quantity, 2) }}
              </template>
            </el-table-column>
            <el-table-column label="平均成本" width="120" align="right">
              <template #default="{ row }">
                {{ formatNumber(row.avg_cost, 4) }}
              </template>
            </el-table-column>
            <el-table-column label="当前价格" width="180">
              <template #default="{ row }">
                <el-input-number v-model="row.current_price" :min="0" :precision="4" size="small" />
              </template>
            </el-table-column>
          </el-table>
        </div>

        <template #footer>
          <div class="mobile-dialog-footer price-dialog-footer">
            <el-button @click="showPriceDialog = false">取消</el-button>
            <el-button @click="savePrices" :loading="saving">保存价格</el-button>
            <el-button type="primary" @click="calculatePerformance" :loading="loading"
              >计算</el-button
            >
          </div>
        </template>
      </el-dialog>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, type Ref } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { PieChart, BarChart, LineChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  DataZoomComponent
} from 'echarts/components'
import VChart from 'vue-echarts'
import { ElMessage } from 'element-plus'
import { QuestionFilled, Refresh } from '@element-plus/icons-vue'
import { presetRangeParams } from '@/utils/dateRange'
import { CHART_FONT_FAMILY, CHART_PALETTE, COLOR, chartTooltipCurrency } from '@/styles/tokens'
import api from '../api'
import { useHoldingsStore } from '../stores/holdings'
import { useExchangeRates } from '../composables/useExchangeRates'
import { getApiErrorMessage } from '../utils/apiErrors'
import {
  formatNumber,
  formatCurrency,
  formatPercent,
  profitColor as getProfitColor,
  toNumber
} from '../utils/helpers'
import { pollJobUntilDone, type BackgroundJob } from '../utils/polling'

interface MarketStat {
  market: string
  total_cost: number
  missing_rate_currencies?: string[]
  [key: string]: unknown
}

interface TimeStat {
  period: string
  buy_amount: number
  sell_amount: number
  [key: string]: unknown
}

interface ProfitLossItem {
  total_cost: number
  currency: string
  [key: string]: unknown
}

interface SummaryStats {
  total_invested_cny?: number
  missing_rate_currencies?: string[]
  [key: string]: unknown
}

interface CurrentPerformance {
  unrealized_pnl: number
  current_holdings_cost: number
  unrealized_pnl_rate: number
  current_market_value: number
  holdings_detail: Array<Record<string, unknown>>
  missing_rate_currencies?: string[]
  [key: string]: unknown
}

interface RealizedPnL {
  realized_pnl: number
  sold_cost: number
  realized_pnl_rate: number
  trades_detail: Array<Record<string, unknown>>
  data_quality?: { warnings?: string[] }
  [key: string]: unknown
}

interface DividendSummary {
  total_dividend_gross: number
  total_tax: number
  total_dividend_net: number
  by_symbol: Array<Record<string, unknown>>
  missing_rate_currencies?: string[]
  [key: string]: unknown
}

interface TotalRealizedReturn {
  realized_trading_pnl_cny: number
  net_dividend_income_cny: number
  total_realized_return: number
  total_realized_return_rate: number
  sold_cost_cny: number
  [key: string]: unknown
}

interface AccountReturn {
  total_return: number
  total_return_rate: number
  annualized_return_rate: number | null
  net_invested_principal_cny: number
  current_market_value_cny: number
  realized_trading_pnl_cny: number
  unrealized_pnl_cny: number
  net_dividend_income_cny: number
  [key: string]: unknown
}

interface CurvePoint {
  date: string
  cumulative_return_rate?: number | string | null
  drawdown_rate?: number | string | null
  [key: string]: unknown
}

interface AnalyticsMetrics {
  annualized_return_rate?: number | null
  max_drawdown_rate?: number | null
  sharpe_ratio?: number | null
  sortino_ratio?: number | null
  calmar_ratio?: number | null
  [key: string]: unknown
}

interface TradeSkill {
  win_rate?: number | null
  payoff_ratio?: number | null
  profit_factor?: number | null
  [key: string]: unknown
}

interface RangeSummary {
  realized_pnl_cny?: number | null
  dividend_net_cny?: number | null
  xirr_annualized_rate?: number | null
  [key: string]: unknown
}

interface BenchmarkComparison {
  benchmark_total_return_rate?: number | null
  excess_return_rate?: number | null
  benchmark_max_drawdown_rate?: number | null
  [key: string]: unknown
}

interface BenchmarkBlock {
  code: string
  name: string
  status: string
  alignment?: string
  points?: CurvePoint[]
  total_return_rate?: number | null
  comparison?: BenchmarkComparison | null
  [key: string]: unknown
}

interface PerformanceAnalytics {
  calculation_level: string
  curve: CurvePoint[]
  benchmarks?: BenchmarkBlock[]
  metrics: AnalyticsMetrics
  trade_skill: TradeSkill
  range_summary?: RangeSummary
  date_range?: { start_date?: string; end_date?: string; clamped?: boolean } | null
  data_quality?: { warnings?: string[] }
  [key: string]: unknown
}

interface HistorySyncJob {
  id?: number
  status?: string
  progress_percent?: number | string | null
  completed?: number
  total?: number
  current_symbol?: string | null
  current_market?: string | null
  success_count?: number
  skipped_count?: number
  failed_count?: number
  error?: string | null
  [key: string]: unknown
}

interface PriceInputRow {
  symbol: string
  name?: string | null
  market: string
  avg_cost: number
  current_price: number | null
  quantity: number
}

interface PriceRefreshResult {
  success_count: number
  skipped_count: number
  failed_count: number
  failed_list?: Array<{ symbol: string; market: string; error?: string }>
  [key: string]: unknown
}

use([
  CanvasRenderer,
  PieChart,
  BarChart,
  LineChart,
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  DataZoomComponent
])

const marketStats = ref<MarketStat[]>([])
const holdingsStore = useHoldingsStore()
const timeStats = ref<TimeStat[]>([])
const profitLossData = ref<ProfitLossItem[]>([])
const timeGroupBy = ref('month')
const summaryStats = ref<SummaryStats>({})

// 新增：性能统计数据
const currentPerformance = ref<CurrentPerformance>({
  unrealized_pnl: 0,
  current_holdings_cost: 0,
  unrealized_pnl_rate: 0,
  current_market_value: 0,
  holdings_detail: []
})

const realizedPnL = ref<RealizedPnL>({
  realized_pnl: 0,
  sold_cost: 0,
  realized_pnl_rate: 0,
  trades_detail: [],
  data_quality: {
    warnings: []
  }
})

const dividendSummary = ref<DividendSummary>({
  total_dividend_gross: 0,
  total_tax: 0,
  total_dividend_net: 0,
  by_symbol: []
})

const totalRealizedReturn = ref<TotalRealizedReturn>({
  realized_trading_pnl_cny: 0,
  net_dividend_income_cny: 0,
  total_realized_return: 0,
  total_realized_return_rate: 0,
  sold_cost_cny: 0
})

const accountReturn = ref<AccountReturn>({
  total_return: 0,
  total_return_rate: 0,
  annualized_return_rate: null,
  net_invested_principal_cny: 0,
  current_market_value_cny: 0,
  realized_trading_pnl_cny: 0,
  unrealized_pnl_cny: 0,
  net_dividend_income_cny: 0
})

const performanceAnalytics = ref<PerformanceAnalytics>({
  calculation_level: 'empty',
  curve: [],
  metrics: {},
  trade_skill: {},
  data_quality: {
    warnings: []
  }
})

const showPriceDialog = ref(false)
const priceInputData = ref<PriceInputRow[]>([])
const loading = ref(false)
const initialLoading = ref(true)
const analyticsLoading = ref(false)
const saving = ref(false)
const refreshing = ref(false)
const historyRefreshing = ref(false)
const historySyncJob = ref<HistorySyncJob | null>(null)
const { loadExchangeRates, convertToCNY } = useExchangeRates()
let isUnmounted = false

const totalInvested = computed(() => {
  return marketStats.value.reduce((sum, item) => sum + item.total_cost, 0)
})

const totalInvestedCNY = computed(() => {
  return profitLossData.value.reduce((sum, item) => {
    return sum + convertToCNY(item.total_cost, item.currency)
  }, 0)
})

const performanceCurve = computed(() => performanceAnalytics.value.curve || [])
const analyticsMetrics = computed(() => performanceAnalytics.value.metrics || {})
const tradeSkill = computed(() => performanceAnalytics.value.trade_skill || {})
const rangeSummary = computed(() => performanceAnalytics.value.range_summary || {})

// 基准对比：选择持久化 localStorage；无数据基准降级为标签提示
const BENCHMARK_STORAGE_KEY = 'statistics.benchmarks'
const benchmarkOptions = ref<{ code: string; name: string; currency: string }[]>([])
const selectedBenchmarks = ref<string[]>(
  (() => {
    try {
      const stored = JSON.parse(window.localStorage.getItem(BENCHMARK_STORAGE_KEY) || 'null')
      if (Array.isArray(stored)) return stored.filter((code) => typeof code === 'string')
    } catch {
      // 损坏的本地存储按默认值处理
    }
    return ['000300.SH']
  })()
)
// 基准虚线用调色板后段，避开组合主线（primary）与回撤（danger）用色
const BENCHMARK_COLORS = [CHART_PALETTE[3], CHART_PALETTE[5], CHART_PALETTE[6]]

const analyticsBenchmarks = computed(() => performanceAnalytics.value.benchmarks || [])
const okBenchmarks = computed(() => analyticsBenchmarks.value.filter((b) => b.status === 'ok'))
const unavailableBenchmarks = computed(() =>
  analyticsBenchmarks.value.filter((b) => b.status !== 'ok')
)
// 基准数据晚于区间起点（first_available）：曲线仍画（头部空洞可见），
// 但不产出超额收益——tag 说明降级原因
const partialBenchmarks = computed(() =>
  analyticsBenchmarks.value.filter((b) => b.status === 'ok' && b.alignment === 'first_available')
)
const primaryBenchmarkComparison = computed(() => {
  const first = okBenchmarks.value.find((b) => b.comparison)
  if (!first?.comparison) return null
  return { name: first.name, excess_return_rate: first.comparison.excess_return_rate ?? null }
})

// 区间选择：预设即业界主交互，自定义次之。切换只做便宜的重算（不触发行情同步）。
const RANGE_PRESETS = [
  { value: 'all', label: '成立以来' },
  { value: '1m', label: '近1月' },
  { value: '3m', label: '近3月' },
  { value: '6m', label: '近6月' },
  { value: '1y', label: '近1年' },
  { value: 'ytd', label: '今年以来' },
  { value: 'custom', label: '自定义' }
]
const analyticsRangePreset = ref('all')
const analyticsCustomRange = ref<[string, string] | null>(null)

function analyticsRangeParams() {
  const preset = analyticsRangePreset.value
  if (preset === 'all') return {}
  if (preset === 'custom') {
    if (!analyticsCustomRange.value || analyticsCustomRange.value.length !== 2) return {}
    return { start_date: analyticsCustomRange.value[0], end_date: analyticsCustomRange.value[1] }
  }
  return presetRangeParams(preset) || {}
}

const effectiveRangeLabel = computed(() => {
  const range = performanceAnalytics.value.date_range
  if (!range) return ''
  let label = `区间 ${range.start_date} ~ ${range.end_date}`
  if (range.clamped) label += '（已按有效数据区间调整）'
  return label
})

watch([analyticsRangePreset, analyticsCustomRange], () => {
  if (
    analyticsRangePreset.value === 'custom' &&
    (!analyticsCustomRange.value || analyticsCustomRange.value.length !== 2)
  ) {
    return // 等待自定义区间选完再重算
  }
  loadPerformanceAnalytics()
})

watch(selectedBenchmarks, (codes) => {
  try {
    window.localStorage.setItem(BENCHMARK_STORAGE_KEY, JSON.stringify(codes))
  } catch {
    // 存储失败不阻断（隐私模式等）
  }
  loadPerformanceAnalytics()
})
// 缺汇率的币种：后端在这些块里各自剔除了无法折算的金额（不再按原值当 CNY 混入），
// 但只有 realized 一块带 data_quality.warnings。其余三块只给 missing_rate_currencies，
// 不在这里合成提示的话，用户只会看到「累计投入变成 0」而不知道原因。
const missingRateCurrencies = computed(() => {
  const collected = new Set<string>()
  const sources = [
    summaryStats.value?.missing_rate_currencies,
    dividendSummary.value?.missing_rate_currencies,
    currentPerformance.value?.missing_rate_currencies,
    ...(marketStats.value || []).map((row) => row?.missing_rate_currencies)
  ]
  for (const list of sources) {
    for (const currency of list || []) collected.add(currency)
  }
  return Array.from(collected).sort()
})

const summaryWarnings = computed(() => {
  const warnings = [...(realizedPnL.value.data_quality?.warnings || [])]
  const currencies = missingRateCurrencies.value
  // realized 那条已含币种名，避免同一批币种重复提示两遍
  const alreadyMentioned = warnings.some((text) =>
    currencies.every((currency) => text.includes(currency))
  )
  if (currencies.length && !alreadyMentioned) {
    warnings.push(
      `缺少 ${currencies.join('/')} 对 CNY 的汇率，这些币种的金额未计入 CNY 汇总（不会按原值混入）。` +
        '请在「汇率管理」补录后重新查看。'
    )
  }
  return warnings
})
const analyticsWarnings = computed(() => performanceAnalytics.value.data_quality?.warnings || [])
const historySyncPercent = computed(() => {
  return Math.max(0, Math.min(100, Math.round(Number(historySyncJob.value?.progress_percent || 0))))
})
const historySyncProgressStatus = computed(() => {
  if (historySyncJob.value?.status === 'failed' || historySyncJob.value?.status === 'interrupted')
    return 'exception'
  if (historySyncJob.value?.status === 'succeeded') return 'success'
  return undefined
})
const historySyncStatusText = computed(() => {
  const status = historySyncJob.value?.status
  if (status === 'queued') return '历史行情同步排队中'
  if (status === 'running') return '历史行情同步中'
  if (status === 'succeeded') return '历史行情同步完成'
  if (status === 'failed') return '历史行情同步失败'
  if (status === 'interrupted') return '历史行情同步已中断'
  return '历史行情同步'
})

const marketChartOption = computed(() => ({
  color: CHART_PALETTE,
  textStyle: { fontFamily: CHART_FONT_FAMILY },
  tooltip: {
    trigger: 'item',
    formatter: (params: { name: string; value: number; percent: number }) =>
      `${params.name}: ${chartTooltipCurrency(params.value)} (${params.percent}%)`
  },
  legend: { bottom: 0, left: 'center' },
  series: [
    {
      type: 'pie',
      radius: ['42%', '68%'],
      center: ['50%', '44%'],
      avoidLabelOverlap: false,
      itemStyle: {
        borderRadius: 10,
        borderColor: '#fff',
        borderWidth: 2
      },
      label: {
        show: true,
        formatter: '{b}: {d}%'
      },
      emphasis: {
        label: {
          show: true,
          fontSize: 16,
          fontWeight: 'bold'
        }
      },
      data: marketStats.value.map((item) => ({
        name: item.market,
        value: item.total_cost
      }))
    }
  ]
}))

const timeChartOption = computed(() => {
  const periods = timeStats.value.map((item) => item.period)
  const buyAmounts = timeStats.value.map((item) => item.buy_amount)
  const sellAmounts = timeStats.value.map((item) => item.sell_amount)

  return {
    textStyle: { fontFamily: CHART_FONT_FAMILY },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'shadow'
      },
      valueFormatter: (value: number | string) => chartTooltipCurrency(value)
    },
    legend: {
      data: ['买入金额', '卖出金额']
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: periods
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: '¥{value}'
      }
    },
    series: [
      {
        name: '买入金额',
        type: 'bar',
        data: buyAmounts,
        itemStyle: {
          color: COLOR.success
        }
      },
      {
        name: '卖出金额',
        type: 'bar',
        data: sellAmounts,
        itemStyle: {
          color: COLOR.danger
        }
      }
    ]
  }
})

const analyticsChartOption = computed(() => {
  const dates = performanceCurve.value.map((item) => item.date)
  const returns = performanceCurve.value.map((item) => Number(item.cumulative_return_rate || 0))
  const drawdowns = performanceCurve.value.map((item) => Number(item.drawdown_rate || 0))

  // 基准虚线：与组合曲线同栅格生成，按日期对齐（first_available 时头部为空洞）
  const benchmarkSeries = okBenchmarks.value.map((block, index) => {
    const rateByDate = new Map(
      (block.points || []).map((point) => [point.date, Number(point.cumulative_return_rate || 0)])
    )
    return {
      name: block.name,
      type: 'line',
      smooth: true,
      symbol: 'none',
      data: dates.map((day) => rateByDate.get(day) ?? null),
      lineStyle: {
        width: 2,
        type: 'dashed',
        color: BENCHMARK_COLORS[index % BENCHMARK_COLORS.length]
      },
      itemStyle: {
        color: BENCHMARK_COLORS[index % BENCHMARK_COLORS.length]
      }
    }
  })

  return {
    textStyle: { fontFamily: CHART_FONT_FAMILY },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value: number | string) => `${formatNumber(value, 2)}%`
    },
    legend: {
      data: ['累计TTWR收益率', '回撤', ...benchmarkSeries.map((series) => series.name)]
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '12%',
      containLabel: true
    },
    dataZoom: [
      {
        type: 'inside'
      },
      {
        type: 'slider',
        height: 22,
        bottom: 8
      }
    ],
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: dates
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: '{value}%'
      }
    },
    series: [
      {
        name: '累计TTWR收益率',
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 5,
        data: returns,
        lineStyle: {
          width: 3,
          color: COLOR.primary
        },
        itemStyle: {
          color: COLOR.primary
        }
      },
      {
        name: '回撤',
        type: 'line',
        smooth: true,
        symbol: 'none',
        data: drawdowns,
        areaStyle: {
          color: 'rgba(225, 29, 72, 0.12)'
        },
        lineStyle: {
          width: 2,
          color: COLOR.danger
        },
        itemStyle: {
          color: COLOR.danger
        }
      },
      ...benchmarkSeries
    ]
  }
})

// 统计块加载工厂；silent=true 时失败只记 console（摘要卡片允许静默降级）
function makeStatsLoader<T>(
  target: Ref<T>,
  fetcher: () => Promise<{ data: T }>,
  failureMessage: string,
  { silent = false }: { silent?: boolean } = {}
) {
  return async () => {
    try {
      const response = await fetcher()
      target.value = response.data
    } catch (error) {
      if (silent) console.error(failureMessage, error)
      else ElMessage.error(failureMessage)
    }
  }
}

const loadMarketStats = makeStatsLoader(
  marketStats,
  () => api.getStatsByMarket(),
  '加载市场统计失败'
)
const loadTimeStats = makeStatsLoader(
  timeStats,
  () => api.getStatsByTime(timeGroupBy.value),
  '加载时间统计失败'
)
const loadProfitLoss = makeStatsLoader(
  profitLossData,
  () => api.getHoldingsCostBreakdown(),
  '加载持仓成本分布失败'
)
const loadSummaryStats = makeStatsLoader(summaryStats, () => api.getSummary(), '加载统计摘要失败', {
  silent: true
})

async function loadAllData() {
  initialLoading.value = true
  try {
    const supportingDataPromise = Promise.all([
      loadMarketStats(),
      loadTimeStats(),
      loadProfitLoss(),
      loadSummaryStats(),
      loadExchangeRates(),
      loadBenchmarkCatalog()
    ])

    // Server-side pricing (issue #46) lets these run concurrently: the two
    // heavy endpoints no longer wait for the holdings price payload.
    await Promise.all([
      loadHoldingsForPrice(),
      loadPerformanceSummary(),
      loadPerformanceAnalytics()
    ])
    initialLoading.value = false
    await supportingDataPromise
  } finally {
    initialLoading.value = false
  }
}

function getCurrentPrices() {
  const prices: Record<string, number> = {}
  priceInputData.value.forEach((item) => {
    if (item.current_price && item.current_price > 0) {
      prices[item.symbol] = item.current_price
    }
  })
  return prices
}

function applyPerformanceSummary(data: {
  current_performance?: CurrentPerformance
  realized_pnl?: RealizedPnL
  dividend_summary?: DividendSummary
  total_realized_return?: TotalRealizedReturn
  account_return?: AccountReturn
}) {
  currentPerformance.value = data.current_performance || currentPerformance.value
  realizedPnL.value = data.realized_pnl || realizedPnL.value
  dividendSummary.value = data.dividend_summary || dividendSummary.value
  totalRealizedReturn.value = data.total_realized_return || totalRealizedReturn.value
  accountReturn.value = data.account_return || accountReturn.value
}

async function loadBenchmarkCatalog() {
  try {
    const response = await api.getBenchmarkCatalog()
    benchmarkOptions.value = response.data
  } catch (error) {
    console.warn('加载基准目录失败', error) // 选择器降级为空，不阻断主流程
  }
}

async function loadPerformanceSummary(prices: Record<string, number> | null = null) {
  // null -> server-side authoritative prices (GET); a prices object is the
  // manual what-if path from the price dialog (POST).
  const response = await api.getPerformanceSummary(prices)
  applyPerformanceSummary(response.data)
}

// 快速切换区间时的请求竞态防护：只有最后一次发起的请求可以写入数据、
// 清除 loading 或弹错误——较慢的旧响应直接丢弃。
let analyticsRequestSeq = 0

async function loadPerformanceAnalytics(
  options: { prices?: Record<string, number> | null; refresh_history?: boolean } = {}
) {
  const seq = ++analyticsRequestSeq
  analyticsLoading.value = options.refresh_history !== true
  try {
    const response = await api.getPerformanceAnalytics(options.prices || null, {
      refresh_history: options.refresh_history === true,
      risk_free_rate: 0,
      benchmarks: selectedBenchmarks.value.join(','),
      ...analyticsRangeParams()
    })
    if (seq !== analyticsRequestSeq) return
    performanceAnalytics.value = response.data
  } catch (error) {
    if (seq !== analyticsRequestSeq) return
    ElMessage.error('加载收益率曲线失败：' + getApiErrorMessage(error))
  } finally {
    if (seq === analyticsRequestSeq) {
      analyticsLoading.value = false
    }
  }
}

async function refreshHistoryAndAnalytics() {
  historyRefreshing.value = true
  try {
    const response = await api.startPerformanceHistorySync()
    historySyncJob.value = response.data
    const completedJob = await pollPerformanceHistorySyncJob(response.data.id)
    if (!completedJob) return

    await loadPerformanceAnalytics()

    if (completedJob.status === 'succeeded') {
      ElMessage.success(
        `历史行情同步完成：成功${completedJob.success_count || 0}项，跳过${
          completedJob.skipped_count || 0
        }项`
      )
    } else {
      ElMessage.warning(
        `历史行情同步完成但有失败：成功${completedJob.success_count || 0}项，失败${
          completedJob.failed_count || 0
        }项`
      )
    }
  } catch (error) {
    ElMessage.error('历史行情同步失败：' + getApiErrorMessage(error))
  } finally {
    historyRefreshing.value = false
  }
}

async function pollPerformanceHistorySyncJob(jobId: number | string) {
  return pollJobUntilDone(() => api.getPerformanceHistorySyncJob(jobId), {
    maxAttempts: 1800,
    isCancelled: () => isUnmounted,
    onUpdate: (job: BackgroundJob) => {
      historySyncJob.value = job as HistorySyncJob
    },
    timeoutMessage: '历史行情同步仍在后台运行，请稍后刷新统计页查看',
    failureMessage: '历史行情同步失败'
  })
}

// 新增：性能统计方法
async function loadHoldingsForPrice(options: { force?: boolean } = {}) {
  try {
    const holdings = await holdingsStore.fetchHoldings(
      {},
      {
        force: options?.force === true
      }
    )
    priceInputData.value = holdings.map((h) => ({
      symbol: h.symbol,
      name: h.name,
      market: h.market,
      avg_cost: toNumber(h.avg_cost),
      // Use the database price if available; missing prices should stay empty.
      current_price:
        h.current_price && toNumber(h.current_price) > 0 ? toNumber(h.current_price) : null,
      quantity: toNumber(h.quantity)
    }))
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '加载持仓失败'))
  }
}

async function calculatePerformance(showSuccess = true) {
  loading.value = true
  try {
    // Dialog what-if: value using the (possibly unsaved) dialog prices.
    const prices = getCurrentPrices()
    await loadPerformanceSummary(prices)
    await loadPerformanceAnalytics({ prices })
    showPriceDialog.value = false
    if (showSuccess) {
      ElMessage.success('计算完成')
    }
  } catch (error) {
    ElMessage.error('计算失败：' + getApiErrorMessage(error))
  } finally {
    loading.value = false
  }
}

async function savePrices() {
  saving.value = true
  try {
    // Build updates array with symbol, market, and price
    const updates: Array<{ symbol: string; market: string; price: number }> = []
    priceInputData.value.forEach((item) => {
      if (item.current_price && item.current_price > 0) {
        updates.push({
          symbol: item.symbol,
          market: item.market,
          price: item.current_price
        })
      }
    })

    if (updates.length === 0) {
      ElMessage.warning('没有可保存的价格')
      return
    }

    const response = await holdingsStore.batchUpdatePrices(updates)
    const result = response.data
    ElMessage.success(`成功保存 ${result.success_count} 个价格`)
    await loadHoldingsForPrice({ force: true })

    if (result.failed_count > 0) {
      console.error('保存失败的项:', result.failed_list)
    }
  } catch (error) {
    ElMessage.error('保存失败：' + getApiErrorMessage(error))
  } finally {
    saving.value = false
  }
}

async function refreshPricesAndCalculate() {
  refreshing.value = true
  try {
    // Step 1: Refresh prices from API
    const refreshResponse = await holdingsStore.refreshAllPrices()
    const refreshJob = refreshResponse.data
    const refreshResult = await pollPriceRefreshJob(refreshJob.id)
    if (!refreshResult) return

    // Step 2: Reload holdings with updated prices
    await loadHoldingsForPrice({ force: true })

    // Step 3: Auto-calculate performance
    await calculatePerformance(false)

    // Show success message
    const successMsg = `股价已更新 (成功${refreshResult.success_count}只`
    const failedMsg = refreshResult.failed_count > 0 ? `, 失败${refreshResult.failed_count}只` : ''
    const skippedMsg =
      refreshResult.skipped_count > 0 ? `, 跳过${refreshResult.skipped_count}只` : ''
    ElMessage.success(successMsg + failedMsg + skippedMsg + ') 并完成计算')

    if (refreshResult.failed_list && refreshResult.failed_list.length > 0) {
      console.error('刷新失败的股票:', refreshResult.failed_list)
    }
  } catch (error) {
    if (!isUnmounted) {
      ElMessage.error('刷新失败：' + getApiErrorMessage(error))
    }
  } finally {
    if (!isUnmounted) {
      refreshing.value = false
    }
  }
}

async function pollPriceRefreshJob(jobId: number | string): Promise<PriceRefreshResult | null> {
  const job = await pollJobUntilDone(() => api.getPriceRefreshJob(jobId), {
    isCancelled: () => isUnmounted,
    timeoutMessage: '刷新仍在后台运行，请稍后重新查看持仓价格',
    failureMessage: '后台刷新失败'
  })
  return (job?.result ?? null) as PriceRefreshResult | null
}

function formatNullableNumber(value: number | string | null | undefined, precision = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '--'
  return formatNumber(Number(value), precision)
}

function formatNullablePercent(value: number | string | null | undefined, precision = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '--'
  return `${formatNumber(Number(value), precision)}%`
}

onMounted(() => {
  isUnmounted = false
  loadAllData()
})

onUnmounted(() => {
  isUnmounted = true
})
</script>

<style scoped>
.statistics-page {
  width: 100%;
}

.performance-cards {
  margin-bottom: 20px;
}

.stat-card {
  height: 100%;
}

.skeleton-heading {
  width: 160px;
  height: 18px;
}

.statistic-skeleton-block {
  min-height: 86px;
  padding: 8px 0;
}

.skeleton-label {
  width: 90px;
  height: 14px;
  margin-bottom: 12px;
}

.skeleton-number {
  width: 70%;
  height: 28px;
}

.stat-card-skeleton-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin: 24px 0;
}

.chart-circle-skeleton {
  display: block;
  width: 180px;
  height: 180px;
  margin: 32px auto 16px;
}

.card-header > div,
.chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.title-with-tag {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.empty-statistic {
  display: flex;
  min-height: 65px;
  flex-direction: column;
  gap: 8px;
}

.empty-statistic span {
  color: var(--app-text-muted);
  font-size: 13px;
}

.empty-statistic strong {
  color: var(--app-text);
  font-size: 24px;
  line-height: 1.2;
}

.range-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}

.range-label {
  color: var(--app-text-soft);
  font-size: 12px;
}

.range-picker {
  max-width: 260px;
}

.benchmark-select {
  min-width: 180px;
  max-width: 260px;
}

.benchmark-empty {
  padding: 10px 12px;
  margin: 0;
  font-size: 12px;
  color: var(--app-text-soft);
}

.label-help {
  margin-left: 4px;
  color: var(--app-text-soft);
  cursor: help;
  vertical-align: -2px;
}

.methodology-alert {
  margin-bottom: 16px;
}

.refresh-button {
  margin-right: 8px;
}

.chart {
  width: 100%;
  height: 400px;
}

.chart-performance {
  height: 420px;
}

.analytics-summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.analytics-metric {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 72px;
  padding: 14px;
  background:
    linear-gradient(150deg, var(--app-primary-soft), transparent 60%), var(--app-surface-muted);
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-radius-inner);
  transition:
    transform 0.18s ease,
    box-shadow 0.18s ease;
}

.analytics-metric:hover {
  transform: translateY(-2px);
  box-shadow: var(--app-shadow-sm);
}

.metric-label {
  color: var(--app-text-muted);
  font-size: 13px;
  font-weight: 500;
}

.metric-value {
  color: var(--app-text);
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.metric-value.danger {
  color: var(--app-danger);
}

.analytics-warnings {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.summary-warning {
  margin-bottom: 16px;
}

.stat-detail {
  margin-top: 16px;
  padding: 4px 0;
}

.stat-item {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 8px 0;
  font-size: 14px;
  color: var(--app-text-muted);
  border-bottom: 1px solid var(--app-separator);
}

.stat-item:last-of-type {
  border-bottom: 0;
}

.stat-item .value {
  color: var(--app-text);
  font-weight: 600;
  text-align: right;
}

.stat-item .value.success {
  color: var(--app-success);
}

.stat-item .value.danger {
  color: var(--app-danger);
}

.stat-note {
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.5;
  padding: 10px 12px;
  margin-top: 8px;
  background: var(--app-surface-secondary);
  border-radius: var(--app-radius-sm);
}

:deep(.el-statistic__head) {
  color: var(--app-text-muted);
  font-size: 13px;
}

:deep(.el-statistic__content) {
  font-weight: 600;
  letter-spacing: -0.022em;
}

@media (max-width: 900px) {
  .card-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .card-header > div,
  .chart-header {
    align-items: stretch;
    flex-direction: column;
    width: 100%;
  }

  .refresh-button {
    margin-right: 0;
  }

  .card-header .el-button {
    width: 100%;
    margin-left: 0;
  }

  .chart {
    height: 300px;
  }

  .chart-performance {
    height: 340px;
  }

  .analytics-summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .metric-value {
    font-size: 18px;
  }

  .stat-item {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .stat-item .value {
    max-width: 100%;
    text-align: left;
    overflow-wrap: anywhere;
  }

  .price-dialog-footer {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .analytics-summary-grid {
    grid-template-columns: 1fr;
  }
}
</style>
