<script setup lang="ts">
/**
 * 可搜索的标的选择器（跨页复用：交易/自选/公司行动/规则/对账/筛选）。
 *
 * 选 el-autocomplete 而非 el-select remote allow-create：v-model 就是输入文本，
 * 自由文本零成本（新加坡股/加密货币/漏网 B 股仍要手输），E2E 的 fill/placeholder
 * 选择器不变；`@select(item)` 拿到整行（value-key 只决定显示文本，同代码不同市场是
 * 两行）。allow-create 的自由文本 blur 即丢、远端刷新即消失、值必须标量——全踩雷。
 *
 * 三种事件对应三种表单补丁（见 utils/securities.ts）：
 * - `select(item)`：明确选中候选 → 调用方完整替换 symbol/market/name/currency
 * - `free-text({symbol, lastPicked})`：blur / Enter 提交了非候选文本
 * - `resolved(result)`：free-text 后（且 market 已知）后端按需解析回来 → 只填空
 *
 * 外部 set（openEdit / reset）经 `watch(modelValue, sync)` 识别：清掉 lastPicked、
 * 不触发解析。attrs（data-testid / @clear / @keyup.enter / size）透传给 el-autocomplete。
 */
import { ref, watch } from 'vue'
import { useAliveGuard } from '@/composables/useAliveGuard'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { useSecuritySearch } from '@/composables/useSecuritySearch'
import { normalizeSymbolInput } from '@/utils/securities'
import type { SecurityResolveResponse, SecuritySearchItem } from '@/types'

defineOptions({ inheritAttrs: false })

const props = withDefaults(
  defineProps<{
    modelValue: string
    /** 当前表单的市场：free-text 后按需解析用；restrictMarket 时也限定检索 */
    market?: string | null
    restrictMarket?: boolean
    /** 手输未收录代码时是否向后端按需解析名称/币种（筛选框关掉） */
    resolve?: boolean
    disabled?: boolean
    clearable?: boolean
    placeholder?: string
    limit?: number
  }>(),
  {
    market: null,
    restrictMarket: false,
    resolve: true,
    disabled: false,
    clearable: true,
    placeholder: '输入代码/名称/拼音检索；也可直接输入新标的',
    limit: 20
  }
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  select: [item: SecuritySearchItem]
  'free-text': [payload: { symbol: string; lastPicked: SecuritySearchItem | null }]
  resolved: [result: SecurityResolveResponse]
}>()

const { isUnmounted } = useAliveGuard()
const isMobile = useMediaQuery('(max-width: 640px)')
const { fetchSuggestions, resolve } = useSecuritySearch({
  isUnmounted,
  market: () => (props.restrictMarket ? props.market : null),
  limit: props.limit
})

const lastPicked = ref<SecuritySearchItem | null>(null)
let lastCommitted = normalizeSymbolInput(props.modelValue)
// 最近一次由本组件发出的值：props 是父组件重渲染时才更新（异步），不能用同步标记
// 区分"自己发出的回声"与"外部赋值"，只能按值比对（归一化后，父层把 txkg 规范成 TXKG 也算回声）
let lastEmitted: string | null = null

function onInput(value: string | number) {
  lastEmitted = String(value)
  emit('update:modelValue', lastEmitted)
}

watch(
  () => props.modelValue,
  (value) => {
    if (lastEmitted !== null && normalizeSymbolInput(value) === normalizeSymbolInput(lastEmitted)) {
      return
    }
    // 外部赋值（编辑回填 / 重置）：不是用户在换标的
    lastPicked.value = null
    lastCommitted = normalizeSymbolInput(value)
    lastEmitted = null
  }
)

function isSearchItem(
  payload: Record<string, unknown>
): payload is Record<string, unknown> & SecuritySearchItem {
  return typeof payload.symbol === 'string' && typeof payload.market === 'string'
}

function onSelect(payload: Record<string, unknown>) {
  if (isSearchItem(payload)) {
    lastPicked.value = payload
    lastCommitted = normalizeSymbolInput(payload.symbol)
    emit('select', payload)
    return
  }
  // select-when-unmatched：Enter 时没有高亮候选 → { value }
  commitFreeText(String(payload.value ?? props.modelValue))
}

function onChange(value: string | number) {
  commitFreeText(String(value))
}

function commitFreeText(raw: string) {
  const symbol = normalizeSymbolInput(raw)
  if (!symbol || symbol === lastCommitted) return
  lastCommitted = symbol
  if (lastPicked.value && symbol === normalizeSymbolInput(lastPicked.value.symbol)) return
  emit('free-text', { symbol, lastPicked: lastPicked.value })
  lastPicked.value = null
  void resolveIfPossible(symbol)
}

async function resolveIfPossible(symbol: string) {
  if (!props.resolve || !props.market) return
  const result = await resolve(symbol, props.market)
  // 用户在等待期间又改了输入 → 结果作废
  if (result && normalizeSymbolInput(props.modelValue) === symbol) emit('resolved', result)
}

// "先打代码再选市场"（自选流程）：市场一确定就补一次解析
watch(
  () => props.market,
  (market) => {
    const symbol = normalizeSymbolInput(props.modelValue)
    if (market && symbol && !lastPicked.value) void resolveIfPossible(symbol)
  }
)

function typeTag(item: { security_type?: string | null }): string | null {
  const type = item.security_type
  if (!type || type === 'stock' || type === 'unknown') return null
  return type.toUpperCase()
}
</script>

<template>
  <el-autocomplete
    :model-value="modelValue"
    v-bind="$attrs"
    :fetch-suggestions="fetchSuggestions"
    :debounce="200"
    :trigger-on-focus="true"
    :select-when-unmatched="true"
    :fit-input-width="isMobile"
    value-key="symbol"
    :placeholder="placeholder"
    :disabled="disabled"
    :clearable="clearable"
    popper-class="security-select-popper"
    class="security-select"
    @update:model-value="onInput"
    @select="onSelect"
    @change="onChange"
  >
    <template #default="{ item }">
      <div class="security-option">
        <span class="security-option__symbol">{{ item.symbol }}</span>
        <span class="security-option__name">{{ item.name || item.name_en || '—' }}</span>
        <el-tag size="small" effect="plain" class="security-option__tag">{{ item.market }}</el-tag>
        <el-tag
          v-if="typeTag(item)"
          size="small"
          type="info"
          effect="plain"
          class="security-option__tag"
        >
          {{ typeTag(item) }}
        </el-tag>
        <el-tag
          v-if="item.origins?.includes('holding')"
          size="small"
          type="success"
          effect="plain"
          class="security-option__tag"
        >
          持仓
        </el-tag>
        <el-tag
          v-else-if="item.origins?.includes('watchlist')"
          size="small"
          type="warning"
          effect="plain"
          class="security-option__tag"
        >
          自选
        </el-tag>
        <el-tag
          v-if="item.list_status === 'delisted'"
          size="small"
          type="danger"
          effect="plain"
          class="security-option__tag"
        >
          已退市
        </el-tag>
        <span v-if="!isMobile && item.name && item.name_en" class="security-option__en">
          {{ item.name_en }}
        </span>
      </div>
    </template>
  </el-autocomplete>
</template>

<style scoped>
.security-select {
  width: 100%;
}
.security-option {
  display: flex;
  align-items: center;
  gap: 8px;
  line-height: 1.4;
  padding: 2px 0;
}
.security-option__symbol {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  min-width: 56px;
}
.security-option__name {
  flex: 0 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.security-option__tag {
  flex: none;
}
.security-option__en {
  margin-left: auto;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 40%;
}
</style>
