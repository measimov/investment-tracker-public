<script setup lang="ts">
import { computed, h, ref } from 'vue'
import {
  NButton,
  NConfigProvider,
  NDataTable,
  NDrawer,
  NDrawerContent,
  NDropdown,
  NInput,
  NInputNumber,
  NModal,
  NSelect,
  zhCN,
  dateZhCN,
  type DataTableColumns,
  type GlobalThemeOverrides
} from 'naive-ui'
import {
  Grid,
  Wallet,
  Sort,
  DataAnalysis,
  Star,
  Document,
  Collection,
  Setting,
  Fold,
  Expand,
  Search,
  Refresh,
  ArrowRight,
  ArrowDown,
  MoreFilled,
  InfoFilled,
  Close,
  Edit,
  ArrowUp,
  Menu
} from '@element-plus/icons-vue'
import {
  formatCurrency,
  formatNumber,
  formatPrice,
  formatQuantity,
  formatPercent,
  formatDate
} from '@/utils/helpers'
import { accounts, positions, estimate, type Row, type Market } from './data'
import Sidebar from './Sidebar.vue'

const theme: GlobalThemeOverrides = {
  common: {
    primaryColor: '#A6533C',
    primaryColorHover: '#8e4531',
    primaryColorPressed: '#783825',
    primaryColorSuppl: '#A6533C',
    textColorBase: '#292722',
    textColor1: '#292722',
    textColor2: '#605d55',
    textColor3: '#777268',
    bodyColor: '#faf9f5',
    borderColor: '#ddd9d0',
    borderRadius: '6px',
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", sans-serif',
    fontSize: '14px',
    heightMedium: '36px'
  },
  DataTable: {
    thColor: '#f8f7f3',
    thTextColor: '#6c675e',
    tdColor: '#fff',
    tdColorHover: '#faf9f5',
    borderColor: '#ece9e2',
    thFontWeight: '400',
    tdPaddingMedium: '17px 16px',
    thPaddingMedium: '13px 16px'
  },
  Button: { fontWeight: '500', textColorText: '#605d55' },
  Input: { color: '#fff', border: '1px solid #ddd9d0' },
  Select: { peers: { InternalSelection: { border: '1px solid #ddd9d0', color: '#fff' } } }
}
const definitions = ref(structuredClone(positions))
const collapsed = ref(false)
const mobileNav = ref(false)
const keyword = ref('')
const account = ref('all')
const market = ref<Market | '全部'>('全部')
const groupBy = ref<'symbol' | 'account'>('symbol')
const sortDesc = ref(true)
const refreshing = ref(false)
const notice = ref('')
let noticeTimer: ReturnType<typeof setTimeout>
function inform(message: string) {
  notice.value = message
  clearTimeout(noticeTimer)
  noticeTimer = setTimeout(() => (notice.value = ''), 3500)
}
const detail = ref<Row | null>(null)
const detailOpen = computed({
  get: () => !!detail.value,
  set: (value) => {
    if (!value) detail.value = null
  }
})
const quote = ref<Row | null>(null)
const quotePrice = ref<number | null>(null)
const quoteError = ref('')
const quoteOpen = computed({
  get: () => !!quote.value,
  set: (value) => {
    if (!value) quote.value = null
  }
})
const expanded = ref<string[]>([])
const rowsInScope = computed(() =>
  definitions.value
    .map((item) => estimate(item, account.value))
    .filter(
      (item) => item.quantity > 0 && (market.value === '全部' || item.market === market.value)
    )
)
const valued = computed(() => rowsInScope.value.filter((row) => row.cny !== null))
const totalValue = computed(() => valued.value.reduce((sum, row) => sum + row.cny!, 0))
const totalCost = computed(() => valued.value.reduce((sum, row) => sum + row.costCny, 0))
const totalProfit = computed(() => totalValue.value - totalCost.value)
const totalRate = computed(() =>
  totalCost.value ? (totalProfit.value / totalCost.value) * 100 : null
)
const missing = computed(() => rowsInScope.value.length - valued.value.length)
const rows = computed(() => {
  const source =
    groupBy.value === 'symbol'
      ? rowsInScope.value
      : rowsInScope.value.flatMap((row) =>
          row.lots.map((lot) => ({ ...estimate(row, lot.account), id: `${row.id}-${lot.account}` }))
        )
  return source
    .filter((row) =>
      `${row.name} ${row.symbol} ${row.industry}`
        .toLowerCase()
        .includes(keyword.value.toLowerCase().trim())
    )
    .sort((a, b) => {
      if (a.cny === null) return b.cny === null ? 0 : 1
      if (b.cny === null) return -1
      return sortDesc.value ? b.cny - a.cny : a.cny - b.cny
    })
})
const marketParts = computed(() =>
  (['港股', 'A股', '美股'] as Market[]).map((name) => ({
    name,
    value: valued.value.filter((row) => row.market === name).reduce((sum, row) => sum + row.cny!, 0)
  }))
)
const signClass = (amount: number | null) =>
  amount === null || amount === 0 ? '' : amount > 0 ? 'is-gain' : 'is-loss'
