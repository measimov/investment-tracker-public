<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import { useElementBounding, useWindowSize } from '@vueuse/core'
import { NPopover } from 'naive-ui'
import { CircleHelp } from '@lucide/vue'
import type { PeriodReceivablePnl } from '@/types'
import { formatCurrency, profitColor } from '@/utils/helpers'

defineProps<{
  summary: PeriodReceivablePnl
  periodKey: 'mtd' | 'ytd'
  compact?: boolean
}>()
const explanationShow = ref(false)
const explanationId = useId()
const explanationTrigger = ref<HTMLButtonElement | null>(null)
const { left, right, top, bottom } = useElementBounding(explanationTrigger)
const { width, height } = useWindowSize()
const alignEnd = computed(() => right.value > width.value - left.value)
const explanationWidth = computed(() =>
  Math.max(1, Math.min(360, (alignEnd.value ? right.value : width.value - left.value) - 24))
)
</script>

<template>
  <div
    class="period-receivable"
    :class="{ 'period-receivable-compact': compact }"
    :data-testid="`period-receivable-${periodKey}`"
  >
    <template v-if="compact">
      <div class="compact-period-heading">
        <span>{{ summary.is_partial ? '含已知待收（税前·部分）' : '含待收（税前估算）' }}</span>
        <strong
          class="receivable-value"
          :style="{ color: profitColor(summary.estimated_pnl_cny) }"
          :data-testid="`period-estimated-pnl-${periodKey}`"
        >
          {{
            summary.estimated_pnl_cny === null
              ? '无法计算'
              : formatCurrency(summary.estimated_pnl_cny)
          }}
        </strong>
        <NPopover
          v-model:show="explanationShow"
          trigger="click"
          :placement="alignEnd ? 'bottom-end' : 'bottom-start'"
          :width="explanationWidth"
          scrollable
          :style="{ maxHeight: `${Math.max(1, Math.max(top, height - bottom) - 24)}px` }"
        >
          <template #trigger>
            <button
              ref="explanationTrigger"
              type="button"
              class="compact-explanation-trigger"
              :aria-label="`查看${periodKey === 'mtd' ? '本月' : '本年'}待收损益口径`"
              :aria-expanded="explanationShow"
              :aria-controls="explanationShow ? explanationId : undefined"
              @keydown.esc.stop="explanationShow = false"
            >
              <CircleHelp aria-hidden="true" />
            </button>
          </template>
          <div :id="explanationId" class="compact-explanation">
            实收损益 + 期末待收 − 期初待收；两端分别按对应日期汇率折算。
            待收未扣预计税款，到账后冲销待收并计入实际净额，避免跨月重复计算。
            <template v-if="summary.is_partial"
              >仅含可核实部分，历史权益、金额或到账关联尚待核对的记录未计入。</template
            >
          </div>
        </NPopover>
      </div>
      <div class="compact-period-balances">
        <span
          ><span>本期待收变动</span
          ><strong>{{ formatCurrency(summary.receivable_change_cny) }}</strong></span
        >
        <span
          ><span>期初待收</span
          ><strong>{{ formatCurrency(summary.opening_receivable_cny) }}</strong></span
        >
        <span
          ><span>期末待收</span
          ><strong>{{ formatCurrency(summary.closing_receivable_cny) }}</strong></span
        >
      </div>
      <p v-if="summary.is_partial" class="receivable-warning">
        仅含可核实部分。<template v-if="summary.unresolved_count"
          >{{ summary.unresolved_count }} 笔历史权益、金额或到账关联尚待核对。</template
        >
      </p>
      <p v-if="summary.missing_rate_currencies.length" class="receivable-warning">
        缺少 {{ summary.missing_rate_currencies.join('/') }} 对应日期汇率，相关待收未计入。
      </p>
    </template>
    <template v-else>
      <div class="receivable-label">
        {{ summary.is_partial ? '含已知待收股息（税前估算）' : '含待收股息（税前估算）' }}
      </div>
      <strong
        class="receivable-value"
        :style="{ color: profitColor(summary.estimated_pnl_cny) }"
        :data-testid="`period-estimated-pnl-${periodKey}`"
      >
        {{
          summary.estimated_pnl_cny === null
            ? '无法计算'
            : formatCurrency(summary.estimated_pnl_cny)
        }}
      </strong>
      <div class="receivable-detail">
        本期待收变动 {{ formatCurrency(summary.receivable_change_cny) }}
      </div>
      <p v-if="summary.is_partial" class="receivable-warning">
        仅含可核实部分。
        <template v-if="summary.unresolved_count">
          {{ summary.unresolved_count }} 笔历史权益、金额或到账关联尚待核对。
        </template>
      </p>
      <p v-if="summary.missing_rate_currencies.length" class="receivable-warning">
        缺少 {{ summary.missing_rate_currencies.join('/') }} 对应日期汇率，相关待收未计入。
      </p>
      <details class="receivable-detail">
        <summary>查看待收变动</summary>
        <dl>
          <div>
            <dt>期初待收</dt>
            <dd>{{ formatCurrency(summary.opening_receivable_cny) }}</dd>
          </div>
          <div>
            <dt>期末待收</dt>
            <dd>{{ formatCurrency(summary.closing_receivable_cny) }}</dd>
          </div>
        </dl>
        实收损益 + 期末待收 − 期初待收；两端分别按对应日期汇率折算。
        待收未扣预计税款，到账后冲销待收并计入实际净额，避免跨月重复计算。
      </details>
    </template>
  </div>
