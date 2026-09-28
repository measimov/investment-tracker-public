<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, EditPen } from '@element-plus/icons-vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import {
  formatNumber,
  formatCurrency,
  formatDate,
  formatPercent,
  formatPrice,
  formatQuantity,
  toNumber
} from '@/utils/helpers'
import type { Holding } from '@/stores/holdings'
import type { HoldingRow, HoldingsTableFeature } from './useHoldingsTable'
import type { SecurityBadgesFeature } from './useSecurityBadges'
import type { SortOrder } from './display'
import { displayAnalysisTag, industryTooltip } from './display'

const props = defineProps<{ table: HoldingsTableFeature; badges: SecurityBadgesFeature }>()

defineEmits<{ transfer: [row: Holding] }>()

const isMobileView = useMediaQuery('(max-width: 640px)')
const router = useRouter()

// 进入编辑态即聚焦并全选：点一下就能直接输入新价
const vFocus = {
  mounted(el: HTMLElement) {
    const input = el.querySelector('input')
    input?.focus()
    input?.select()
  }
}

function openSecurityDetail(row: { symbol: string; market: string }) {
  router.push(`/securities/${encodeURIComponent(row.market)}/${encodeURIComponent(row.symbol)}`)
}

function isAccountView() {
  return props.table.state.viewMode === 'account'
}

// 合并视图下只有多账户行可展开：单账户行展开内容与主行重复，隐藏其展开箭头；
// 深链定位的目标行加高亮
function rowClassName({ row }: { row: HoldingRow }) {
  const classes: string[] = []
  if (!isAccountView() && row.accounts.length <= 1) classes.push('single-account-row')
  if (props.table.isFocused(row)) classes.push('holding-focus-row')
  return classes.join(' ')
}

// 深链定位：目标行渲染出来后滚到视口中间，每个定位目标只滚一次（之后排序/改价不再抢滚动）
const rootEl = ref<HTMLElement | null>(null)
let scrolledFocus: object | null = null
watch(
  () => [props.table.state.focus, props.table.rows, isMobileView.value] as const,
  async ([focus]) => {
    if (!focus || focus === scrolledFocus) return
    await nextTick()
    const target = rootEl.value?.querySelector('.holding-focus-row, .mobile-card--focus')
    if (!target) return
    scrolledFocus = focus
    target.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
  },
  { flush: 'post' }
)

function onSortChange({ prop, order }: { prop: string | null; order: SortOrder }) {
  props.table.setSort({ prop, order })
}

// 回车 = 失焦提交：el-input-number 在原生 change（失焦前触发）时才回写 v-model，
// 直接在 keydown 里读草稿会拿到旧值
function submitPriceOnEnter(event: KeyboardEvent) {
  ;(event.target as HTMLElement | null)?.blur?.()
}

function accountSummary(row: HoldingRow): string {
  if (row.accounts.length <= 1) return props.table.accountLabel(row.accounts[0]?.broker_account_id)
  return `${row.accounts.length} 个账户`
}

function riskText(level: string) {
  return `${props.badges.riskLabels[level] || level}风险`
}
</script>

