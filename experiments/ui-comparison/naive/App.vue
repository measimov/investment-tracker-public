<script setup lang="ts">
import {
  computed,
  h,
  nextTick,
  type InputHTMLAttributes,
  type ObjectDirective,
  type VNode
} from 'vue'
import {
  NButton,
  NConfigProvider,
  NDataTable,
  NDatePicker,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NModal,
  NSelect,
  dateZhCN,
  zhCN,
  type DataTableColumns,
  type DataTableSortState,
  type GlobalThemeOverrides
} from 'naive-ui'
import Cell from '../shared/Cell.vue'
import {
  accountName,
  accounts,
  formatDate,
  formatPrice,
  formatQuantity,
  sampleCounts,
  useComparison,
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
} = useComparison('naive')

const theme: GlobalThemeOverrides = {
  common: {
    primaryColor: '#a6533c',
    primaryColorHover: '#93452f',
    primaryColorPressed: '#803b29',
    primaryColorSuppl: '#a6533c',
    bodyColor: '#faf9f5',
    cardColor: '#ffffff',
    modalColor: '#ffffff',
    popoverColor: '#ffffff',
    tableColor: '#ffffff',
    textColorBase: '#242321',
    textColor1: '#242321',
    textColor2: '#5f5b53',
    textColor3: '#6b665c',
    borderColor: '#e5e1d8',
    dividerColor: '#e5e1d8',
    borderRadius: '6px',
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
    fontSize: '14px'
  },
  DataTable: {
    thColor: '#f5f2eb',
    tdColor: '#ffffff',
    thTextColor: '#5f5b53',
    tdTextColor: '#242321',
    borderColor: '#e5e1d8'
  }
}

const tradeAccounts = accounts.filter((account) => account.value !== 'all')
const startDate = computed({
  get: () => query.start || null,
  set: (value) => {
    query.start = value || ''
  }
})
const endDate = computed({
  get: () => query.end || null,
  set: (value) => {
    query.end = value || ''
  }
})
const tradeDate = computed({
  get: () => draft.date || null,
  set: (value) => {
    draft.date = value || ''
  }
})

function inputAttrs(
  id: string,
  testId: string,
  invalid = false
): InputHTMLAttributes & { 'data-testid': string } {
  return { id, 'data-testid': testId, 'aria-invalid': invalid }
}

// DatePicker has no input-attribute passthrough; labels must target its input,
// not its wrapper. These local directives only add DOM semantics for the demo.
type DateFieldAttrs = { id: string; testId: string; invalid?: boolean }
function labelDateInput(el: HTMLElement, attrs: DateFieldAttrs) {
  const input = el.querySelector('input')
  if (!input) return
  input.id = attrs.id
  input.dataset.testid = attrs.testId
  input.setAttribute('aria-invalid', String(!!attrs.invalid))
}
const vDateField: ObjectDirective<HTMLElement, DateFieldAttrs> = {
  mounted: (el, { value }) => labelDateInput(el, value),
  updated: (el, { value }) => labelDateInput(el, value)
}
function labelSelect(el: HTMLElement, labelId: string) {
  const target = el.querySelector<HTMLElement>('[tabindex="0"]')
  target?.setAttribute('aria-labelledby', labelId)
  target?.setAttribute('role', 'button')
  target?.setAttribute('aria-haspopup', 'listbox')
}
const vSelectLabel: ObjectDirective<HTMLElement, string> = {
  mounted: (el, { value }) => labelSelect(el, value),
  updated: (el, { value }) => labelSelect(el, value)
}

function updateSort(value: DataTableSortState | DataTableSortState[] | null) {
  const state = Array.isArray(value) ? value[0] : value
  sort.value = state?.order === 'ascend' ? 'asc' : state?.order === 'descend' ? 'desc' : 'original'
}

// Naive's default sortable header is click-only. Keep its indicator and event
// contract, adding one keyboard target and the state on the actual table header.
function updateHeaderSort(vnode: VNode) {
  const button = vnode.el as HTMLElement | null
  button
    ?.closest('th')
    ?.setAttribute(
      'aria-sort',
      sort.value === 'original' ? 'none' : sort.value === 'asc' ? 'ascending' : 'descending'
    )
}

