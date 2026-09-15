<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import api from '@/api'
import SecuritySelect from '@/components/SecuritySelect.vue'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { formatDateTime } from '@/utils/helpers'
import { inferCurrency } from '@/utils/securities'
import type { SecuritySearchItem } from '@/types'
import { type SecurityRuleRow, currencyOptions, makeRemover, marketOptions } from './shared'

// 特例规则 tab 完全自足（数据+筛选+弹窗都在本组件）：页头汇总卡不消费
// 规则数据，父组件只经 defineExpose 的 reload 参与整页刷新。
const securityRules = ref<SecurityRuleRow[]>([])
const loading = ref(false)

// 六类特例规则：一个表单承载全部字段，按 rule_type 动态显示与校验
const RULE_TYPE_LABELS: Record<string, string> = {
  EXCLUDE: '排除标的',
  CASH_MANAGEMENT: '现金管理标的',
  RELISTING: '转板映射',
  NAME_OVERRIDE: '名称覆盖',
  PRICE_GAP_EXEMPTION: '行情缺口豁免',
  CMB_CASH_BUSINESS: '招商现金业务'
}
const ruleTypeOptions = Object.entries(RULE_TYPE_LABELS).map(([value, label]) => ({ value, label }))
const RULE_TYPE_HINTS: Record<string, string> = {
  EXCLUDE: '导入只归档不入账，对账比对双侧忽略该标的（高影响：现金/收益都不再计入）',
  CASH_MANAGEMENT: '并非排除：该标的的"产品红利发放"按利息（INTEREST）入账，而不是股息',
  RELISTING: '旧标的退市转新市场重新上市，导入时自动生成转换交易',
  NAME_OVERRIDE: '行情源查不到名称时使用的手工显示名',
  PRICE_GAP_EXEMPTION: '该区间行情永久缺失（停牌-摘牌等），历史同步跳过且不计失败',
  CMB_CASH_BUSINESS: '招商对账单业务名 → 现金事件类型的入账口径'
}
const ruleTypeHint = (type: string) => RULE_TYPE_HINTS[type] || ''
const ruleTypeLabel = (type: string) => RULE_TYPE_LABELS[type] || type
const ruleTypeTag = (type: string) =>
  (
    ({
      EXCLUDE: 'danger',
      CASH_MANAGEMENT: 'warning',
      RELISTING: 'primary',
      NAME_OVERRIDE: 'success'
    }) as Record<string, 'danger' | 'warning' | 'primary' | 'success'>
  )[type] || 'info'

// CMB 业务映射可选事件类型：不含 FX_IN/FX_OUT（IBKR 外汇兑换专用，
// CMB 方向校验不认识，后端 CMB_ALLOWED_EVENT_TYPES 同步拒绝）
const cmbEventTypeOptions = [
  { label: '入金', value: 'DEPOSIT' },
  { label: '出金', value: 'WITHDRAWAL' },
  { label: '利息', value: 'INTEREST' },
  { label: '费用', value: 'FEE' },
  { label: '税费', value: 'TAX' },
  { label: '转入', value: 'TRANSFER_IN' },
  { label: '转出', value: 'TRANSFER_OUT' },
  { label: '其他', value: 'OTHER' }
]

const ruleFilters = reactive<{ ruleType: string }>({ ruleType: '' })

// 转板目标选中候选：新市场/新币种一起定（名称覆盖字段不动，那是人工口径）
function onNewSymbolSelected(item: SecuritySearchItem) {
  ruleForm.new_symbol = item.symbol
  ruleForm.new_market = item.market
  ruleForm.new_currency = item.currency || inferCurrency(item.market, item.symbol)
}
const ruleDialog = reactive({ visible: false, saving: false })
const ruleFormRef = ref<FormInstance>()
const ruleForm = reactive({
  rule_type: 'EXCLUDE',
  symbol: '',
  market: 'A股',
  note: '',
  // RELISTING
  old_currency: '',
  new_symbol: '',
  new_market: '',
  new_currency: '',
  // RELISTING / NAME_OVERRIDE
  name: '',
  // PRICE_GAP_EXEMPTION
  start_date: '',
  end_date: '',
  // CMB_CASH_BUSINESS
  event_type: ''
})
const ruleRules = computed<FormRules>(() => {
  const isCmb = ruleForm.rule_type === 'CMB_CASH_BUSINESS'
  const rules: FormRules = {
    rule_type: [{ required: true, message: '请选择规则类型', trigger: 'change' }],
    symbol: [
      { required: true, message: isCmb ? '请输入业务名' : '请输入证券代码', trigger: 'blur' }
    ]
  }
  if (!isCmb) rules.market = [{ required: true, message: '请选择市场', trigger: 'change' }]
  if (ruleForm.rule_type === 'RELISTING') {
    rules.old_currency = [{ required: true, message: '请选择旧币种', trigger: 'change' }]
    rules.new_symbol = [{ required: true, message: '请输入新代码', trigger: 'blur' }]
    rules.new_market = [{ required: true, message: '请选择新市场', trigger: 'change' }]
    rules.new_currency = [{ required: true, message: '请选择新币种', trigger: 'change' }]
  }
  if (ruleForm.rule_type === 'NAME_OVERRIDE')
    rules.name = [{ required: true, message: '请输入覆盖名称', trigger: 'blur' }]
  if (ruleForm.rule_type === 'PRICE_GAP_EXEMPTION')
    rules.start_date = [{ required: true, message: '请选择开始日期', trigger: 'change' }]
  if (isCmb) rules.event_type = [{ required: true, message: '请选择事件类型', trigger: 'change' }]
  return rules
})

