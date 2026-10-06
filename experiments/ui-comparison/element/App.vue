<script setup lang="ts">
import { computed, type VNode } from 'vue'
import {
  ElButton,
  ElConfigProvider,
  ElDatePicker,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElOption,
  ElSelect,
  ElTable,
  ElTableColumn
} from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import Cell from '../shared/Cell.vue'
import { accounts, accountName, sampleCounts, useComparison, type SampleRow } from '../shared/model'

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
} = useComparison('element')
const accountOptions = accounts.filter((a) => a.value !== 'all')
const sortText = computed(() =>
  sort.value === 'desc' ? '浮动盈亏 ↓' : sort.value === 'asc' ? '浮动盈亏 ↑' : '浮动盈亏 ↕'
)
function dateValue(value: string | null, field: 'start' | 'end') {
  query[field] = value || ''
}
function rowClass({ row }: { row: SampleRow }) {
  return `sample-row-${row.id}`
}
function labelSort(node: VNode) {
  const header = (node.el as HTMLElement | null)?.closest('th')
  header?.setAttribute(
    'aria-sort',
    sort.value === 'desc' ? 'descending' : sort.value === 'asc' ? 'ascending' : 'none'
  )
}
</script>

<template>
  <ElConfigProvider :locale="zhCn">
    <main class="comparison-page">
      <nav class="comparison-links" aria-label="对照版本">
        <a href="/">对照说明</a><a href="/element/" aria-current="page">Element Plus</a
        ><a href="/naive/">Naive UI</a><a href="/nuxt/">Nuxt UI</a>
      </nav>
      <header class="page-heading">
        <div>
          <h1>持仓与交易</h1>
          <p>Element Plus 2.13.2 · 同一份合成数据，仅用于组件对照</p>
        </div>
        <ElButton data-testid="open-trade" type="primary" @click="openTrade()"
          >录入演示交易</ElButton
        >
      </header>
      <section class="filters" aria-label="样本筛选">
        <div class="filter-field filter-search">
          <label for="search">标的</label
          ><ElInput
            id="search"
            v-model="query.keyword"
            data-testid="search-input"
            placeholder="名称或代码"
            clearable
          />
        </div>
        <div class="filter-field">
          <label for="account">账户</label
          ><ElSelect
            id="account"
            v-model="query.account"
            data-testid="account-filter"
            aria-label="账户"
            ><ElOption
              v-for="item in accounts"
              :key="item.value"
              :label="item.label"
              :value="item.value"
          /></ElSelect>
        </div>
        <div class="filter-field filter-date">
          <label for="start">样本交易起日</label
          ><span data-testid="start-date"
            ><ElDatePicker
              id="start"
              :model-value="query.start || null"
              type="date"
              format="YYYY-MM-DD"
              value-format="YYYY-MM-DD"
              placeholder="开始日期"
              @update:model-value="dateValue($event, 'start')"
          /></span>
        </div>
        <div class="filter-field filter-date">
          <label for="end">样本交易止日</label
          ><span data-testid="end-date"
            ><ElDatePicker
              id="end"
              :model-value="query.end || null"
              type="date"
              format="YYYY-MM-DD"
              value-format="YYYY-MM-DD"
              placeholder="结束日期"
              @update:model-value="dateValue($event, 'end')"
          /></span>
        </div>
        <div class="filter-field">
          <label for="count">样本数量</label
          ><ElSelect id="count" v-model="count" data-testid="sample-count" aria-label="样本数量"
            ><ElOption
              v-for="item in sampleCounts"
              :key="item.value"
              :label="item.label"
              :value="item.value"
          /></ElSelect>
        </div>
        <ElButton data-testid="reset-filters" @click="reset">重置</ElButton>
      </section>
      <div class="summary-line">
        <span data-testid="result-count" aria-live="polite">{{ rows.length }} 条记录</span
        ><span>原币与 CNY 并列 · 未知保持为 — · 日期筛选仅作用于样本</span>
      </div>
      <section class="table-region" aria-label="持仓列表">
        <ElTable
          :data="rows"
          row-key="id"
          height="560"
          data-testid="holding-table"
          :row-class-name="rowClass"
          empty-text="没有符合筛选条件的样本"
        >
          <ElTableColumn type="expand" width="36"
            ><template #default="{ row }"
              ><div :data-testid="`expanded-row-${row.id}`" class="expanded-detail">
                <strong>{{ accountName(row.account) }}</strong> · {{ row.symbol }} ·
                该行使用合成数据。账户明细展开后可检查变高行及固定列的对齐。
              </div></template
            ></ElTableColumn
          >
          <ElTableColumn label="标的 / 账户" min-width="240"
            ><template #default="{ row }"><Cell :row="row" kind="name" /></template
          ></ElTableColumn>
          <ElTableColumn label="持仓 / 均价" width="130" align="right"
            ><template #default="{ row }"><Cell :row="row" kind="quantity" /></template
          ></ElTableColumn>
          <ElTableColumn label="现价 / 样本日期" width="140" align="right"
            ><template #default="{ row }"><Cell :row="row" kind="price" /></template
          ></ElTableColumn>
          <ElTableColumn label="市值 / CNY" width="180" align="right"
            ><template #default="{ row }"><Cell :row="row" kind="value" /></template
          ></ElTableColumn>
          <ElTableColumn width="170" align="right"
            ><template #header
              ><button
                class="profit-sort"
                data-testid="sort-profit"
                type="button"
                :aria-label="`${sortText}，点击切换排序`"
                @vue:mounted="labelSort"
                @vue:updated="labelSort"
                @click="toggleSort"
              >
                {{ sortText }}
              </button></template
            ><template #default="{ row }"><Cell :row="row" kind="profit" /></template
          ></ElTableColumn>
          <ElTableColumn label="操作" width="100" fixed="right"
            ><template #default="{ row }"
              ><ElButton
                :data-testid="`edit-row-${row.id}`"
                :aria-label="`编辑交易 ${row.symbol}`"
                link
                type="primary"
                @click="openTrade(row)"
                >编辑交易</ElButton
              ></template
            ></ElTableColumn
          >
        </ElTable>
      </section>
      <div v-if="saved" class="saved-receipt" role="status" data-testid="saved-receipt">
        演示已保存：{{ saved.symbol }} · {{ saved.date }} · {{ saved.quantity }} 股 · 单价
        {{ saved.price }}。没有发送到账本。
      </div>
      <p class="page-footnote">
        此页复现持仓复合单元格与交易录入的代表性交互，不是完整产品页面。所有操作只在当前浏览器内存中生效。
      </p>
      <ElDialog
        v-model="open"
        title="录入演示交易"
        width="560px"
        class="comparison-dialog"
        data-testid="trade-dialog"
        :close-on-click-modal="false"
        destroy-on-close
      >
        <div class="demo-notice">仅保存演示结果，不会写入真实交易或账户。</div>
        <ElForm label-position="top" :model="draft" @submit.prevent="submit">
          <div class="dialog-fields">
            <ElFormItem label="标的代码" :error="errors.symbol"
              ><ElInput v-model="draft.symbol" data-testid="trade-symbol" aria-label="标的代码"
            /></ElFormItem>
            <ElFormItem label="账户" :error="errors.account"
              ><ElSelect v-model="draft.account" data-testid="trade-account" aria-label="交易账户"
                ><ElOption
                  v-for="item in accountOptions"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value" /></ElSelect
            ></ElFormItem>
            <ElFormItem label="交易日期" :error="errors.date"
              ><span data-testid="trade-date"
                ><ElDatePicker
                  v-model="draft.date"
                  type="date"
                  format="YYYY-MM-DD"
                  value-format="YYYY-MM-DD"
                  aria-label="交易日期" /></span
            ></ElFormItem>
            <ElFormItem label="数量" :error="errors.quantity"
              ><ElInputNumber
                v-model="draft.quantity"
                :controls="false"
                :precision="4"
                :step="0.0001"
                data-testid="trade-quantity"
                aria-label="数量"
            /></ElFormItem>
            <ElFormItem label="成交价格" :error="errors.price"
              ><ElInputNumber
                v-model="draft.price"
                :controls="false"
                :precision="4"
                :step="0.0001"
                data-testid="trade-price"
                aria-label="成交价格"
            /></ElFormItem>
          </div>
          <div class="dialog-actions">
            <ElButton data-testid="cancel-trade" @click="open = false">取消</ElButton
            ><ElButton data-testid="submit-trade" type="primary" native-type="submit"
              >保存演示</ElButton
            >
          </div>
        </ElForm>
      </ElDialog>
    </main>
  </ElConfigProvider>
</template>
