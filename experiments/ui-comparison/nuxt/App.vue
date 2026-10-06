<script setup lang="ts">
import { computed } from 'vue'
import { parseDate, type DateValue } from '@internationalized/date'
import { SelectItemText } from 'reka-ui'
import { zh_cn } from '@nuxt/ui/locale'
import UApp from '@nuxt/ui/components/App.vue'
import UButton from '@nuxt/ui/components/Button.vue'
import UCalendar from '@nuxt/ui/components/Calendar.vue'
import UForm from '@nuxt/ui/components/Form.vue'
import UFormField from '@nuxt/ui/components/FormField.vue'
import UInput from '@nuxt/ui/components/Input.vue'
import UInputDate from '@nuxt/ui/components/InputDate.vue'
import UInputNumber from '@nuxt/ui/components/InputNumber.vue'
import UModal from '@nuxt/ui/components/Modal.vue'
import UPopover from '@nuxt/ui/components/Popover.vue'
import USelect from '@nuxt/ui/components/Select.vue'
import UTable, { type TableColumn } from '@nuxt/ui/components/Table.vue'
import Cell from '../shared/Cell.vue'
import {
  accounts,
  accountName,
  sampleCounts,
  useComparison,
  validateTrade,
  type SampleRow
} from '../shared/model'

const {
  count,
  query,
  sort,
  rows,
  open,
  draft,
  errors,
  saved,
  reset,
  openTrade,
  submit,
  toggleSort
} = useComparison('nuxt')
const accountOptions = accounts.filter((account) => account.value !== 'all')
const startDate = computed({
  get: () => (query.start ? parseDate(query.start) : undefined),
  set: (value: DateValue | undefined) => {
    query.start = value?.toString() || ''
  }
})
const endDate = computed({
  get: () => (query.end ? parseDate(query.end) : undefined),
  set: (value: DateValue | undefined) => {
    query.end = value?.toString() || ''
  }
})
const tradeDate = computed({
  get: () => (draft.date ? parseDate(draft.date) : undefined),
  set: (value: DateValue | undefined) => {
    draft.date = value?.toString() || ''
  }
})
const sampleMonth = parseDate('2026-09-01')
const calendarNavigation = {
  prevMonth: { icon: '', label: '‹' },
  nextMonth: { icon: '', label: '›' },
  yearControls: false
}
const sortText = computed(() =>
  sort.value === 'desc' ? '浮动盈亏 ↓' : sort.value === 'asc' ? '浮动盈亏 ↑' : '浮动盈亏 ↕'
)
const tableSorting = computed(() =>
  sort.value === 'original' ? [] : [{ id: 'profit', desc: sort.value === 'desc' }]
)
const columnPinning = { right: ['actions'] }
const columns: TableColumn<SampleRow>[] = [
  { id: 'expand', header: '', size: 36 },
  { id: 'name', header: '标的 / 账户', size: 240 },
  { id: 'quantity', header: '持仓 / 均价', size: 130 },
  { id: 'price', header: '现价 / 样本日期', size: 140 },
  { id: 'value', header: '市值 / CNY', size: 180 },
  { accessorKey: 'profit', id: 'profit', header: '浮动盈亏', size: 170, enableSorting: true },
  { id: 'actions', header: '操作', size: 100 }
].map((column) => {
  const width: Record<string, string> =
    column.id === 'name' ? { minWidth: '240px' } : { width: `${column.size}px` }
  return {
    ...column,
    meta: {
      class: {
        th: ['quantity', 'price', 'value', 'profit'].includes(column.id)
          ? 'numeric-heading'
          : column.id === 'expand'
            ? 'expand-cell'
            : '',
        td: column.id === 'expand' ? 'expand-cell' : ''
      },
      style: { th: width, td: width }
    }
  }
})
function validateDraft() {
  errors.value = validateTrade(draft)
  return Object.entries(errors.value).map(([name, message]) => ({ name, message }))
}
// Keep Escape dismissal while blocking accidental overlay clicks.
const modalContent = { onInteractOutside: (event: Event) => event.preventDefault() }
</script>

