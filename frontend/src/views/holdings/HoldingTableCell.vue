<script setup lang="ts">
import { NBadge, NPopover, NTag, NTooltip } from 'naive-ui'
import {
  formatNumber,
  formatCurrency,
  formatDate,
  formatPercent,
  formatPrice,
  formatQuantity
} from '@/utils/helpers'
import type { HoldingRow, HoldingsTableFeature } from './useHoldingsTable'
import type { SecurityBadgesFeature } from './useSecurityBadges'
import { industryTooltip } from './display'
import AnalysisBadges from './AnalysisBadges.vue'
import PriceEditor from './PriceEditor.vue'
import PriceFlags from './PriceFlags.vue'
import type { OpinionTagStyle } from '../opinions/opinionTags'

defineProps<{
  table: HoldingsTableFeature
  badges: SecurityBadgesFeature
  row: HoldingRow
  column: 'security' | 'quantity' | 'price' | 'value' | 'profit' | 'analysis'
}>()
function tagType(type: string) {
  if (type === 'danger') return 'error'
  if (type === 'success' || type === 'warning' || type === 'primary') return type
  return 'default'
}
function opinionTagColor(style: OpinionTagStyle) {
  if (style.effect !== 'dark') return undefined
  const color =
    style.type === 'primary'
      ? 'var(--app-primary)'
      : style.type === 'warning'
        ? 'var(--app-warning-text)'
        : 'var(--app-text-muted)'
  return { color, textColor: 'var(--app-on-primary)', borderColor: color }
}
</script>