const money = (amount: number | null, currency: string) =>
  `${amount !== null && amount > 0 ? '+' : ''}${formatCurrency(amount, currency)}`
const oldQuote = (row: Row) => row.date !== '' && row.date < '2026-09-20'
function openQuote(row: Row) {
  quote.value = row
  quotePrice.value = row.price
  quoteError.value = ''
}
function saveQuote() {
  if (quotePrice.value === null || !Number.isFinite(quotePrice.value) || quotePrice.value <= 0) {
    quoteError.value = '请输入大于 0 的价格'
    return
  }
  const target = definitions.value.find((item) => item.symbol === quote.value?.symbol)
  if (target) {
    target.price = quotePrice.value
    target.date = '2026-09-30'
    target.manual = true
  }
  quote.value = null
  inform('演示报价已更新，仅在当前预览中生效')
}
async function refresh() {
  refreshing.value = true
  await new Promise((resolve) => setTimeout(resolve, 650))
  refreshing.value = false
  inform('已展示刷新完成状态；本页使用固定演示行情')
}
function resetFilters() {
  keyword.value = ''
  account.value = 'all'
  market.value = '全部'
}
function navTo(label: string) {
  if (label !== '持仓') inform('本轮视觉预览仅开放持仓页')
  else mobileNav.value = false
}
const icon = (component: typeof MoreFilled) => () => h(component, { class: 'small-icon' })
const columns = computed<DataTableColumns<Row>>(() => [
  {
    type: 'expand',
    width: 30,
    expandable: (row) => row.lots.length > 1,
    renderExpand: (row) =>
      h('div', { class: 'account-breakdown' }, [
        h('strong', '账户明细'),
        ...row.lots.map((lot) =>
          h('div', { class: 'account-line' }, [
            h('span', lot.account),
            h('span', `${formatQuantity(lot.quantity)} 股`),
            h('span', `均价 ${formatPrice(lot.average)} ${row.currency}`)
          ])
        )
      ])
  },
  {
    key: 'name',
    title: '标的',
    minWidth: 250,
    render: (row) =>
      h('div', { class: 'security-cell' }, [
        h(
          'span',
          { class: ['security-avatar', row.tone], 'aria-hidden': 'true' },
          row.symbol === 'AAPL' ? 'A' : row.symbol === 'VOO' ? 'V' : row.name[0]
        ),
        h('div', { class: 'security-copy' }, [
          h('button', { class: 'security-title', onClick: () => (detail.value = row) }, row.name),
          h('div', { class: 'security-meta' }, [
            h('span', row.symbol),
            h('i', '·'),
            h('span', `${row.market} / ${row.industry}`)
          ]),
          row.lots.length > 1
            ? h('span', { class: 'multi-account' }, `${row.lots.length} 个账户`)
            : null
        ])
      ])
  },
  {
    key: 'account',
    title: '账户',
    width: 126,
    render: (row) =>
      row.lots.length > 1
        ? h(
            'button',
            {
              class: 'account-expand-button',
              'aria-expanded': expanded.value.includes(row.id),
              onClick: () => {
                expanded.value = expanded.value.includes(row.id)
                  ? expanded.value.filter((id) => id !== row.id)
                  : [...expanded.value, row.id]
              }
            },
            [`${row.lots.length} 个账户`, h(ArrowDown, { class: 'tiny-icon' })]
          )
        : h('span', { class: 'account-name' }, row.lots[0]?.account)
  },
  {
    key: 'quantity',
    title: '持仓数量 / 均价',
    width: 145,
    align: 'right',
    render: (row) =>
      h('div', { class: 'number-cell' }, [
        h('div', formatQuantity(row.quantity)),
        h('div', { class: 'secondary' }, `${formatPrice(row.average)} ${row.currency}`)
      ])
  },
  {
    key: 'price',
    title: '现价',
    width: 128,
    align: 'right',
    render: (row) =>
      h('div', { class: 'number-cell' }, [
        h(
          'button',
          {
            class: ['price-button', row.price === null ? 'price-missing' : ''],
            'aria-label': `修改${row.name}报价`,
            onClick: () => openQuote(row)
          },
          [formatPrice(row.price), h(Edit, { class: 'price-edit' })]
        ),
        h(
          'div',
          { class: ['secondary', oldQuote(row) ? 'attention-text' : ''] },
          row.price === null
            ? '暂无报价'
            : oldQuote(row)
              ? '09/16 · 手工陈价'
              : row.manual
                ? `${row.currency} · 手工`
                : row.currency
        )
      ])
  },
  {
    key: 'value',
    width: 184,
    align: 'right',
    title: () =>
      h(
        'button',
        {
          class: 'sort-button',
          'aria-label': '切换市值排序',
          onClick: () => (sortDesc.value = !sortDesc.value)
        },
        ['市值 / 折 CNY', h(sortDesc.value ? ArrowDown : ArrowUp, { class: 'tiny-icon' })]
      ),
    render: (row) =>
      h('div', { class: 'number-cell value-cell' }, [
        h('div', formatCurrency(row.value, row.currency)),
        h(
          'div',
          { class: 'secondary' },
          row.currency === 'CNY'
            ? `占比 ${row.cny === null || !totalValue.value ? '—' : formatNumber((row.cny / totalValue.value) * 100, 1) + '%'}`
            : `折 ${formatCurrency(row.cny, 'CNY')}`
        )
      ])
  },
  {
    key: 'profit',
    title: '浮动盈亏 / 收益率',
    width: 190,
    align: 'right',
    render: (row) =>
      h('div', { class: ['number-cell', signClass(row.profit)] }, [
        h('div', money(row.profit, row.currency)),
        h('div', { class: 'profit-secondary' }, [
          h('span', formatPercent(row.rate)),
          row.currency !== 'CNY'
            ? h('span', { class: 'profit-cny' }, `折 ${money(row.profitCny, 'CNY')}`)
            : null
        ])
      ])
  },
  {
    key: 'action',
    width: 50,
    fixed: 'right',
    render: (row) =>
      h(
        NDropdown,
        {
          trigger: 'click',
          options: [
            { label: '查看持仓明细', key: 'detail' },
            { label: '修改报价', key: 'price' }
          ],
          onSelect: (key: string) => (key === 'detail' ? (detail.value = row) : openQuote(row))
        },
        {
          default: () =>
            h(
              NButton,
              {
                quaternary: true,
                circle: true,
                'aria-label': `${row.name}操作`,
                class: 'row-menu'
              },
              { icon: icon(MoreFilled) }
            )
        }
      )
  }
])
</script>

