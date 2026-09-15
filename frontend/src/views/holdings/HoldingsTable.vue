<script setup lang="ts">
import { useRouter } from 'vue-router'
import { ArrowRight } from '@element-plus/icons-vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import {
  formatNumber,
  formatCurrency,
  formatDateTime,
  formatPercent,
  toNumber
} from '@/utils/helpers'
import type { Holding } from '@/stores/holdings'
import type { HoldingsTableFeature } from './useHoldingsTable'
import type { SecurityBadgesFeature } from './useSecurityBadges'

defineProps<{ table: HoldingsTableFeature; badges: SecurityBadgesFeature }>()

defineEmits<{ transfer: [row: Holding] }>()

const isMobileView = useMediaQuery('(max-width: 640px)')
const router = useRouter()

function openSecurityDetail(row: { symbol: string; market: string }) {
  router.push(`/securities/${encodeURIComponent(row.market)}/${encodeURIComponent(row.symbol)}`)
}
</script>

<template>
  <div v-if="!isMobileView" class="responsive-table desktop-data-table">
    <el-table :data="table.visibleHoldings" v-loading="table.state.loading" stripe>
      <template #empty>
        <el-empty description="暂无持仓数据" :image-size="88" />
      </template>
      <el-table-column prop="symbol" label="代码" min-width="90" />
      <el-table-column label="名称" min-width="150">
        <template #default="{ row }">
          <el-link
            type="primary"
            :underline="false"
            class="holding-name"
            @click="openSecurityDetail(row)"
          >
            {{ row.name }}
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
        </template>
      </el-table-column>
      <el-table-column prop="market" label="市场" width="80" />
      <el-table-column label="账户" min-width="110" show-overflow-tooltip>
        <template #default="{ row }">
          <span :class="{ 'account-unassigned': !row.broker_account_id }">
            {{ table.accountLabel(row.broker_account_id) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="quantity" label="持仓数量" min-width="120" align="right">
        <template #default="{ row }">
          {{ formatNumber(row.quantity, 4) }}
        </template>
      </el-table-column>
      <el-table-column prop="avg_cost" label="平均成本" min-width="105" align="right">
        <template #default="{ row }">
          {{ formatNumber(row.avg_cost, 4) }}
        </template>
      </el-table-column>
      <el-table-column prop="total_cost" label="总成本" min-width="135" align="right">
        <template #default="{ row }">
          <span class="accent-strong">
            {{ formatCurrency(row.total_cost, row.currency) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="当前价格" width="150" align="right">
        <template #default="{ row }">
          <!-- 表格行内不放 +/- 步进钮：132px 下会把输入框压碎（截图实锤） -->
          <el-input-number
            v-model="table.state.currentPrices[table.priceKey(row)]"
            :min="0"
            :precision="4"
            size="small"
            :controls="false"
            @change="table.savePriceToDatabase(row)"
          />
        </template>
      </el-table-column>
      <el-table-column label="当前市值" min-width="130" align="right">
        <template #default="{ row }">
          <span v-if="table.state.currentPrices[table.priceKey(row)]" style="font-weight: bold">
            {{
              formatCurrency(
                (table.state.currentPrices[table.priceKey(row)] ?? 0) * toNumber(row.quantity),
                row.currency
              )
            }}
          </span>
          <span v-else style="color: var(--app-text-soft)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="浮动盈亏" min-width="115" align="right">
        <template #default="{ row }">
          <span
            v-if="table.state.currentPrices[table.priceKey(row)]"
            :style="{ fontWeight: 'bold', color: table.getProfitColor(row) }"
          >
            {{ formatCurrency(table.calculateProfitAmount(row), row.currency) }}
          </span>
          <span v-else style="color: var(--app-text-soft)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="收益率" min-width="90" align="right">
        <template #default="{ row }">
          <span
            v-if="table.state.currentPrices[table.priceKey(row)]"
            :style="{ fontWeight: 'bold', color: table.getProfitColor(row) }"
          >
            {{ formatPercent(table.calculateProfitRate(row)) }}
          </span>
          <span v-else style="color: var(--app-text-soft)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="AI 标签" min-width="130">
        <template #default="{ row }">
          <template v-if="badges.analysisFor(row)">
            <el-tooltip :content="badges.analysisFor(row)!.summary">
              <span class="ai-tags" data-testid="ai-tags">
                <el-tag
                  :type="badges.riskTagType(badges.analysisFor(row)!.risk_level)"
                  size="small"
                  effect="plain"
                >
                  {{
                    badges.riskLabels[badges.analysisFor(row)!.risk_level] ||
                    badges.analysisFor(row)!.risk_level
                  }}
                </el-tag>
                <el-tag
                  v-for="tag in badges.analysisFor(row)!.tags.slice(0, 2)"
                  :key="tag"
                  size="small"
                  effect="plain"
                  type="warning"
                >
                  {{ tag }}
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
      <el-table-column prop="currency" label="币种" width="70" />
      <el-table-column prop="updated_at" label="更新时间" min-width="150">
        <template #default="{ row }">
          {{ formatDateTime(row.updated_at) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button type="primary" size="small" text @click="$emit('transfer', row)">
            转仓
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>

  <div v-else v-loading="table.state.loading" class="mobile-card-list">
    <article
      v-for="row in table.visibleHoldings"
      :key="`${row.market}-${row.symbol}-${row.broker_account_id ?? 'null'}`"
      class="mobile-card"
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
          <el-tag size="small" effect="plain">{{ row.market }}</el-tag>
          <el-tag size="small" type="info" effect="plain">
            {{ table.accountLabel(row.broker_account_id) }}
          </el-tag>
          <el-tag v-if="badges.upcomingEvent(row)" type="warning" size="small" effect="plain">
            {{ badges.upcomingEvent(row)!.label }}·{{ badges.upcomingEvent(row)!.daysText }}
          </el-tag>
          <!-- AI 标签：移动端只取 1 个（标签行已有市场/账户/事件三个 tag）。
               没有这块，手机上发起批量分析后回到本页看不到任何结果 -->
          <template v-if="badges.analysisFor(row)">
            <el-tag
              :type="badges.riskTagType(badges.analysisFor(row)!.risk_level)"
              size="small"
              effect="plain"
              data-testid="ai-tags"
            >
              {{
                badges.riskLabels[badges.analysisFor(row)!.risk_level] ||
                badges.analysisFor(row)!.risk_level
              }}
            </el-tag>
            <el-tag
              v-if="badges.analysisFor(row)!.tags[0]"
              size="small"
              effect="plain"
              type="warning"
            >
              {{ badges.analysisFor(row)!.tags[0] }}
            </el-tag>
          </template>
        </div>
      </div>

      <div class="mobile-card-metrics">
        <div>
          <span class="mobile-metric-label">总成本</span>
          <strong>{{ formatCurrency(row.total_cost, row.currency) }}</strong>
        </div>
        <div>
          <span class="mobile-metric-label">当前市值</span>
          <strong v-if="table.state.currentPrices[table.priceKey(row)]">
            {{
              formatCurrency(
                (table.state.currentPrices[table.priceKey(row)] ?? 0) * toNumber(row.quantity),
                row.currency
              )
            }}
          </strong>
          <strong v-else>-</strong>
        </div>
        <div>
          <span class="mobile-metric-label">浮动盈亏</span>
          <strong
            v-if="table.state.currentPrices[table.priceKey(row)]"
            :style="{ color: table.getProfitColor(row) }"
          >
            {{ formatCurrency(table.calculateProfitAmount(row), row.currency) }}
          </strong>
          <strong v-else>-</strong>
        </div>
        <div>
          <span class="mobile-metric-label">收益率</span>
          <strong
            v-if="table.state.currentPrices[table.priceKey(row)]"
            :style="{ color: table.getProfitColor(row) }"
          >
            {{ formatPercent(table.calculateProfitRate(row)) }}
          </strong>
          <strong v-else>-</strong>
        </div>
      </div>

      <div class="mobile-card-meta">
        <span>数量 {{ formatNumber(row.quantity, 4) }}</span>
        <span>成本 {{ formatNumber(row.avg_cost, 4) }}</span>
        <span>{{ row.currency }}</span>
      </div>

      <div class="mobile-price-row">
        <span>当前价格</span>
        <el-input-number
          v-model="table.state.currentPrices[table.priceKey(row)]"
          :min="0"
          :precision="4"
          size="small"
          @change="table.savePriceToDatabase(row)"
        />
      </div>

      <div class="mobile-card-actions">
        <el-button type="primary" size="small" text @click="$emit('transfer', row)">
          转仓到其他账户
        </el-button>
      </div>
    </article>
    <el-empty
      v-if="!table.state.loading && table.visibleHoldings.length === 0"
      description="暂无持仓数据"
      :image-size="88"
    />
  </div>
</template>

<style scoped>
.holding-name {
  margin-right: 6px;
}

.ai-tags {
  display: inline-flex;
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

.account-unassigned {
  color: var(--app-text-soft);
}

:deep(.el-input-number) {
  width: 112px;
}

@media (max-width: 640px) {
  /* 卡片通用外观见 styles.css 的 .mobile-card 套件；这里只留持仓特有的价格输入行 */
  .mobile-price-row {
    display: grid;
    grid-template-columns: 72px minmax(0, 1fr);
    align-items: center;
    gap: 10px;
    margin-top: 12px;
    color: var(--app-text-muted);
    font-size: 13px;
  }

  .mobile-price-row :deep(.el-input-number) {
    width: 100%;
  }
}
</style>
