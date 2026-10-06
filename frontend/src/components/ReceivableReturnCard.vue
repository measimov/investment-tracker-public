<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import { useElementBounding, useWindowSize } from '@vueuse/core'
import { NPopover } from 'naive-ui'
import { CircleHelp } from '@lucide/vue'
import type { ReceivableReturn } from '@/types'
import { formatCurrency, formatPercent, profitColor } from '@/utils/helpers'

defineProps<{
  summary?: ReceivableReturn | null
  legacyUnreviewedCount?: number
  returnRate?: number | null
  annualizedRate?: number | null
  embedded?: boolean
  compact?: boolean
  hideReviewLink?: boolean
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
  <section
    v-if="summary"
    class="receivable-card"
    :class="{
      'receivable-dashboard': returnRate !== undefined,
      'receivable-embedded': embedded,
      'receivable-compact': compact
    }"
    data-testid="receivable-return"
    aria-label="累计收益双口径"
  >
    <template v-if="compact">
      <div class="compact-return-grid">
        <div>
          <div class="compact-return-label">
            <span>累计收益·实收</span>
            <span class="compact-return-tools">
              <router-link v-if="!hideReviewLink" class="dashboard-link" to="/corporate-actions"
                >核对股息</router-link
              >
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
                    :class="{ 'has-review': summary.pending_overdue_count }"
                    aria-label="查看累计收益口径说明"
                    :aria-expanded="explanationShow"
                    :aria-controls="explanationShow ? explanationId : undefined"
                    @keydown.esc.stop="explanationShow = false"
                  >
                    <CircleHelp aria-hidden="true" />
                  </button>
                </template>
                <div :id="explanationId" class="compact-explanation">
                  <p>实收股息口径的当前累计收益 + 已知待收股息税前估算 = 含待收估算的累计收益。</p>
                  <p
                    v-if="summary.pending_overdue_count"
                    class="compact-return-warning"
                    data-testid="receivable-overdue"
                  >
                    {{ summary.pending_overdue_count }}
                    笔已过预计派息日，仍有待收余额（已计入待收）。
                  </p>
                  <p>
                    待收为已除息、持股权益明确的税前估算，按最新汇率折算，未扣预计税款。收齐后以实际净额替换，不会重复加算，也不改变买入成本。
                  </p>
                  <p>
                    本月、本年另列含待收股息的损益；当日损益、收益曲线和 XIRR
                    仍按实收口径计算。待收是预估收入，不是可用现金。
                  </p>
                  <template v-if="summary.unresolved_count">
                    <p>上方仅合计可确定的待收金额。需要核对的公告不等于尚未收到股息。</p>
                    <p v-if="summary.review_counts.received">
                      已匹配实收的已收金额已计入收益；剩余金额尚不能确定，暂不再加算待收。
                      <span v-if="summary.received_review_reasons.net_amount_only"
                        >{{
                          summary.received_review_reasons.net_amount_only
                        }}
                        笔仅有净到账额；</span
                      >
                      <span v-if="summary.received_review_reasons.currency_mismatch"
                        >{{
                          summary.received_review_reasons.currency_mismatch
                        }}
                        笔公告与到账币种不同；</span
                      >
                      <span v-if="summary.received_review_reasons.payout_currency_unverified"
                        >{{
                          summary.received_review_reasons.payout_currency_unverified
                        }}
                        笔派息币种待核实；</span
                      >
                      <span v-if="summary.received_review_reasons.other"
                        >{{ summary.received_review_reasons.other }} 笔金额或关联待核对。</span
                      >
                    </p>
                    <p v-if="summary.review_counts.possible_receipt">
                      疑似到账记录尚未关联，先核对是否属于同一次派息，避免重复计入。
                    </p>
                    <p v-if="summary.review_counts.entitlement">
                      账户或持股数量待核对，尚不能确定你应收到多少。
                    </p>
                    <p v-if="summary.review_counts.amount">派息金额或币种待核实，暂时无法估算。</p>
                  </template>
                  <p v-if="summary.review_counts.zero_remaining">
                    已匹配实收且计算剩余为零的记录待确认收齐，未再计入待收，也不列为逾期未收。
                  </p>
                  <p v-if="legacyUnreviewedCount">
                    当前收益包含沿用旧计算的历史股息，到账依据尚待核验。
                  </p>
                </div>
              </NPopover>
            </span>
          </div>
          <strong
            class="compact-return-value"
            :style="{ color: profitColor(summary.cash_basis_return_cny) }"
            data-testid="cash-basis-return"
          >
            {{
              summary.cash_basis_return_cny === null
                ? '无法计算'
                : formatCurrency(summary.cash_basis_return_cny)
            }}
          </strong>
          <div v-if="returnRate !== undefined" class="compact-return-detail">
            收益率 <span class="dashboard-number">{{ formatPercent(returnRate) }}</span> ·
            年化（XIRR）
            <span class="dashboard-number">{{ formatPercent(annualizedRate ?? null) }}</span>
          </div>
        </div>
        <div>
          <div class="compact-return-label">
            {{ summary.is_partial ? '累计收益·含已知待收估算（部分）' : '累计收益·含待收估算' }}
          </div>
          <strong
            class="compact-return-value"
            :style="{ color: profitColor(summary.estimated_return_cny) }"
            data-testid="estimated-dividend-return"
          >
            {{
              summary.estimated_return_cny === null
                ? '无法计算'
                : formatCurrency(summary.estimated_return_cny)
            }}
          </strong>
          <div class="compact-return-detail">
            <span
              >{{ summary.is_partial ? '已知待收（税前）' : '待收（税前）' }}
              <strong data-testid="pending-dividend-return"
                ><span class="dashboard-number">{{
                  formatCurrency(summary.known_pending_gross_cny)
                }}</span></strong
              ></span
            >
            <span
              v-for="(amount, currency) in summary.known_pending_gross_by_currency"
              :key="currency"
              ><span class="dashboard-number">{{ formatCurrency(amount, currency) }}</span></span
            >
            <span v-if="!summary.included_count">暂无可计入的已知待收金额</span>
          </div>
        </div>
      </div>
      <div
        v-if="summary.unresolved_count"
        class="compact-return-status"
        data-testid="receivable-unresolved"
      >
        <strong>{{ summary.unresolved_count }} 笔公告需核对，不等于未收股息</strong>
        <span v-if="summary.review_counts.received" data-testid="receivable-received-review"
          >{{ summary.review_counts.received }} 笔已匹配实收，待确认是否收齐</span
        >
        <span v-if="summary.review_counts.possible_receipt"
          >{{ summary.review_counts.possible_receipt }} 笔疑似到账，尚未关联</span
        >
        <span v-if="summary.review_counts.entitlement"
          >{{ summary.review_counts.entitlement }} 笔账户或持股数量待核对</span
        >
        <span v-if="summary.review_counts.amount"
          >{{ summary.review_counts.amount }} 笔派息金额或币种待核实</span
        >
      </div>
      <p v-if="summary.missing_rate_currencies.length" class="compact-return-warning">
        缺少 {{ summary.missing_rate_currencies.join('/') }} 汇率，对应待收未计入人民币估算。
      </p>
      <p
        v-if="summary.review_counts.zero_remaining"
        class="compact-return-status"
        data-testid="receivable-zero-remaining"
      >
        {{ summary.review_counts.zero_remaining }}
        笔已匹配实收且剩余为零，待确认收齐；未再计入待收、不列为逾期未收。
      </p>
      <p v-if="legacyUnreviewedCount" class="compact-return-warning">
        {{ legacyUnreviewedCount }} 笔旧计算历史股息，到账依据待核验。
      </p>
    </template>
    <template v-else>
      <div class="receivable-header">
        <h2 v-if="!embedded">累计收益双口径</h2>
        <router-link to="/corporate-actions">核对股息</router-link>
      </div>

      <div class="receivable-grid">
        <div>
          <div class="metric-label">当前累计收益（实收股息口径）</div>
          <strong
            class="metric-value"
            :style="{ color: profitColor(summary.cash_basis_return_cny) }"
            data-testid="cash-basis-return"
          >
            {{ formatCurrency(summary.cash_basis_return_cny) }}
          </strong>
          <div v-if="returnRate !== undefined" class="metric-detail">
            收益率 {{ formatPercent(returnRate)
            }}<template v-if="annualizedRate != null">
              · 年化（XIRR） {{ formatPercent(annualizedRate) }}</template
            >
          </div>
        </div>
        <div class="estimated-metric">
          <div class="metric-label">
            {{ summary.is_partial ? '累计收益（含已知待收估算）' : '累计收益（含待收估算）' }}
          </div>
          <strong
            class="metric-value"
            :style="{ color: profitColor(summary.estimated_return_cny) }"
            data-testid="estimated-dividend-return"
          >
            {{ formatCurrency(summary.estimated_return_cny) }}
          </strong>
          <div class="metric-detail">当前累计收益 + 下列待收估算</div>
          <div class="pending-composition">
            <div class="metric-label">
              {{ summary.is_partial ? '已知待收股息（税前估算）' : '待收股息（税前估算）' }}
            </div>
            <strong class="metric-value" data-testid="pending-dividend-return">
              {{ formatCurrency(summary.known_pending_gross_cny) }}
            </strong>
            <div class="metric-detail">
              <span
                v-for="(amount, currency) in summary.known_pending_gross_by_currency"
                :key="currency"
              >
                {{ formatCurrency(amount, currency) }}
              </span>
              <span v-if="!summary.included_count">暂无可计入的已知待收金额</span>
            </div>
          </div>
        </div>
      </div>
      <p class="receivable-note">
        上述待收为已除息、持股权益明确的税前估算，按最新汇率折算，未扣预计税款。收齐后以实际净额替换，不会重复加算，也不改变买入成本。
      </p>
      <div
        v-if="summary.unresolved_count"
        class="receivable-review"
        data-testid="receivable-unresolved"
      >
        <strong>{{ summary.unresolved_count }} 笔公告需要核对，不等于尚未收到股息</strong>
        <ul class="review-list">
          <li v-if="summary.review_counts.received" data-testid="receivable-received-review">
            <strong>{{ summary.review_counts.received }} 笔已匹配实收，待确认是否收齐。</strong>
            已收金额已计入收益；剩余金额尚不能确定，暂不再加算待收。
            <span class="review-reasons">
              <span v-if="summary.received_review_reasons.net_amount_only">
                {{ summary.received_review_reasons.net_amount_only }} 笔仅有净到账额；
              </span>
              <span v-if="summary.received_review_reasons.currency_mismatch">
                {{ summary.received_review_reasons.currency_mismatch }} 笔公告与到账币种不同；
              </span>
              <span v-if="summary.received_review_reasons.payout_currency_unverified">
                {{ summary.received_review_reasons.payout_currency_unverified }} 笔派息币种待核实；
              </span>
              <span v-if="summary.received_review_reasons.other">
                {{ summary.received_review_reasons.other }} 笔金额或关联待核对。
              </span>
            </span>
          </li>
          <li v-if="summary.review_counts.possible_receipt">
            <strong
              >{{ summary.review_counts.possible_receipt }} 笔有疑似到账记录，尚未关联。</strong
            >
            先核对是否属于同一次派息，避免重复计入。
          </li>
          <li v-if="summary.review_counts.entitlement">
            <strong>{{ summary.review_counts.entitlement }} 笔账户或持股数量待核对。</strong>
            尚不能确定你应收到多少。
          </li>
          <li v-if="summary.review_counts.amount">
            <strong>{{ summary.review_counts.amount }} 笔派息金额或币种待核实。</strong>
            暂时无法估算。
          </li>
        </ul>
        <p class="review-total">上方仅合计可确定的待收金额。</p>
      </div>
      <p v-if="summary.missing_rate_currencies.length" class="receivable-warning">
        缺少 {{ summary.missing_rate_currencies.join('/') }} 汇率，对应待收金额未计入人民币估算。
      </p>
      <p
        v-if="summary.pending_overdue_count"
        class="receivable-warning"
        data-testid="receivable-overdue"
      >
        已计入上方待收的 {{ summary.pending_overdue_count }}
        笔已过预计派息日，账本仍有待收余额，请核对到账情况。
      </p>
      <p
        v-if="summary.review_counts.zero_remaining"
        class="receivable-note"
        data-testid="receivable-zero-remaining"
      >
        {{ summary.review_counts.zero_remaining }}
        笔已匹配实收且计算剩余为零，待确认收齐；未再计入待收，也不列为逾期未收。
      </p>
      <p v-if="legacyUnreviewedCount" class="receivable-warning">
        当前收益包含 {{ legacyUnreviewedCount }} 笔沿用旧计算的历史股息，到账依据尚待核验。
      </p>
      <details class="receivable-note">
        <summary>查看收益口径说明</summary>
        本月、本年另列含待收股息的损益；当日损益、收益曲线和 XIRR 仍按实收口径计算。
        待收是预估收入，不是可用现金。
      </details>
    </template>
  </section>