const columns = computed<DataTableColumns<SampleRow>>(() => [
  {
    type: 'expand',
    width: 36,
    renderExpand: (row) =>
      h('div', { class: 'expanded-detail', 'data-testid': `expanded-row-${row.id}` }, [
        h('strong', accountName(row.account)),
        ` · 最近交易 ${formatDate(row.date)} · ${row.currency} · 数量 ${formatQuantity(row.quantity)}。`,
        h('div', row.note || '账户明细使用与持仓行相同的演示数据。')
      ])
  },
  {
    key: 'name',
    title: '标的 / 账户',
    minWidth: 240,
    render: (row) => h(Cell, { row, kind: 'name' })
  },
  {
    key: 'quantity',
    title: '数量 / 成本',
    width: 130,
    align: 'right',
    render: (row) => h(Cell, { row, kind: 'quantity' })
  },
  {
    key: 'price',
    title: '最新价',
    width: 140,
    align: 'right',
    render: (row) => h(Cell, { row, kind: 'price' })
  },
  {
    key: 'value',
    title: '市值 / 折算',
    width: 180,
    align: 'right',
    render: (row) => h(Cell, { row, kind: 'value' })
  },
  {
    key: 'profit',
    width: 170,
    align: 'right',
    sorter: true,
    sortOrder: sort.value === 'asc' ? 'ascend' : sort.value === 'desc' ? 'descend' : false,
    title: () =>
      h(
        'button',
        {
          type: 'button',
          class: 'naive-sort-button',
          'data-testid': 'sort-profit',
          'aria-label': `按持仓收益排序，当前${sort.value === 'original' ? '原始顺序' : sort.value === 'asc' ? '升序' : '降序'}`,
          onClick: (event: MouseEvent) => {
            event.stopPropagation()
            toggleSort()
          },
          onVnodeMounted: updateHeaderSort,
          onVnodeUpdated: updateHeaderSort
        },
        '持仓收益'
      ),
    render: (row) => h(Cell, { row, kind: 'profit' })
  },
  {
    key: 'actions',
    title: '操作',
    width: 100,
    fixed: 'right',
    render: (row) =>
      h(
        NButton,
        {
          text: true,
          type: 'primary',
          'aria-label': `编辑交易 ${row.symbol}`,
          'data-testid': `edit-row-${row.id}`,
          onClick: () => openTrade(row)
        },
        { default: () => '编辑交易' }
      )
  }
])

async function submitTrade() {
  if (submit()) return
  await nextTick()
  document.querySelector<HTMLElement>('.naive-dialog input[aria-invalid="true"]')?.focus()
}
</script>