<template>
  <div v-if="!isMobileView" ref="rootEl" class="responsive-table desktop-data-table">
    <!-- 排序由 useHoldingsTable 做（sortable="custom"）：缺价/缺汇率的行无论升降序都沉底；
         default-sort 只负责让表头箭头高亮出当前排序 -->
    <el-table
      :data="table.rows"
      :row-key="(row: HoldingRow) => row.key"
      :row-class-name="rowClassName"
      :default-sort="{ prop: 'marketValue', order: 'descending' }"
      v-loading="table.state.loading"
      class="holdings-table"
      @sort-change="onSortChange"
    >
      <template #empty>
        <el-empty
          :description="table.isFiltered ? '没有符合筛选条件的持仓' : '暂无持仓数据'"
          :image-size="88"
        />
      </template>

      <!-- 合并视图：多账户行展开看各账户明细与转仓；单账户行不可展开（账户名在副行），
           按账户视图不需要展开 -->
      <el-table-column v-if="!isAccountView()" type="expand" width="36">
        <template #default="{ row }">
          <div class="account-breakdown" data-testid="holding-accounts">
            <div v-for="holding in row.accounts" :key="holding.id" class="account-line">
              <span
                class="account-name"
                :class="{ 'account-unassigned': !holding.broker_account_id }"
              >
                {{ table.accountLabel(holding.broker_account_id) }}
              </span>
              <span class="num">{{ formatQuantity(holding.quantity) }} 股</span>
              <span class="num muted">均价 {{ formatPrice(holding.avg_cost) }}</span>
              <span class="num muted"
                >成本 {{ formatCurrency(holding.total_cost, holding.currency) }}</span
              >
              <el-button type="primary" size="small" text @click="$emit('transfer', holding)">
                转仓
              </el-button>
            </div>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="标的" width="200">
        <template #default="{ row }">
          <div class="cell-title">
            <el-link
              type="primary"
              :underline="false"
              class="holding-name"
              @click="openSecurityDetail(row)"
            >
              {{ row.name || row.symbol }}
            </el-link>
            <el-tooltip
              v-if="badges.upcomingEvent(row)"
              :content="badges.eventTooltip(row)"
              placement="top"
            >
              <el-tag
                type="warning"
                size="small"
                effect="plain"
                class="event-badge"
                data-testid="security-event-badge"
              >
                {{ badges.upcomingEvent(row)!.label }}·{{ badges.upcomingEvent(row)!.daysText }}
              </el-tag>
            </el-tooltip>
          </div>
          <div class="cell-sub">
            {{ row.symbol }} · {{ row.market }}
            <template v-if="badges.industryFor(row)">
              ·
              <el-tooltip
                :content="industryTooltip(badges.industryFor(row)!.source)"
                placement="top"
              >
                <span
                  class="industry-text"
                  :class="{
                    'industry-unofficial': badges.industryFor(row)!.source === 'eastmoney'
                  }"
                  data-testid="holding-industry"
                  >{{ badges.industryFor(row)!.industry }}</span
                >
              </el-tooltip>
            </template>
            <template v-if="isAccountView() || row.accounts.length === 1">
              ·
              <span :class="{ 'account-unassigned': !row.accounts[0]?.broker_account_id }">
                {{ table.accountLabel(row.accounts[0]?.broker_account_id) }}
              </span>
            </template>
            <template v-else> · {{ accountSummary(row) }}</template>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="持仓 / 均价" width="130" align="right">
        <template #default="{ row }">
          <div class="cell-main num">{{ formatQuantity(row.quantity) }}</div>
          <div class="cell-sub num">
            均价 {{ formatPrice(row.avgCost) }}
            <el-tooltip
              v-if="row.unknownCost > 0"
              content="其中这部分来自成本未知的期初建仓/转托管转入，平均成本与已实现盈亏为估计值；可在公司行动页补录成本"
            >
              <el-tag type="warning" size="small" effect="plain" class="unknown-cost-tag">
                成本未知 {{ formatQuantity(row.unknownCost) }}
              </el-tag>
            </el-tooltip>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="现价" width="160" align="right">
        <template #default="{ row }">
          <!-- 默认只读；点数值或铅笔进入编辑，回车/失焦保存、Esc 取消。
               保存作用于该标的全部账户，所以不做常驻输入框（易误改） -->
          <div
            v-if="table.isEditingPrice(row)"
            class="price-editor"
            @keydown.enter.prevent="submitPriceOnEnter"
            @keydown.esc.prevent="table.cancelPriceEdit()"
          >
            <el-input-number
              v-model="table.state.priceDraft"
              v-focus
              :min="0"
              size="small"
              :controls="false"
              data-testid="price-input"
              @blur="table.commitPriceEdit(row)"
            />
          </div>
          <div v-else class="price-line">
            <el-tag
              v-if="table.priceInfoOf(row)?.manual"
              type="info"
              size="small"
              effect="plain"
              class="price-flag"
              data-testid="price-manual-tag"
            >
              手工
            </el-tag>
            <el-tag
              v-if="table.priceInfoOf(row)?.stale"
              type="warning"
              size="small"
              effect="plain"
              class="price-flag"
              data-testid="price-stale-tag"
            >
              陈价
            </el-tag>
            <button
              type="button"
              class="price-display"
              :aria-label="`编辑 ${row.name || row.symbol} 的现价`"
              data-testid="price-display"
              @click="table.startPriceEdit(row)"
            >
              <span class="cell-main num">{{ formatPrice(table.priceOf(row)) }}</span>
              <el-icon class="price-edit-icon"><EditPen /></el-icon>
            </button>
          </div>
          <el-tooltip :disabled="!table.priceInfoOf(row)" placement="top">
            <template #content>
              <div v-for="line in table.priceInfoOf(row)?.tooltip || []" :key="line">
                {{ line }}
              </div>
            </template>
            <div class="cell-sub" data-testid="price-date">
              {{ row.currency
              }}<template v-if="table.priceInfoOf(row)?.label">
                · {{ table.priceInfoOf(row)!.label }}</template
              >
            </div>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column
        label="市值 / 占比"
        prop="marketValue"
        width="170"
        align="right"
        sortable="custom"
        :sort-orders="['descending', 'ascending', null]"
      >
        <template #default="{ row }">
          <template v-if="table.marketValueOf(row) !== null">
            <div class="cell-main num strong">
              {{ formatCurrency(table.marketValueOf(row), row.currency) }}
            </div>
            <div class="cell-sub num">
              <template v-if="row.currency !== 'CNY'">
                ≈{{ formatCurrency(table.marketValueCNYOf(row)) }} ·
              </template>
              {{ table.weightOf(row) === null ? '—' : `${formatNumber(table.weightOf(row), 1)}%` }}
            </div>
          </template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>

      <el-table-column
        prop="profit"
        width="170"
        align="right"
        sortable="custom"
        :sort-orders="['descending', 'ascending', null]"
      >
        <template #header>
          <el-tooltip
            placement="top"
            content="按折人民币金额排序（缺汇率的行沉底）。成本与市值均按今日汇率折人民币，不含汇兑损益"
          >
            <span class="header-help">浮动盈亏</span>
          </el-tooltip>
        </template>
        <template #default="{ row }">
          <template v-if="table.profitOf(row) !== null">
            <div class="cell-main num strong" :style="{ color: table.getProfitColor(row) }">
              {{ formatCurrency(table.profitOf(row), row.currency) }}
            </div>
            <div class="cell-sub num" :style="{ color: table.getProfitColor(row) }">
              <template v-if="row.currency !== 'CNY'">
                ≈{{ formatCurrency(table.profitCNYOf(row)) }} ·
              </template>
              {{ formatPercent(table.profitRateOf(row)) }}
            </div>
          </template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>

      <el-table-column label="AI" min-width="170">
        <template #default="{ row }">
          <template v-if="badges.analysisFor(row)">
            <el-tooltip placement="top">
              <template #content>
                <div class="ai-tooltip">{{ badges.analysisFor(row)!.summary }}</div>
                <div v-if="badges.analysisFor(row)!.created_at" class="ai-tooltip-date">
                  分析于 {{ formatDate(badges.analysisFor(row)!.created_at) }}
                </div>
              </template>
              <span class="ai-tags" data-testid="ai-tags">
                <!-- 风险用 success/warning/danger，观点标签用中性 info：两者不再同为橙色 -->
                <el-tag
                  :type="badges.riskTagType(badges.analysisFor(row)!.risk_level)"
                  size="small"
                  effect="light"
                >
                  {{ riskText(badges.analysisFor(row)!.risk_level) }}
                </el-tag>
                <el-tag
                  v-if="displayAnalysisTag(badges.analysisFor(row)!.tags)"
                  size="small"
                  effect="plain"
                  type="info"
                >
                  {{ displayAnalysisTag(badges.analysisFor(row)!.tags) }}
                </el-tag>
              </span>
            </el-tooltip>
          </template>
          <template v-if="badges.opinionBadgeTags(row).length">
            <el-tooltip :content="badges.opinionFor(row)?.summary || '雪球观点变化'">
              <span class="ai-tags" data-testid="opinion-tags">
                <el-tag
                  v-for="tag in badges.opinionBadgeTags(row)"
                  :key="tag"
                  size="small"
                  :type="badges.opinionTagType(tag)"
                >
                  {{ tag }}
                </el-tag>
                <el-badge
                  v-if="badges.opinionFor(row)?.new_utterance_count"
                  :value="badges.opinionFor(row)!.new_utterance_count!"
                  type="warning"
                  :max="99"
                  class="opinion-new-badge"
                />
              </span>
            </el-tooltip>
          </template>
          <!-- 不能用 v-else：那会错绑到上面的观点 template（有分析无观点时会双显） -->
          <span v-if="!badges.analysisFor(row)" class="ai-untagged">未分析</span>
        </template>
      </el-table-column>

      <el-table-column v-if="isAccountView()" label="操作" width="72" fixed="right">
        <template #default="{ row }">
          <el-button type="primary" size="small" text @click="$emit('transfer', row.accounts[0])">
            转仓
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>

  <div v-else ref="rootEl" v-loading="table.state.loading" class="mobile-card-list">
    <article
      v-for="row in table.rows"
      :key="row.key"
      class="mobile-card"
      :class="{ 'mobile-card--focus': table.isFocused(row) }"
      data-testid="holding-card"
    >
      <div class="mobile-card-head">
        <!-- 移动端此前是纯 span：桌面端标题是 el-link 可进标的档案，手机上
             整页没有任何入口，AI 分析/财报摘要在手机上根本打不开 -->
        <button
          type="button"
          class="mobile-card-title mobile-card-title-link"
          data-testid="holding-card-title"
          :aria-label="`查看 ${row.name || row.symbol} 的标的档案`"
          @click="openSecurityDetail(row)"
        >
          <span class="mobile-card-symbol" data-testid="holding-card-symbol">
            {{ row.symbol }}
            <el-icon class="mobile-card-title-chevron"><ArrowRight /></el-icon>
          </span>
          <span v-if="row.name" class="mobile-card-name">{{ row.name }}</span>
        </button>
        <div class="mobile-card-tags">
          <!-- 第一个 tag 必须是市场（移动端 E2E 据此拼标的档案路由） -->
          <el-tag size="small" effect="plain">{{ row.market }}</el-tag>
          <el-tooltip
            v-if="badges.industryFor(row)"
            :content="industryTooltip(badges.industryFor(row)!.source)"
            placement="top"
          >
            <el-tag size="small" type="info" effect="plain" data-testid="holding-card-industry">
              {{ badges.industryFor(row)!.industry }}
            </el-tag>
          </el-tooltip>
          <el-tag v-if="badges.upcomingEvent(row)" type="warning" size="small" effect="plain">
            {{ badges.upcomingEvent(row)!.label }}·{{ badges.upcomingEvent(row)!.daysText }}
          </el-tag>
          <template v-if="badges.analysisFor(row)">
            <el-tag
              :type="badges.riskTagType(badges.analysisFor(row)!.risk_level)"
              size="small"
              effect="light"
              data-testid="ai-tags"
            >
              {{ riskText(badges.analysisFor(row)!.risk_level) }}
            </el-tag>
          </template>
        </div>
      </div>

      <div class="mobile-card-metrics">
        <div>
          <span class="mobile-metric-label">市值</span>
          <strong v-if="table.marketValueOf(row) !== null">
            {{ formatCurrency(table.marketValueOf(row), row.currency) }}
          </strong>
          <strong v-else>—</strong>
          <small v-if="row.currency !== 'CNY' && table.marketValueOf(row) !== null" class="approx">
            ≈{{ formatCurrency(table.marketValueCNYOf(row)) }}
          </small>
        </div>
        <div>
          <span class="mobile-metric-label">浮动盈亏</span>
          <strong v-if="table.profitOf(row) !== null" :style="{ color: table.getProfitColor(row) }">
            {{ formatCurrency(table.profitOf(row), row.currency) }}
            <small v-if="table.profitRateOf(row) !== null">
              {{ formatPercent(table.profitRateOf(row)) }}
            </small>
          </strong>
          <strong v-else>—</strong>
          <small
            v-if="row.currency !== 'CNY' && table.profitOf(row) !== null"
            class="approx"
            :style="{ color: table.getProfitColor(row) }"
          >
            ≈{{ formatCurrency(table.profitCNYOf(row)) }}
          </small>
        </div>
      </div>

      <div class="mobile-card-meta">
        <span>数量 {{ formatQuantity(row.quantity) }}</span>
        <span>均价 {{ formatPrice(row.avgCost) }}</span>
        <span v-if="table.weightOf(row) !== null"
          >占比 {{ formatNumber(table.weightOf(row), 1) }}%</span
        >
        <el-tag
          v-if="row.unknownCost > 0"
          type="warning"
          size="small"
          effect="plain"
          class="unknown-cost-tag"
        >
          成本未知 {{ formatQuantity(row.unknownCost) }}
        </el-tag>
      </div>

      <div class="mobile-price-row">
        <span>现价 {{ row.currency }}</span>
        <div
          v-if="table.isEditingPrice(row)"
          class="price-editor"
          @keydown.enter.prevent="submitPriceOnEnter"
          @keydown.esc.prevent="table.cancelPriceEdit()"
        >
          <el-input-number
            v-model="table.state.priceDraft"
            v-focus
            :min="0"
            size="small"
            :controls="false"
            data-testid="price-input"
            @blur="table.commitPriceEdit(row)"
          />
        </div>
        <div v-else class="mobile-price-value">
          <button
            type="button"
            class="price-display"
            :aria-label="`编辑 ${row.name || row.symbol} 的现价`"
            data-testid="price-display"
            @click="table.startPriceEdit(row)"
          >
            <span class="num">{{ formatPrice(table.priceOf(row)) }}</span>
            <el-icon class="price-edit-icon"><EditPen /></el-icon>
          </button>
          <span class="mobile-price-date">
            <el-tag v-if="table.priceInfoOf(row)?.manual" type="info" size="small" effect="plain">
              手工
            </el-tag>
            <el-tag v-if="table.priceInfoOf(row)?.stale" type="warning" size="small" effect="plain">
              陈价
            </el-tag>
            {{ table.priceInfoOf(row)?.label }}
          </span>
        </div>
      </div>

      <div class="mobile-card-accounts">
        <div v-for="holding in row.accounts" :key="holding.id" class="mobile-account-line">
          <span :class="{ 'account-unassigned': !holding.broker_account_id }">
            {{ table.accountLabel(holding.broker_account_id) }} ·
            {{ formatQuantity(toNumber(holding.quantity)) }}
          </span>
          <el-button type="primary" size="small" text @click="$emit('transfer', holding)">
            转仓到其他账户
          </el-button>
        </div>
      </div>
    </article>
    <el-empty
      v-if="!table.state.loading && table.rows.length === 0"
      :description="table.isFiltered ? '没有符合筛选条件的持仓' : '暂无持仓数据'"
      :image-size="88"
    />
  </div>