<template>
  <UApp :locale="zh_cn" :toaster="null">
    <main class="comparison-page">
      <nav class="comparison-links" aria-label="对照版本">
        <a href="/">对照说明</a><a href="/element/">Element Plus</a><a href="/naive/">Naive UI</a
        ><a href="/nuxt/" aria-current="page">Nuxt UI</a>
      </nav>
      <header class="page-heading">
        <div>
          <h1>持仓与交易</h1>
          <p>Nuxt UI 4.11.3 · 同一份合成数据，仅用于组件对照</p>
        </div>
        <UButton data-testid="open-trade" @click="openTrade()">录入演示交易</UButton>
      </header>
      <section class="filters" aria-label="样本筛选">
        <div class="filter-field filter-search">
          <label for="search">标的</label
          ><UInput
            id="search"
            v-model="query.keyword"
            data-testid="search-input"
            placeholder="名称或代码"
          />
        </div>
        <div class="filter-field">
          <label for="account">账户</label
          ><USelect
            id="account"
            v-model="query.account"
            :items="accounts"
            :ui="{ content: 'nuxt-popup' }"
            data-testid="account-filter"
            aria-label="账户"
            ><template #trailing><span aria-hidden="true">▾</span></template
            ><template #item="{ item }"
              ><SelectItemText>{{ item.label }}</SelectItemText></template
            ></USelect
          >
        </div>
        <div class="filter-field filter-date">
          <label for="start">样本交易起日</label>
          <UInputDate
            id="start"
            v-model="startDate"
            locale="zh-CN"
            :default-placeholder="sampleMonth"
            data-testid="start-date"
            aria-label="样本交易起日"
            class="nuxt-date-input"
          >
            <template #trailing
              ><UPopover :ui="{ content: 'nuxt-popup' }"
                ><UButton
                  color="neutral"
                  variant="ghost"
                  class="nuxt-date-button"
                  aria-label="选择开始日期"
                  >日历</UButton
                ><template #content
                  ><UCalendar
                    v-model="startDate"
                    locale="zh-CN"
                    :default-placeholder="sampleMonth"
                    v-bind="calendarNavigation" /></template></UPopover
            ></template>
          </UInputDate>
        </div>
        <div class="filter-field filter-date">
          <label for="end">样本交易止日</label>
          <UInputDate
            id="end"
            v-model="endDate"
            locale="zh-CN"
            :default-placeholder="sampleMonth"
            data-testid="end-date"
            aria-label="样本交易止日"
            class="nuxt-date-input"
          >
            <template #trailing
              ><UPopover :ui="{ content: 'nuxt-popup' }"
                ><UButton
                  color="neutral"
                  variant="ghost"
                  class="nuxt-date-button"
                  aria-label="选择结束日期"
                  >日历</UButton
                ><template #content
                  ><UCalendar
                    v-model="endDate"
                    locale="zh-CN"
                    :default-placeholder="sampleMonth"
                    v-bind="calendarNavigation" /></template></UPopover
            ></template>
          </UInputDate>
        </div>
        <div class="filter-field">
          <label for="count">样本数量</label
          ><USelect
            id="count"
            v-model="count"
            :items="sampleCounts"
            :ui="{ content: 'nuxt-popup' }"
            data-testid="sample-count"
            aria-label="样本数量"
            ><template #trailing><span aria-hidden="true">▾</span></template
            ><template #item="{ item }"
              ><SelectItemText>{{ item.label }}</SelectItemText></template
            ></USelect
          >
        </div>
        <UButton data-testid="reset-filters" color="neutral" variant="outline" @click="reset"
          >重置</UButton
        >
      </section>
      <div class="summary-line">
        <span data-testid="result-count" aria-live="polite">{{ rows.length }} 条记录</span
        ><span>原币与 CNY 并列 · 未知保持为 — · 日期筛选仅作用于样本</span>
      </div>
      <section class="table-region" aria-label="持仓列表">
        <UTable
          :data="rows"
          :columns="columns"
          :get-row-id="(row) => String(row.id)"
          :column-pinning="columnPinning"
          :sorting="tableSorting"
          :sorting-options="{ manualSorting: true }"
          :expanded-options="{ getRowCanExpand: () => true }"
          sticky
          class="nuxt-holdings-table"
          data-testid="holding-table"
          empty="没有符合筛选条件的样本"
        >
          <template #expand-cell="{ row }"
            ><UButton
              color="neutral"
              variant="ghost"
              size="xs"
              :data-testid="`expand-row-${row.original.id}`"
              :aria-label="`${row.getIsExpanded() ? '收起' : '展开'}账户 ${row.original.symbol}`"
              :aria-expanded="row.getIsExpanded()"
              @click="row.toggleExpanded()"
              >{{ row.getIsExpanded() ? '−' : '+' }}</UButton
            ></template
          >
          <template #name-cell="{ row }"><Cell :row="row.original" kind="name" /></template>
          <template #quantity-cell="{ row }"><Cell :row="row.original" kind="quantity" /></template>
          <template #price-cell="{ row }"><Cell :row="row.original" kind="price" /></template>
          <template #value-cell="{ row }"><Cell :row="row.original" kind="value" /></template>
          <template #profit-header
            ><UButton
              class="profit-sort"
              color="neutral"
              variant="link"
              data-testid="sort-profit"
              :aria-label="`${sortText}，点击切换排序`"
              @click="toggleSort"
              >{{ sortText }}</UButton
            ></template
          >
          <template #profit-cell="{ row }"><Cell :row="row.original" kind="profit" /></template>
          <template #actions-cell="{ row }"
            ><UButton
              variant="link"
              size="sm"
              :data-testid="`edit-row-${row.original.id}`"
              :aria-label="`编辑交易 ${row.original.symbol}`"
              @click="openTrade(row.original)"
              >编辑交易</UButton
            ></template
          >
          <template #expanded="{ row }"
            ><div :data-testid="`expanded-row-${row.original.id}`" class="expanded-detail">
              <strong>{{ accountName(row.original.account) }}</strong> · {{ row.original.symbol }} ·
              该行使用合成数据。账户明细展开后可检查变高行及固定列的对齐。
            </div></template
          >
        </UTable>
      </section>
      <div v-if="saved" class="saved-receipt" role="status" data-testid="saved-receipt">
        演示已保存：{{ saved.symbol }} · {{ saved.date }} · {{ saved.quantity }} 股 · 单价
        {{ saved.price }}。没有发送到账本。
      </div>
      <p class="page-footnote">
        此页复现持仓复合单元格与交易录入的代表性交互，不是完整产品页面。所有操作只在当前浏览器内存中生效。
      </p>
      <UModal
        v-model:open="open"
        title="录入演示交易"
        description="仅保存演示结果，不会写入真实交易或账户。"
        :content="modalContent"
        :ui="{ overlay: 'nuxt-trade-overlay' }"
        class="nuxt-trade-modal"
      >
        <template #close
          ><UButton color="neutral" variant="ghost" aria-label="关闭">关闭</UButton></template
        >
        <template #body>
          <div data-testid="trade-dialog">
            <UForm :state="draft" :validate="validateDraft" :validate-on="[]" @submit="submit">
              <div class="dialog-fields">
                <UFormField label="标的代码" name="symbol" :error="errors.symbol"
                  ><UInput v-model="draft.symbol" data-testid="trade-symbol" aria-label="标的代码"
                /></UFormField>
                <UFormField label="账户" name="account" :error="errors.account"
                  ><USelect
                    v-model="draft.account"
                    :items="accountOptions"
                    :ui="{ content: 'nuxt-popup' }"
                    data-testid="trade-account"
                    aria-label="交易账户"
                    ><template #trailing><span aria-hidden="true">▾</span></template
                    ><template #item="{ item }"
                      ><SelectItemText>{{ item.label }}</SelectItemText></template
                    ></USelect
                  ></UFormField
                >
                <UFormField label="交易日期" name="date" :error="errors.date">
                  <UInputDate
                    v-model="tradeDate"
                    locale="zh-CN"
                    data-testid="trade-date"
                    aria-label="交易日期"
                    class="nuxt-date-input"
                  >
                    <template #trailing
                      ><UPopover :ui="{ content: 'nuxt-popup' }"
                        ><UButton
                          color="neutral"
                          variant="ghost"
                          class="nuxt-date-button"
                          aria-label="选择交易日期"
                          >日历</UButton
                        ><template #content
                          ><UCalendar
                            v-model="tradeDate"
                            locale="zh-CN"
                            v-bind="calendarNavigation" /></template></UPopover
                    ></template>
                  </UInputDate>
                </UFormField>
                <UFormField label="数量" name="quantity" :error="errors.quantity"
                  ><UInputNumber
                    :model-value="draft.quantity ?? undefined"
                    :increment="false"
                    :decrement="false"
                    :format-options="{ maximumFractionDigits: 4, useGrouping: false }"
                    :step-snapping="false"
                    data-testid="trade-quantity"
                    aria-label="数量"
                    @update:model-value="draft.quantity = $event ?? null"
                /></UFormField>
                <UFormField label="成交价格" name="price" :error="errors.price"
                  ><UInputNumber
                    :model-value="draft.price ?? undefined"
                    :increment="false"
                    :decrement="false"
                    :format-options="{ maximumFractionDigits: 4, useGrouping: false }"
                    :step-snapping="false"
                    data-testid="trade-price"
                    aria-label="成交价格"
                    @update:model-value="draft.price = $event ?? null"
                /></UFormField>
              </div>
              <div class="dialog-actions">
                <UButton
                  data-testid="cancel-trade"
                  color="neutral"
                  variant="outline"
                  @click="open = false"
                  >取消</UButton
                ><UButton data-testid="submit-trade" type="submit">保存演示</UButton>
              </div>
            </UForm>
          </div>
        </template>
      </UModal>
    </main>
  </UApp>
</template>