<template>
  <NConfigProvider :theme-overrides="theme" :locale="zhCN" :date-locale="dateZhCN">
    <div
      class="portfolio-shell"
      :class="{ 'sidebar-collapsed': collapsed, 'mobile-nav-open': mobileNav }"
    >
      <aside class="portfolio-sidebar" aria-label="主导航"><Sidebar @navigate="navTo" /></aside>
      <NDrawer v-model:show="mobileNav" placement="left" :width="224" class="navigation-drawer">
        <NDrawerContent :body-content-style="{ padding: '0', height: '100%' }">
          <Sidebar closable @close="mobileNav = false" @navigate="navTo" />
        </NDrawerContent>
      </NDrawer>
      <div class="portfolio-workspace">
        <div class="topbar">
          <div class="topbar-location">
            <button
              class="icon-button desktop-nav-toggle"
              :aria-label="collapsed ? '展开侧栏' : '收起侧栏'"
              @click="collapsed = !collapsed"
            >
              <Expand v-if="collapsed" /><Fold v-else /></button
            ><button
              class="icon-button mobile-nav-toggle"
              aria-label="打开导航"
              @click="mobileNav = true"
            >
              <Menu /></button
            ><span>投资组合</span><ArrowRight class="tiny-icon" /><strong>持仓</strong>
          </div>
          <span class="preview-label"><span />视觉预览 · 虚构数据</span>
        </div>
        <main class="portfolio-main">
          <header class="portfolio-heading">
            <div>
              <p class="eyebrow">我的投资组合</p>
              <h1>持仓</h1>
              <p class="heading-description">查看各市场持仓与持有期表现。</p>
            </div>
            <div class="heading-actions">
              <span class="quote-date">行情日期 <strong>2026/09/30</strong></span
              ><NButton type="primary" :loading="refreshing" @click="refresh"
                ><template #icon><Refresh /></template>刷新行情</NButton
              >
            </div>
          </header>
          <section class="portfolio-overview" aria-label="持仓汇总">
            <div class="overview-primary">
              <div class="metric-label">
                {{ missing ? '已知持仓市值' : '持仓市值' }}<span class="currency-unit">CNY</span>
              </div>
              <div class="hero-value" data-testid="total-value">
                {{ formatCurrency(totalValue, 'CNY') }}
              </div>
              <div class="scope-caption">
                {{ account === 'all' ? '全部账户' : account }}<span>·</span
                >{{ market === '全部' ? '全部市场' : market }}<span>·</span>{{ valued.length }} /
                {{ rowsInScope.length }} 项可估值
              </div>
            </div>
            <div class="overview-metric">
              <div class="metric-label">持仓成本 <span>CNY</span></div>
              <div class="metric-value">{{ formatCurrency(totalCost, 'CNY') }}</div>
              <div class="metric-caption">与市值采用相同估值范围</div>
            </div>
            <div class="overview-metric">
              <div class="metric-label">浮动盈亏 <span>CNY</span></div>
              <div class="metric-value" :class="signClass(totalProfit)">
                {{ money(totalProfit, 'CNY') }}
              </div>
              <div class="metric-caption">按持仓平均成本计算</div>
            </div>
            <div class="overview-metric compact-metric">
              <div class="metric-label">浮动收益率</div>
              <div class="metric-value" :class="signClass(totalProfit)">
                {{ formatPercent(totalRate) }}
              </div>
              <div class="metric-caption">不含汇兑损益</div>
            </div>
          </section>
          <div class="overview-baseline">
            <div class="market-composition">
              <span v-for="part in marketParts.filter((item) => item.value > 0)" :key="part.name"
                ><i
                  :class="
                    part.name === '港股'
                      ? 'market-hk'
                      : part.name === 'A股'
                        ? 'market-cn'
                        : 'market-us'
                  "
                />{{ part.name }}
                <strong
                  >{{
                    totalValue ? formatNumber((part.value / totalValue) * 100, 1) : '—'
                  }}%</strong
                ></span
              >
            </div>
            <button
              v-if="missing"
              class="missing-summary"
              @click="detail = rowsInScope.find((row) => row.cny === null) || null"
            >
              <InfoFilled />{{ missing }} 项缺少行情，未计入汇总<ArrowRight /></button
            ><span v-else class="complete-summary">当前范围均可估值</span>
          </div>
          <section class="holdings-section" aria-label="持仓明细">
            <div class="holdings-section-heading">
              <div class="section-title">
                <h2>持仓明细</h2>
                <span>{{ rowsInScope.length }}</span>
              </div>
              <div class="view-toggle" aria-label="持仓分组">
                <button
                  :class="{ selected: groupBy === 'symbol' }"
                  :aria-pressed="groupBy === 'symbol'"
                  @click="groupBy = 'symbol'"
                >
                  按标的</button
                ><button
                  :class="{ selected: groupBy === 'account' }"
                  :aria-pressed="groupBy === 'account'"
                  @click="groupBy = 'account'"
                >
                  按账户
                </button>
              </div>
            </div>
            <div class="holdings-toolbar">
              <div class="market-tabs" role="group" aria-label="市场筛选">
                <button
                  v-for="item in ['全部', '港股', 'A股', '美股'] as const"
                  :key="item"
                  :class="{ selected: market === item }"
                  :aria-pressed="market === item"
                  @click="market = item"
                >
                  {{ item }}
                </button>
              </div>
              <div class="table-filters">
                <NInput
                  v-model:value="keyword"
                  placeholder="搜索标的、代码"
                  clearable
                  :input-props="{ 'aria-label': '搜索持仓' }"
                  class="holding-search"
                  ><template #prefix><Search class="small-icon" /></template></NInput
                ><NSelect
                  v-model:value="account"
                  :options="accounts"
                  class="account-select"
                  aria-label="账户筛选"
                />
              </div>
            </div>
            <div v-if="keyword" class="search-caption">
              找到 {{ rows.length }} 项；搜索仅筛选明细，汇总范围保持不变。<button
                @click="keyword = ''"
              >
                清除搜索
              </button>
            </div>
            <NDataTable
              v-model:expanded-row-keys="expanded"
              class="holdings-table"
              :columns="columns"
              :data="rows"
              :row-key="(row) => row.id"
              :scroll-x="1103"
              :bordered="false"
              :single-line="true"
              ><template #empty
                ><div class="empty-holdings">
                  没有匹配的持仓<NButton text type="primary" @click="resetFilters"
                    >清除筛选</NButton
                  >
                </div></template
              ></NDataTable
            >
            <div class="mobile-holdings">
              <article v-for="row in rows" :key="row.id" class="holding-card">
                <div class="holding-card-title">
                  <div class="security-cell">
                    <span class="security-avatar" :class="row.tone" aria-hidden="true">{{
                      row.symbol === 'AAPL' ? 'A' : row.symbol === 'VOO' ? 'V' : row.name[0]
                    }}</span>
                    <div>
                      <button class="security-title" @click="detail = row">{{ row.name }}</button>
                      <div class="security-meta">{{ row.symbol }} · {{ row.market }}</div>
                    </div>
                  </div>
                  <button
                    class="icon-button"
                    :aria-label="`查看${row.name}明细`"
                    @click="detail = row"
                  >
                    <ArrowRight />
                  </button>
                </div>
                <div class="holding-card-numbers">
                  <div>
                    <span>市值</span><strong>{{ formatCurrency(row.value, row.currency) }}</strong
                    ><small>{{
                      row.currency === 'CNY'
                        ? `持仓 ${formatQuantity(row.quantity)}`
                        : `折 ${formatCurrency(row.cny, 'CNY')}`
                    }}</small>
                  </div>
                  <div :class="signClass(row.profit)">
                    <span>浮动盈亏</span><strong>{{ money(row.profit, row.currency) }}</strong
                    ><small>{{ formatPercent(row.rate) }}</small>
                  </div>
                </div>
                <div class="holding-card-meta">
                  <span>{{
                    row.lots.length > 1 ? `${row.lots.length} 个账户` : row.lots[0]?.account
                  }}</span
                  ><button class="mobile-price" @click="openQuote(row)">
                    现价 {{ formatPrice(row.price) }}<Edit />
                  </button>
                </div>
                <p v-if="row.price === null || oldQuote(row)" class="holding-card-notice">
                  {{ row.price === null ? '行情缺失 · 未计入汇总' : '手工陈价 · 2026/09/16' }}
                </p>
              </article>
              <div v-if="!rows.length" class="empty-holdings">
                没有匹配的持仓<NButton text type="primary" @click="resetFilters">清除筛选</NButton>
              </div>
            </div>
            <footer class="table-footer">
              <span>{{ rows.length }} 项持仓<span class="footer-dot">·</span>按人民币市值排序</span
              ><span>原币与 CNY 折算并列显示</span>
            </footer>
          </section>
          <div class="accounting-note">
            <InfoFilled />
            <p>
              成本与市值按同一组汇率折算；普通现金股息不调整买入成本。<br /><span
                >此视觉稿仅用于审阅布局与交互；公司名称用于示例，持仓、报价与汇率均为虚构。</span
              >
            </p>
            <a href="/">返回组件对照<ArrowRight /></a>
          </div>
        </main>
      </div>
      <Transition name="notice"
        ><div v-if="notice" class="preview-notice" role="status">{{ notice }}</div></Transition
      >
      <NDrawer v-model:show="detailOpen" :width="430" placement="right" class="position-drawer"
        ><NDrawerContent v-if="detail" closable
          ><template #header><span class="drawer-eyebrow">持仓明细</span></template>
          <div class="detail-heading">
            <span class="security-avatar large" :class="detail.tone">{{ detail.name[0] }}</span>
            <h2>{{ detail.name }}</h2>
            <p>{{ detail.symbol }} · {{ detail.market }} · {{ detail.industry }}</p>
          </div>
          <div class="detail-valuation">
            <span>当前市值</span><strong>{{ formatCurrency(detail.value, detail.currency) }}</strong
            ><small>折 {{ formatCurrency(detail.cny, 'CNY') }}</small>
          </div>
          <dl class="detail-list">
            <div>
              <dt>持仓数量</dt>
              <dd>{{ formatQuantity(detail.quantity) }}</dd>
            </div>
            <div>
              <dt>持仓均价</dt>
              <dd>{{ formatPrice(detail.average) }} {{ detail.currency }}</dd>
            </div>
            <div>
              <dt>现价</dt>
              <dd>{{ formatPrice(detail.price) }} {{ detail.currency }}</dd>
            </div>
            <div>
              <dt>浮动盈亏</dt>
              <dd :class="signClass(detail.profit)">{{ money(detail.profit, detail.currency) }}</dd>
            </div>
            <div>
              <dt>报价日期</dt>
              <dd>{{ formatDate(detail.date) }}</dd>
            </div>
            <div>
              <dt>当前范围占比</dt>
              <dd>
                {{
                  detail.cny === null || !totalValue
                    ? '—'
                    : formatNumber((detail.cny / totalValue) * 100, 2) + '%'
                }}
              </dd>
            </div>
          </dl>
          <h3 class="detail-subheading">账户分布</h3>
          <div v-for="lot in detail.lots" :key="lot.account" class="detail-account">
            <strong>{{ lot.account }}</strong
            ><span>{{ formatQuantity(lot.quantity) }} 股</span
            ><small>持仓均价 {{ formatPrice(lot.average) }} {{ detail.currency }}</small>
          </div>
          <div v-if="detail.price === null || oldQuote(detail)" class="detail-warning">
            {{
              detail.price === null
                ? '暂无行情，成本与市值均未计入汇总。'
                : '当前使用手工陈价，估值可能与实际市场情况不同。'
            }}
          </div>
          <template #footer
            ><NButton @click="detail = null">关闭</NButton></template
          ></NDrawerContent
        ></NDrawer
      >
      <NModal
        v-model:show="quoteOpen"
        preset="card"
        class="quote-modal"
        title="修改报价"
        :mask-closable="false"
        ><template v-if="quote"
          ><p class="quote-description">
            {{ quote.name }} <span>{{ quote.symbol }}</span>
          </p>
          <label class="quote-label">现价（{{ quote.currency }}）</label
          ><NInputNumber
            v-model:value="quotePrice"
            :precision="4"
            :step="0.0001"
            :show-button="false"
            :input-props="{ 'aria-label': '现价' }"
            style="width: 100%"
          />
          <p v-if="quoteError" class="quote-error" role="alert">{{ quoteError }}</p>
          <p class="quote-footnote">仅更新演示数据，不修改真实账本。</p>
          <div class="quote-actions">
            <NButton @click="quote = null">取消</NButton
            ><NButton type="primary" @click="saveQuote">保存报价</NButton>
          </div></template
        ></NModal
      >
    </div>
  </NConfigProvider>
</template>