<template>
  <div v-if="column === 'security'" class="security-cell">
    <router-link
      :to="`/securities/${encodeURIComponent(row.market)}/${encodeURIComponent(row.symbol)}`"
      class="holding-name"
      :aria-label="`查看 ${row.name || row.symbol} 的标的档案`"
      >{{ row.name || row.symbol }}</router-link
    >
    <div class="cell-sub">
      {{ row.symbol }} · {{ row.market }}
      <template v-if="badges.industryFor(row)">
        ·
        <span
          tabindex="0"
          :title="industryTooltip(badges.industryFor(row)!.source)"
          data-testid="holding-industry"
          >{{ badges.industryFor(row)!.industry }}</span
        ></template
      >
    </div>
    <div
      class="cell-sub"
      :class="{
        'account-unassigned': row.accounts.length === 1 && !row.accounts[0]?.broker_account_id
      }"
    >
      {{
        row.accounts.length === 1
          ? table.accountLabel(row.accounts[0]?.broker_account_id)
          : `${row.accounts.length} 个账户`
      }}
    </div>
    <div v-if="badges.upcomingEvent(row) || badges.announcementBadge(row)" class="cell-badges">
      <NPopover v-if="badges.upcomingEvent(row)" trigger="click">
        <template #trigger
          ><button type="button" class="badge-button" :aria-label="badges.eventTooltip(row)">
            <NTag type="warning" size="small" data-testid="security-event-badge"
              >{{ badges.upcomingEvent(row)!.label }}·{{
                badges.upcomingEvent(row)!.daysText
              }}</NTag
            >
          </button></template
        >
        {{ badges.eventTooltip(row) }}
      </NPopover>
      <NPopover v-if="badges.announcementBadge(row)" trigger="click">
        <template #trigger
          ><button type="button" class="badge-button" aria-label="查看近期重要公告">
            <NTag type="error" size="small" data-testid="announcement-badge">{{
              badges.announcementBadge(row)!.text
            }}</NTag>
          </button></template
        >
        <div v-for="line in badges.announcementBadge(row)!.lines" :key="line">{{ line }}</div>
      </NPopover>
    </div>
  </div>
  <div v-else-if="column === 'quantity'" class="number-cell">
    <div class="cell-main num">{{ formatQuantity(row.quantity) }}</div>
    <div class="cell-sub num">均价 {{ formatPrice(row.avgCost) }} {{ row.currency }}</div>
    <NPopover v-if="row.unknownCost > 0" trigger="click">
      <template #trigger
        ><button type="button" class="badge-button">
          <NTag type="warning" size="small">成本未知 {{ formatQuantity(row.unknownCost) }}</NTag>
        </button></template
      >
      这部分来自成本未知的期初建仓或转托管转入，平均成本与已实现盈亏为估计值；可在公司行动页补录成本。
    </NPopover>
  </div>
  <div v-else-if="column === 'price'" class="number-cell">
    <div class="price-line">
      <PriceEditor :table="table" :row="row" />
      <span class="price-currency">{{ row.currency }}</span>
      <PriceFlags v-if="!table.isEditingPrice(row)" :info="table.priceInfoOf(row)" />
    </div>
    <NPopover v-if="table.priceInfoOf(row)" trigger="click">
      <template #trigger>
        <button
          type="button"
          class="source-button cell-sub"
          :aria-label="`查看 ${row.symbol} 行情来源`"
          data-testid="price-date"
        >
          {{ table.priceInfoOf(row)?.label || '行情来源' }}
        </button>
      </template>
      <div v-for="line in table.priceInfoOf(row)?.tooltip || []" :key="line">{{ line }}</div>
      <p>修改现价将同步至该标的全部账户，持仓成本不变。</p>
    </NPopover>
  </div>
  <div v-else-if="column === 'value'" class="number-cell">
    <template v-if="table.marketValueOf(row) !== null">
      <div class="cell-main num strong">
        {{ formatCurrency(table.marketValueOf(row), row.currency) }}
      </div>
      <div class="cell-sub num">
        <template v-if="row.currency !== 'CNY'"
          >折 {{ formatCurrency(table.marketValueCNYOf(row)) }} · </template
        >{{ table.weightOf(row) === null ? '—' : `${formatNumber(table.weightOf(row), 1)}%` }}
      </div> </template
    ><span v-else class="muted">—</span>
  </div>
  <div v-else-if="column === 'profit'" class="number-cell">
    <template v-if="table.profitOf(row) !== null">
      <div class="cell-main num strong" :style="{ color: table.getProfitColor(row) }">
        {{ formatCurrency(table.profitOf(row), row.currency) }}
      </div>
      <div class="cell-sub num" :style="{ color: table.getProfitColor(row) }">
        <template v-if="row.currency !== 'CNY'"
          >折 {{ formatCurrency(table.profitCNYOf(row)) }} · </template
        >{{ formatPercent(table.profitRateOf(row)) }}
      </div> </template
    ><span v-else class="muted">—</span>
  </div>
  <div v-else class="analysis-cell">
    <NTooltip v-if="badges.analysisFor(row)" placement="top">
      <template #trigger
        ><span tabindex="0" class="ai-tags" data-testid="ai-tags"
          ><AnalysisBadges :badges="badges" :row="row" with-tag /></span
      ></template>
      <div class="ai-tooltip">{{ badges.analysisFor(row)!.summary }}</div>
      <div v-if="badges.analysisFor(row)!.created_at">
        分析于 {{ formatDate(badges.analysisFor(row)!.created_at) }}
      </div>
      <div v-if="badges.analysisOutdated(row)">
        之后有新的财报摘要/报表（{{
          formatDate(badges.analysisFor(row)!.latest_data_at)
        }}），可重新分析
      </div>
    </NTooltip>
    <NTooltip v-if="badges.opinionBadgeTags(row).length">
      <template #trigger
        ><span tabindex="0" class="ai-tags" data-testid="opinion-tags"
          ><NTag
            v-for="tag in badges.opinionBadgeTags(row)"
            :key="tag"
            size="small"
            :type="tagType(badges.opinionTagStyle(tag).type)"
            :color="opinionTagColor(badges.opinionTagStyle(tag))"
            >{{ badges.opinionTagStyle(tag).label }}</NTag
          ><NBadge
            v-if="badges.opinionFor(row)?.new_utterance_count"
            :value="badges.opinionFor(row)!.new_utterance_count!"
            :max="99"
            type="warning" /></span
      ></template>
      {{ badges.opinionFor(row)?.summary || '雪球观点变化' }}
    </NTooltip>
    <span v-if="!badges.analysisFor(row)" class="muted">未分析</span>
  </div>
</template>

<style scoped>
.holding-name {
  color: var(--app-text);
  text-decoration: none;
  font-size: 14px;
  font-weight: 500;
  line-height: 1.6;
}
.holding-name:hover {
  color: var(--app-primary);
  text-decoration: underline;
}
.cell-sub {
  font-size: 12px;
  line-height: 1.6;
  color: var(--app-text-soft);
  margin-top: 3px;
}
.cell-main {
  font-size: 14px;
  line-height: 1.5;
}
.cell-badges,
.ai-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}
.number-cell {
  text-align: right;
}
.num {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.strong {
  font-weight: 500;
}
.muted {
  color: var(--app-text-soft);
  font-size: 12px;
}
.account-unassigned {
  color: var(--app-warning-text);
}
.price-line {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
  justify-content: flex-end;
}
.price-currency {
  font-size: 12px;
  color: var(--app-text-soft);
}
.badge-button {
  font: inherit;
  border: 0;
  background: transparent;
  padding: 0;
  cursor: pointer;
  max-width: 100%;
}
.source-button {
  font: inherit;
  font-size: 12px;
  padding: 0;
  background: none;
  border: 0;
  color: var(--app-text-soft);
  cursor: pointer;
  text-decoration: underline dotted;
  text-underline-offset: 3px;
}
.ai-tooltip {
  max-width: 360px;
  white-space: normal;
}
</style>