// 筛选切换是竞态高发区：慢的旧响应不得覆盖新筛选的结果——捕获请求
// 令牌，只接纳仍是最新请求的响应（含 loading 归位与错误提示）
let rulesRequestToken = 0
async function loadSecurityRules() {
  rulesRequestToken += 1
  const token = rulesRequestToken
  const requestedType = ruleFilters.ruleType
  loading.value = true
  try {
    const response = await api.getSecurityRules(
      requestedType ? { rule_type: requestedType } : undefined
    )
    if (token !== rulesRequestToken) return
    securityRules.value = response.data
  } catch (error) {
    if (token !== rulesRequestToken) return
    ElMessage.error(getApiErrorMessage(error, '特例规则加载失败'))
  } finally {
    if (token === rulesRequestToken) loading.value = false
  }
}

// 摘要列：把 payload 压成一行可读文本，按类型各取要点
function ruleSummary(row: SecurityRuleRow): string {
  const payload = row.payload || {}
  switch (row.rule_type) {
    case 'RELISTING':
      return `→ ${payload.new_symbol} ${payload.new_market} ${payload.new_currency}`
    case 'NAME_OVERRIDE':
      return String(payload.name ?? '—')
    case 'PRICE_GAP_EXEMPTION':
      return `${payload.start_date} ~ ${payload.end_date || '至今'}`
    case 'CMB_CASH_BUSINESS':
      return `→ ${payload.event_type}`
    default:
      return '—'
  }
}

function openRuleDialog() {
  Object.assign(ruleForm, {
    rule_type: ruleFilters.ruleType || 'EXCLUDE',
    symbol: '',
    market: 'A股',
    note: '',
    old_currency: '',
    new_symbol: '',
    new_market: '',
    new_currency: '',
    name: '',
    start_date: '',
    end_date: '',
    event_type: ''
  })
  ruleDialog.visible = true
}

function buildRulePayload(): Record<string, unknown> | null {
  switch (ruleForm.rule_type) {
    case 'RELISTING': {
      const payload: Record<string, unknown> = {
        new_symbol: ruleForm.new_symbol.trim(),
        new_market: ruleForm.new_market,
        new_currency: ruleForm.new_currency,
        old_currency: ruleForm.old_currency
      }
      if (ruleForm.name.trim()) payload.name = ruleForm.name.trim()
      return payload
    }
    case 'NAME_OVERRIDE':
      return { name: ruleForm.name.trim() }
    case 'PRICE_GAP_EXEMPTION':
      return { start_date: ruleForm.start_date, end_date: ruleForm.end_date || null }
    case 'CMB_CASH_BUSINESS':
      return { event_type: ruleForm.event_type }
    default:
      // EXCLUDE / CASH_MANAGEMENT 不携带 payload
      return null
  }
}

async function saveRule() {
  if (!(await ruleFormRef.value?.validate().catch(() => false))) return
  ruleDialog.saving = true
  try {
    await api.createSecurityRule({
      rule_type: ruleForm.rule_type,
      symbol: ruleForm.symbol.trim(),
      market: ruleForm.rule_type === 'CMB_CASH_BUSINESS' ? null : ruleForm.market,
      payload: buildRulePayload(),
      note: ruleForm.note.trim() || null
    })
    ElMessage.success('特例规则已创建，导入、对账与行情将按规则处理')
    ruleDialog.visible = false
    await loadSecurityRules()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '特例规则保存失败'))
  } finally {
    ruleDialog.saving = false
  }
}

const removeRule = makeRemover<SecurityRuleRow>({
  title: '删除特例规则',
  // EXCLUDE 删除后果与旧排除清单一致：后续导入会重新入账，保留原警示
  message: (row) =>
    row.rule_type === 'EXCLUDE'
      ? `移出排除清单后，${row.symbol} 在后续导入时会重新入账。确认移出？`
      : `确认删除「${ruleTypeLabel(row.rule_type)}」规则 ${row.symbol}？删除后导入与统计不再应用该规则。`,
  request: (row) => api.deleteSecurityRule(row.id),
  successMessage: '特例规则已删除',
  failureMessage: '特例规则删除失败',
  reload: () => loadSecurityRules()
})

defineExpose({ reload: loadSecurityRules })
</script>

