<script setup lang="ts">
import HelpTip from '@/components/HelpTip.vue'
import { NAlert, NTag } from 'naive-ui'
import type { PeriodPnlResponse } from '@/types'
import PeriodReceivablePnl from '@/components/PeriodReceivablePnl.vue'
import { formatCurrency, formatDate, formatPercent, profitColor } from '@/utils/helpers'
import { periodIsEstimated } from '../dashboard/helpers'

defineProps<{ summary: PeriodPnlResponse | null; error: string }>()
const periodKeys = ['mtd', 'ytd'] as const
</script>

<template>
  <section class="period-section" aria-label="本月与本年损益">
    <header class="period-heading">
      <h2>本月与本年损益</h2>
      <HelpTip label="查看月度与年度损益口径"
        >固定月/年期间，不随上方区间选择变化。收益率、当日损益、收益曲线与 XIRR
        仍按实收口径计算。</HelpTip
      >
    </header>
    <NAlert
      v-if="error"
      :title="`期间损益加载失败：${error}`"
      type="warning"
      data-testid="period-pnl-error"
    />
    <template v-if="summary">
      <div class="period-grid" data-testid="statistics-period-pnl">
        <article v-for="key in periodKeys" :key="key" class="period-card">
          <header>
            <strong>{{ summary.periods[key].label }}损益</strong>
            <HelpTip :label="`查看${summary.periods[key].label}损益日期范围`">
              {{ formatDate(summary.periods[key].start_date) }} 至
              {{ formatDate(summary.periods[key].end_date) }}
            </HelpTip>
          </header>
          <div class="period-label">实收股息口径</div>
          <strong
            class="period-value"
            :style="{ color: profitColor(summary.periods[key].pnl_cny) }"
            :data-testid="`period-pnl-${key}`"
          >
            {{
              summary.periods[key].status === 'unavailable'
                ? '无法计算'
                : formatCurrency(summary.periods[key].pnl_cny)
            }}
          </strong>
          <NTag
            v-if="periodIsEstimated(summary.periods[key])"
            type="warning"
            size="small"
            :bordered="false"
          >
            估算
          </NTag>
          <div class="period-label">
            收益率（实收口径）{{ formatPercent(summary.periods[key].return_rate) }}
          </div>
          <PeriodReceivablePnl
            compact
            v-if="summary.periods[key].receivable_pnl"
            :summary="summary.periods[key].receivable_pnl!"
            :period-key="key"
          />
        </article>
      </div>
      <NAlert
        v-for="warning in summary.data_quality.warnings"
        :key="warning"
        :title="warning"
        type="warning"
        class="period-warning"
      />
    </template>
  </section>
</template>

<style scoped>
.period-section {
  padding: 16px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  margin: 16px 0;
  font-family: var(--app-font-sans);
}
.period-heading {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-bottom: 16px;
}
.period-heading h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 500;
  font-family: var(--app-font-sans);
}
.period-heading p {
  margin: 0;
  font-size: 13px;
  color: var(--app-text);
  line-height: 1.7;
}
.period-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
}
.period-card > header {
  display: flex;
  align-items: center;
  gap: 4px;
}
.period-card {
  min-width: 0;
}
.period-warning {
  margin-bottom: 16px;
}
.period-dates {
  display: block;
  margin-top: 4px;
  color: var(--app-text);
  font-size: 13px;
}
.period-label,
.period-note {
  color: var(--app-text);
  font-size: 13px;
  line-height: 1.7;
}
.period-value {
  display: inline-block;
  margin-right: 8px;
  font-size: var(--app-number-secondary);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.period-note {
  margin: 12px 0 0;
}
.period-card :deep(.receivable-label),
.period-card :deep(.receivable-detail) {
  color: var(--app-text);
  font-size: 13px;
}
.period-card :deep(.receivable-warning) {
  font-size: 13px;
}
@media (max-width: 640px) {
  .period-section {
    padding: 16px;
  }
  .period-grid {
    grid-template-columns: 1fr;
    gap: 24px;
  }
}

@media (min-width: 1025px) {
  .period-section {
    padding: 16px;
    margin: 16px 0;
  }
  .period-grid {
    gap: 20px;
  }
}
</style>