<template>
  <NConfigProvider
    :locale="zhCN"
    :date-locale="dateZhCN"
    :theme-overrides="theme"
    preflight-style-disabled
  >
    <main class="comparison-page naive-page">
      <nav class="comparison-links" aria-label="组件方案对照">
        <a href="/">对照说明</a>
        <a href="/element/">Element Plus</a>
        <a href="/naive/" aria-current="page">Naive UI</a>
        <a href="/nuxt/">Nuxt UI</a>
      </nav>

      <header class="page-heading">
        <div>
          <h1>持仓与交易</h1>
          <p>Naive UI 2.45.3 · 同一份合成数据，仅用于组件对照</p>
        </div>
        <NButton type="primary" data-testid="open-trade" @click="openTrade()">录入演示交易</NButton>
      </header>

      <section class="filters" aria-label="持仓筛选">
        <div class="filter-field filter-search">
          <label for="naive-search">标的代码或名称</label>
          <NInput
            v-model:value="query.keyword"
            clearable
            placeholder="搜索标的"
            :input-props="inputAttrs('naive-search', 'search-input')"
          />
        </div>
        <div class="filter-field">
          <label id="naive-account-label">账户</label>
          <NSelect
            v-model:value="query.account"
            v-select-label="'naive-account-label'"
            :options="accounts"
            data-testid="account-filter"
          />
        </div>
        <div class="filter-field filter-date">
          <label for="naive-start">交易日期起</label>
          <NDatePicker
            v-model:formatted-value="startDate"
            v-date-field="{ id: 'naive-start', testId: 'start-date' }"
            value-format="yyyy-MM-dd"
            format="yyyy-MM-dd"
            type="date"
            clearable
            placeholder="开始日期"
          />
        </div>
        <div class="filter-field filter-date">
          <label for="naive-end">交易日期止</label>
          <NDatePicker
            v-model:formatted-value="endDate"
            v-date-field="{ id: 'naive-end', testId: 'end-date' }"
            value-format="yyyy-MM-dd"
            format="yyyy-MM-dd"
            type="date"
            clearable
            placeholder="结束日期"
          />
        </div>
        <div class="filter-field">
          <label id="naive-count-label">样本数量</label>
          <NSelect
            v-model:value="count"
            v-select-label="'naive-count-label'"
            :options="sampleCounts"
            data-testid="sample-count"
          />
        </div>
        <NButton data-testid="reset-filters" @click="reset">重置</NButton>
      </section>

      <div class="summary-line">
        <span data-testid="result-count" aria-live="polite"
          >显示 {{ rows.length }} / {{ count }} 条持仓</span
        >
        <span>金额保留原币；未知值显示 —，收益排序时置于末尾。</span>
      </div>
      <section class="table-region" aria-label="持仓明细">
        <NDataTable
          :columns="columns"
          :data="rows"
          :row-key="(row: SampleRow) => row.id"
          :scroll-x="996"
          :bordered="false"
          :single-line="true"
          remote
          flex-height
          style="height: 560px"
          data-testid="holding-table"
          @update:sorter="updateSort"
        />
      </section>

      <div v-if="saved" class="saved-receipt" role="status">
        已保存演示交易：{{ saved.symbol }} · {{ accountName(saved.account) }} ·
        {{ formatDate(saved.date) }} · 数量 {{ formatQuantity(saved.quantity) }} · 价格
        {{ formatPrice(saved.price) }}
      </div>
      <p class="page-footnote">同一标的可展开账户信息；右侧编辑操作在横向滚动时保持可见。</p>
    </main>

    <NModal
      v-model:show="open"
      preset="card"
      title="录入演示交易"
      class="naive-dialog"
      style="width: 560px; max-width: calc(100vw - 32px)"
      :mask-closable="false"
      data-testid="trade-dialog"
    >
      <div class="demo-notice">填写完整后保存，结果仅记录在当前演示页。</div>
      <NForm
        :model="draft"
        label-placement="top"
        :show-require-mark="false"
        @submit.prevent="submitTrade"
      >
        <div class="dialog-fields">
          <NFormItem
            label="标的代码"
            :label-props="{ for: 'naive-trade-symbol' }"
            :feedback="errors.symbol"
            :validation-status="errors.symbol ? 'error' : undefined"
          >
            <NInput
              v-model:value="draft.symbol"
              :input-props="inputAttrs('naive-trade-symbol', 'trade-symbol', !!errors.symbol)"
            />
          </NFormItem>
          <NFormItem
            label="账户"
            :label-props="{ id: 'naive-trade-account-label' }"
            :feedback="errors.account"
            :validation-status="errors.account ? 'error' : undefined"
          >
            <NSelect
              v-model:value="draft.account"
              v-select-label="'naive-trade-account-label'"
              :options="tradeAccounts"
              data-testid="trade-account"
            />
          </NFormItem>
          <NFormItem
            label="交易日期"
            :label-props="{ for: 'naive-trade-date' }"
            :feedback="errors.date"
            :validation-status="errors.date ? 'error' : undefined"
          >
            <NDatePicker
              v-model:formatted-value="tradeDate"
              v-date-field="{
                id: 'naive-trade-date',
                testId: 'trade-date',
                invalid: !!errors.date
              }"
              value-format="yyyy-MM-dd"
              format="yyyy-MM-dd"
              type="date"
              clearable
            />
          </NFormItem>
          <NFormItem
            label="数量"
            :label-props="{ for: 'naive-trade-quantity' }"
            :feedback="errors.quantity"
            :validation-status="errors.quantity ? 'error' : undefined"
          >
            <NInputNumber
              v-model:value="draft.quantity"
              :show-button="false"
              placeholder="输入数量"
              :input-props="inputAttrs('naive-trade-quantity', 'trade-quantity', !!errors.quantity)"
            />
          </NFormItem>
          <NFormItem
            label="价格"
            :label-props="{ for: 'naive-trade-price' }"
            :feedback="errors.price"
            :validation-status="errors.price ? 'error' : undefined"
          >
            <NInputNumber
              v-model:value="draft.price"
              :show-button="false"
              placeholder="保留实际价格精度"
              :input-props="inputAttrs('naive-trade-price', 'trade-price', !!errors.price)"
            />
          </NFormItem>
        </div>
        <div class="dialog-actions">
          <NButton data-testid="cancel-trade" @click="open = false">取消</NButton>
          <NButton type="primary" attr-type="submit" data-testid="submit-trade"
            >保存演示交易</NButton
          >
        </div>
      </NForm>
    </NModal>
  </NConfigProvider>
</template>