<template>
  <div>
    <div class="section-toolbar">
      <div>
        <h2>账本特例规则</h2>
        <p>
          六类规则：排除标的＝导入只归档不入账、对账双侧忽略；现金管理标的＝其"产品红利发放"按利息入账（并非排除）；转板映射、名称覆盖、行情缺口豁免、招商现金业务用于修正导入与行情口径。
        </p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openRuleDialog">新增规则</el-button>
    </div>

    <el-form :inline="true" class="compact-filter">
      <el-form-item label="规则类型">
        <el-select
          v-model="ruleFilters.ruleType"
          clearable
          placeholder="全部类型"
          @change="loadSecurityRules"
        >
          <el-option
            v-for="item in ruleTypeOptions"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </el-form-item>
    </el-form>

    <div class="responsive-table">
      <el-table :data="securityRules" v-loading="loading" stripe row-key="id">
        <template #empty>
          <el-empty description="暂无特例规则" :image-size="88" />
        </template>
        <el-table-column label="类型" width="130">
          <template #default="{ row }">
            <el-tag :type="ruleTypeTag(row.rule_type)" size="small">
              {{ ruleTypeLabel(row.rule_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="symbol" label="标的/业务名" min-width="140" />
        <el-table-column label="市场" width="110">
          <template #default="{ row }">{{ row.market || '—' }}</template>
        </el-table-column>
        <el-table-column label="摘要" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">{{ ruleSummary(row) }}</template>
        </el-table-column>
        <el-table-column prop="note" label="备注" min-width="180" show-overflow-tooltip />
        <el-table-column label="创建时间" min-width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button type="danger" text @click="removeRule(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="ruleDialog.visible" title="新增特例规则" width="560px">
      <el-form ref="ruleFormRef" :model="ruleForm" :rules="ruleRules" label-width="100px">
        <el-form-item label="规则类型" prop="rule_type">
          <el-select v-model="ruleForm.rule_type">
            <el-option
              v-for="item in ruleTypeOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
          <div class="rule-type-hint">{{ ruleTypeHint(ruleForm.rule_type) }}</div>
        </el-form-item>

        <el-form-item
          :label="ruleForm.rule_type === 'CMB_CASH_BUSINESS' ? '业务名' : '代码'"
          prop="symbol"
        >
          <!-- 业务名是招商对账单文案，不是证券代码：保留普通输入 -->
          <el-input
            v-if="ruleForm.rule_type === 'CMB_CASH_BUSINESS'"
            v-model="ruleForm.symbol"
            placeholder="如 招现宝收益"
          />
          <SecuritySelect
            v-else
            v-model="ruleForm.symbol"
            :resolve="false"
            placeholder="如 511880"
            @select="ruleForm.market = $event.market"
          />
        </el-form-item>
        <el-form-item v-if="ruleForm.rule_type !== 'CMB_CASH_BUSINESS'" label="市场" prop="market">
          <el-select v-model="ruleForm.market">
            <el-option v-for="m in marketOptions" :key="m" :label="m" :value="m" />
          </el-select>
        </el-form-item>

        <template v-if="ruleForm.rule_type === 'RELISTING'">
          <el-form-item label="旧币种" prop="old_currency">
            <el-select v-model="ruleForm.old_currency">
              <el-option v-for="c in currencyOptions" :key="c" :label="c" :value="c" />
            </el-select>
          </el-form-item>
          <el-form-item label="新代码" prop="new_symbol">
            <SecuritySelect
              v-model="ruleForm.new_symbol"
              :resolve="false"
              placeholder="如 PCT"
              @select="onNewSymbolSelected"
            />
          </el-form-item>
          <el-form-item label="新市场" prop="new_market">
            <el-select v-model="ruleForm.new_market">
              <el-option v-for="m in marketOptions" :key="m" :label="m" :value="m" />
            </el-select>
          </el-form-item>
          <el-form-item label="新币种" prop="new_currency">
            <el-select v-model="ruleForm.new_currency">
              <el-option v-for="c in currencyOptions" :key="c" :label="c" :value="c" />
            </el-select>
          </el-form-item>
          <el-form-item label="名称">
            <el-input v-model="ruleForm.name" placeholder="转板后标的名称，如 柏能集团" />
          </el-form-item>
        </template>

        <el-form-item v-if="ruleForm.rule_type === 'NAME_OVERRIDE'" label="名称" prop="name">
          <el-input v-model="ruleForm.name" placeholder="覆盖显示的标的名称" />
        </el-form-item>

        <template v-if="ruleForm.rule_type === 'PRICE_GAP_EXEMPTION'">
          <el-form-item label="开始日期" prop="start_date">
            <el-date-picker
              v-model="ruleForm.start_date"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="缺口开始日"
            />
          </el-form-item>
          <el-form-item label="结束日期">
            <el-date-picker
              v-model="ruleForm.end_date"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="留空表示至今"
            />
          </el-form-item>
        </template>

        <el-form-item
          v-if="ruleForm.rule_type === 'CMB_CASH_BUSINESS'"
          label="事件类型"
          prop="event_type"
        >
          <el-select v-model="ruleForm.event_type">
            <el-option
              v-for="item in cmbEventTypeOptions"
              :key="item.value"
              :label="`${item.label}（${item.value}）`"
              :value="item.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="备注">
          <el-input v-model="ruleForm.note" placeholder="如 货币基金，专注股票投资回报" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ruleDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="ruleDialog.saving" @click="saveRule">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.rule-type-hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
</style>