</template>

<style scoped>
.holdings-table :deep(.el-table__cell) {
  padding: 8px 0;
}

.cell-title {
  display: flex;
  align-items: center;
  gap: 6px;
  line-height: 1.35;
}

.cell-main {
  line-height: 1.35;
}

.cell-sub {
  margin-top: 2px;
  font-size: 12px;
  line-height: 1.3;
  color: var(--app-text-soft);
}

.num {
  font-variant-numeric: tabular-nums;
}

.strong {
  font-weight: 600;
}

.muted {
  color: var(--app-text-soft);
}

.unknown-cost-tag {
  margin-left: 4px;
}

.holding-name {
  font-weight: 600;
}

.ai-tags {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  cursor: help;
}

.ai-untagged {
  color: var(--app-text-soft);
  font-size: 12px;
}

.event-badge {
  cursor: help;
}

.industry-text {
  cursor: help;
}

/* 东方财富（非官方补缺）的行业加虚线下划线，悬浮看来源 */
.industry-unofficial {
  border-bottom: 1px dashed currentColor;
}

.account-unassigned {
  color: var(--app-text-soft);
}

.account-breakdown {
  padding: 4px 16px 4px 48px;
}

.account-line {
  display: grid;
  grid-template-columns: minmax(120px, 1.2fr) repeat(3, minmax(90px, 1fr)) auto;
  align-items: center;
  gap: 12px;
  padding: 4px 0;
  font-size: 13px;
}