</template>

<style scoped>
.receivable-card {
  margin: 28px 0;
  padding: 24px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
}
.receivable-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 22px;
}
.receivable-header h2 {
  font-size: 20px;
  font-weight: 500;
  margin: 0;
}
.receivable-header a {
  color: var(--app-primary-strong);
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  min-height: 24px;
}
.receivable-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
}
.metric-label {
  color: var(--app-text-muted);
  font-size: 13px;
  margin-bottom: 10px;
}
.metric-value {
  font-size: var(--app-number-cumulative);
  font-weight: 600;
  line-height: 1.4;
  overflow-wrap: anywhere;
  font-variant-numeric: tabular-nums;
}
.receivable-dashboard .metric-value {
  font-size: var(--app-number-cumulative);
}
.pending-composition {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--app-border);
}
.pending-composition .metric-value {
  font-size: var(--app-number-secondary);
}
.metric-detail {
  color: var(--app-text-muted);
  font-size: 12px;
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
}
.estimated-metric {
  border-left: 1px solid var(--app-border);
  padding-left: 24px;
}
.receivable-note,
.receivable-warning {
  font-size: 12px;
  line-height: 1.7;
  margin: 12px 0 0;
  color: var(--app-text-muted);
}
.receivable-warning {
  color: var(--app-warning-text);
}
.receivable-review {
  margin-top: 16px;
  padding: 12px 14px;
  border-radius: var(--app-radius-inner);
  background: var(--app-surface-secondary);
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.review-list {
  margin: 8px 0;
  padding-left: 18px;
}
.review-list li + li {
  margin-top: 8px;
}
.review-reasons {
  display: block;
  color: var(--app-text-muted);
}
.review-total {
  margin: 0;
  color: var(--app-text-muted);
}
.receivable-note summary {
  cursor: pointer;
  min-height: 24px;
}
.receivable-card.receivable-embedded {
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  font-family: var(--app-font-sans);
}
.receivable-embedded .receivable-header {
  margin-bottom: 12px;
}
.receivable-embedded .receivable-grid {
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
}
.receivable-embedded .estimated-metric {
  border-left: 0;
  padding-left: 0;
  border-top: 1px solid var(--app-border);
  padding-top: 12px;
}
.receivable-embedded .estimated-metric .metric-value {
  font-size: var(--app-number-secondary);
}
.receivable-embedded
  :is(
    .metric-label,
    .metric-detail,
    .receivable-note,
    .receivable-review,
    .review-reasons,
    .review-total
  ) {
  color: var(--app-text);
  font-size: 13px;
}
.receivable-embedded .receivable-warning {
  font-size: 13px;
}
@media (min-width: 1025px) {
  .receivable-card {
    margin: var(--app-space-md) 0;
    padding: var(--app-space-sm) var(--app-space-md);
  }
  .receivable-header {
    margin-bottom: var(--app-space-xs);
  }
  .receivable-header h2 {
    font-size: 18px;
  }
  .receivable-grid {
    gap: var(--app-space-md);
  }
  .metric-label {
    margin-bottom: var(--app-space-xs);
  }
  .pending-composition {
    margin-top: var(--app-space-sm);
  }
  .estimated-metric {
    padding-left: var(--app-space-lg);
  }
}
@media (max-width: 767px) {
  .receivable-grid {
    grid-template-columns: 1fr;
    gap: 18px;
  }
  .estimated-metric {
    border-left: 0;
    padding-left: 0;
    border-top: 1px solid var(--app-border);
    padding-top: 18px;
  }
}

@media (max-width: 640px) {
  .receivable-card {
    padding: 16px;
  }
  .receivable-header a {
    min-height: 44px;
  }
  .receivable-note summary {
    min-height: 44px;
    line-height: 44px;
  }
}

.receivable-card.receivable-compact {
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  font-family: var(--app-font-sans);
}
.compact-return-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.compact-return-label,
.compact-return-detail,
.compact-return-status,
.compact-return-warning {
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.compact-return-label,
.compact-return-detail {
  color: var(--app-text-muted);
}
.compact-return-label,
.compact-return-tools,
.compact-return-detail,
.compact-return-status {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0 10px;
}
.compact-return-label {
  align-items: center;
  min-height: 24px;
  margin-bottom: 4px;
}
.compact-return-tools {
  margin-left: auto;
}
.compact-return-tools a,
.compact-explanation-trigger {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  border: 0;
  padding: 0;
  background: transparent;
  color: var(--app-primary-strong);
  font: inherit;
  cursor: pointer;
}
.compact-explanation-trigger:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 2px;
}
.compact-explanation-trigger {
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 4px;
  color: var(--app-text-muted);
}
.compact-explanation-trigger svg {
  width: 14px;
  height: 14px;
}
.compact-explanation-trigger.has-review {
  color: var(--app-warning-text);
}
.compact-return-value {
  display: block;
  font-size: var(--app-number-secondary);
  font-weight: 600;
  line-height: 1.4;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.compact-return-detail {
  font-variant-numeric: tabular-nums;
}
.compact-return-detail strong {
  color: var(--app-text);
}
.compact-return-status,
.compact-return-warning {
  margin: 4px 0 0;
}
.compact-return-warning {
  color: var(--app-warning-text);
}
.compact-explanation {
  font-size: 13px;
  line-height: 1.7;
  color: var(--app-text-muted);
  overflow-wrap: anywhere;
}
.compact-explanation p {
  margin: 0 0 8px;
}
.compact-explanation p:last-child {
  margin-bottom: 0;
}
@media (min-width: 1100px) {
  .compact-return-grid > div + div {
    border-left: 1px solid var(--app-border);
    padding-left: 16px;
  }
}
@media (max-width: 640px) {
  .compact-return-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 8px;
  }
  .compact-return-tools a,
  .compact-explanation-trigger {
    min-height: 44px;
  }
  .compact-explanation-trigger {
    width: 44px;
    height: 44px;
  }
}
</style>