</template>

<style scoped>
.period-receivable {
  border-top: 1px solid var(--app-border);
  margin-top: 14px;
  padding-top: 12px;
  overflow-wrap: anywhere;
}
.receivable-label,
.receivable-detail,
.receivable-warning {
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.7;
}
.receivable-value {
  display: block;
  font-size: var(--app-number-secondary);
  font-weight: 600;
  line-height: 1.5;
  font-variant-numeric: tabular-nums;
}
.receivable-warning {
  color: var(--app-warning-text);
  margin: 6px 0;
}
summary {
  cursor: pointer;
  min-height: 24px;
  margin-top: 6px;
}
dl {
  margin: 8px 0;
}
dl div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
dd {
  margin: 0;
  font-variant-numeric: tabular-nums;
}

@media (max-width: 640px) {
  summary {
    min-height: 44px;
    line-height: 44px;
  }
}

.period-receivable.period-receivable-compact {
  margin-top: 6px;
  padding-top: 4px;
}
.compact-period-heading,
.compact-period-balances {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0 12px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--app-text-muted);
}
.compact-period-heading {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 2px 8px;
}
.compact-period-heading .receivable-value {
  grid-column: 1 / -1;
  grid-row: 2;
  line-height: 1.4;
}
.compact-period-heading .compact-explanation-trigger {
  grid-column: 2;
  grid-row: 1;
}
.compact-period-balances {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 2px;
}
.compact-period-balances > span {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.compact-period-balances {
  font-variant-numeric: tabular-nums;
}
.compact-period-balances strong {
  color: var(--app-text);
  font-weight: 400;
}
.compact-explanation-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  min-height: 24px;
  border: 0;
  padding: 4px;
  background: transparent;
  color: var(--app-text-muted);
  font: inherit;
  cursor: pointer;
}
.compact-explanation-trigger svg {
  width: 14px;
  height: 14px;
}
.compact-explanation-trigger:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 2px;
}
.period-receivable-compact .receivable-warning {
  font-size: 13px;
  line-height: 1.5;
  margin: 4px 0 0;
}
.compact-explanation {
  font-size: 13px;
  line-height: 1.7;
  color: var(--app-text-muted);
  overflow-wrap: anywhere;
}
@media (max-width: 640px) {
  .compact-explanation-trigger {
    min-height: 44px;
    width: 44px;
    height: 44px;
  }
}
</style>
