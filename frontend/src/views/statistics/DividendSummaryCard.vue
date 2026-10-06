<script setup lang="ts">
import HelpTip from '@/components/HelpTip.vue'
import { NAlert } from 'naive-ui'
import FinancialStatistic from '@/components/FinancialStatistic.vue'
import { formatCurrency, profitColor } from '@/utils/helpers'
import type { DividendSummary } from './types'

defineProps<{ dividendSummary: DividendSummary }>()
</script>

<template>
  <section class="dividend-section" aria-labelledby="dividend-title">
    <h2 id="dividend-title">
      已到账股息统计
      <HelpTip label="查看已到账股息统计口径">
        新记录只计已确认到账的现金股息；历史未核验记录按上方提示沿用旧计算，缺少到账日时沿用旧日期口径。待收估算及核对提示统一见上方累计收益卡片，普通现金分红不冲减买入成本。累计金额按最新汇率折算人民币。
        递延扣税按实际扣款日计入，即使尚未归属到某笔股息也不会漏计。 收益曲线、实收口径的区间损益与
        XIRR 按流水当日汇率折算，与这里的累计折算额可能不同。
      </HelpTip>
    </h2>

    <NAlert
      v-if="dividendSummary.missing_rate_currencies?.length"
      type="warning"
      :closable="false"
      class="methodology-alert"
      :title="`缺少 ${dividendSummary.missing_rate_currencies.join('/')} 汇率，对应股息未计入 CNY 折算总额，请先在汇率页补录`"
    />

    <NAlert
      v-if="dividendSummary.unallocated_tax_count"
      type="warning"
      :closable="false"
      :title="`${dividendSummary.unallocated_tax_count} 笔股息税尚待归属，已计入累计税费和税后收益；可在账户数据的现金事件中分摊`"
    />
    <NAlert
      v-if="dividendSummary.legacy_unreviewed_count"
      type="warning"
      :closable="false"
      :title="`${dividendSummary.legacy_unreviewed_count} 笔历史股息仍沿用旧账本计算，到账依据尚待核对；不代表已逐笔确认。`"
    />
    <NAlert
      v-if="dividendSummary.amounts_incomplete_count"
      type="info"
      :closable="false"
      :title="`${dividendSummary.amounts_incomplete_count} 笔仅净到账额已知，税前总额未知；以下税前及税额为已知部分，未知税额不代表免税，净额已计入实收。`"
    />
    <div class="dividend-metrics">
      <div class="net-dividend">
        <FinancialStatistic
          title="累计实收净股息"
          :value="dividendSummary.total_dividend_net"
          :formatter="formatCurrency"
          :value-style="{ color: profitColor(dividendSummary.total_dividend_net) }"
        />
      </div>
      <div>
        <FinancialStatistic
          :title="dividendSummary.amounts_incomplete_count ? '已知税前金额' : '累计股息（税前）'"
          :value="dividendSummary.total_dividend_gross"
          :formatter="formatCurrency"
        />
      </div>
      <div>
        <FinancialStatistic
          :title="dividendSummary.amounts_incomplete_count ? '已记录股息税' : '累计税费'"
          :value="dividendSummary.total_tax"
          :formatter="formatCurrency"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
.dividend-section {
  margin: 16px 0;
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  padding: 16px;
  background: var(--app-surface);
  font-family: var(--app-font-sans);
}
h2 {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0 0 22px;
  font-size: 20px;
  font-weight: 500;
  font-family: var(--app-font-sans);
}
.n-alert {
  margin-bottom: 12px;
}
.dividend-metrics {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) repeat(2, minmax(0, 1fr));
  gap: 24px;
}
.dividend-metrics :deep(.financial-statistic-label) {
  color: var(--app-text);
}
.net-dividend :deep(.financial-statistic-value) {
  font-size: var(--app-number-cumulative);
}
.dividend-section .stat-note {
  margin: 18px 0 0;
  color: var(--app-text);
  font-size: 13px;
  line-height: 1.8;
}
@media (max-width: 640px) {
  .dividend-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 20px;
  }
  .net-dividend {
    grid-column: 1 / -1;
  }
}
@media (max-width: 360px) {
  .dividend-metrics {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (min-width: 1025px) {
  .dividend-section {
    padding: 16px;
  }
  h2 {
    margin-bottom: 12px;
    font-size: 18px;
  }
  .dividend-metrics {
    gap: 20px;
  }
  .dividend-section .stat-note {
    margin-top: 12px;
  }
}
</style>