.account-line + .account-line {
  border-top: 1px dashed var(--app-border-soft);
}

.account-name {
  font-weight: 500;
}

/* 深链定位的目标行 */
.holdings-table :deep(.holding-focus-row > td.el-table__cell) {
  background-color: var(--app-primary-tint);
}

.mobile-card--focus {
  outline: 2px solid var(--app-primary);
  outline-offset: -2px;
}

/* 单账户行：展开内容与主行重复，隐藏展开箭头（多账户行照常可展开） */
.holdings-table :deep(.single-account-row .el-table__expand-icon) {
  visibility: hidden;
  pointer-events: none;
}

.header-help {
  cursor: help;
  border-bottom: 1px dashed currentColor;
}

.price-line {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
}

.price-display {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  cursor: pointer;
}

.price-edit-icon {
  font-size: 13px;
  color: var(--app-text-soft);
  opacity: 0.55;
}

.price-display:hover .price-edit-icon,
.price-display:focus-visible .price-edit-icon {
  opacity: 1;
  color: var(--app-primary);
}

.price-flag {
  flex-shrink: 0;
}

.ai-tags :deep(.el-tag) {
  white-space: nowrap;
}

.ai-tooltip {
  max-width: 360px;
}

.ai-tooltip-date {
  margin-top: 4px;
  opacity: 0.75;
}

:deep(.el-input-number) {
  width: 112px;
}

@media (max-width: 640px) {
  /* 卡片通用外观见 styles.css 的 .mobile-card 套件；这里只留持仓特有的行 */
  .mobile-price-row {
    display: grid;
    grid-template-columns: 88px minmax(0, 1fr);
    align-items: center;
    gap: 10px;
    margin-top: 12px;
    color: var(--app-text-muted);
    font-size: 13px;
  }

  .mobile-price-row :deep(.el-input-number) {
    width: 100%;
  }

  .mobile-price-value {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 10px;
  }

  .mobile-price-value .price-display {
    font-weight: 600;
    color: var(--app-text);
  }

  .mobile-price-date {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: var(--app-text-soft);
  }

  .approx {
    display: block;
    margin-top: 2px;
    font-size: 12px;
    font-weight: 400;
    color: var(--app-text-soft);
  }

  .mobile-card-accounts {
    margin-top: 8px;
    font-size: 13px;
    color: var(--app-text-muted);
  }

  .mobile-account-line {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
}
</style>
