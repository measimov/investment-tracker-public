<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage, type FormInstance } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import api from '@/api'
import SecuritySelect from '@/components/SecuritySelect.vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { formatDate, formatDateTime, formatNumber } from '@/utils/helpers'
import {
  type AccountRow,
  type DialogState,
  type SnapshotRow,
  accountLabelIn,
  accountName,
  currencyOptions,
  makeRemover,
  marketOptions,
  monthEnd
} from './shared'

const props = defineProps<{
  snapshots: SnapshotRow[]
  accounts: AccountRow[]
  loading: boolean
  reload: () => Promise<unknown>
}>()

const isMobileView = useMediaQuery('(max-width: 640px)')
const accountLabel = (id: unknown) => accountLabelIn(props.accounts, id)

interface SnapshotCashRowInput {
  currency: string
  amount: number
}

interface SnapshotPositionRowInput {
  symbol: string
  market: string
  quantity: number
  currency?: string | null
  [key: string]: unknown
}

const snapshotDialog = reactive<DialogState>({ visible: false, id: null, saving: false })
const snapshotFormRef = ref<FormInstance>()
const snapshotForm = reactive<{
  broker_account_id?: number | null
  snapshot_date?: string
  source_filename?: string
  cashRows: SnapshotCashRowInput[]
  positionRows: SnapshotPositionRowInput[]
  notes?: string
}>({ cashRows: [], positionRows: [] })

const snapshotRules = {
  broker_account_id: [{ required: true, message: '请选择账户', trigger: 'change' }],
  snapshot_date: [{ required: true, message: '请选择日期', trigger: 'change' }]
}

const diffDialog = reactive<{ visible: boolean; row: SnapshotRow | null }>({
  visible: false,
  row: null
})
const comparingSnapshotId = ref<number | null>(null)

const DIFF_ITEM_LABELS: Record<string, string> = {
  MATCH: '一致',
  QUANTITY_MISMATCH: '数量差',
  MISSING_IN_SYSTEM: '系统缺记录',
  MISSING_IN_SNAPSHOT: '快照缺记录'
}

const diffItemLabel = (status: string) => DIFF_ITEM_LABELS[status] || status
const diffItemTag = (status: string) => (status === 'MATCH' ? 'success' : 'danger')

function openDiffDialog(row: SnapshotRow) {
  diffDialog.row = row
  diffDialog.visible = true
}

async function compareSnapshot(row: SnapshotRow) {
  comparingSnapshotId.value = row.id
  try {
    const response = await api.compareReconciliationSnapshot(row.id)
    ElMessage.success(
      response.data.status === 'MATCHED'
        ? snapshotStatusLabel(response.data)
        : '比对发现差异，点击状态查看明细'
    )
    await props.reload()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '比对失败'))
  } finally {
    comparingSnapshotId.value = null
  }
}

function resetSnapshotForm(row: Partial<SnapshotRow> = {}) {
  const cashBalances = normalizeJson(row.cash_balances || row.reported_cash, {}) as Record<
    string,
    unknown
  >
  const positions = normalizeJson(row.positions || row.reported_positions, [])
  Object.assign(snapshotForm, {
    broker_account_id: row.broker_account_id || row.account_id || props.accounts[0]?.id || null,
    snapshot_date: (row.snapshot_date || monthEnd()).slice(0, 10),
    source_filename: row.source_filename || '',
    cashRows: Object.entries(cashBalances).length
      ? Object.entries(cashBalances).map(([currency, amount]) => ({
          currency,
          amount: Number(amount)
        }))
      : [{ currency: 'CNY', amount: 0 }],
    positionRows: Array.isArray(positions)
      ? positions.map((item: SnapshotPositionRowInput) => ({
          ...item,
          quantity: Number(item.quantity)
        }))
      : [],
    notes: row.notes || ''
  })
}

function addCashRow() {
  const used = new Set(snapshotForm.cashRows.map((item) => item.currency))
  const currency = currencyOptions.find((item) => !used.has(item)) || 'CNY'
  snapshotForm.cashRows.push({ currency, amount: 0 })
}

function addPositionRow() {
  snapshotForm.positionRows.push({
    symbol: '',
    market: '',
    quantity: 0,
    currency: null
  })
}

function openSnapshotDialog(row?: SnapshotRow) {
  snapshotDialog.id = row?.id || null
  resetSnapshotForm(row)
  snapshotDialog.visible = true
}

async function saveSnapshot() {
  if (!(await snapshotFormRef.value?.validate().catch(() => false))) return
  const cashRows = snapshotForm.cashRows.filter((item) => item.currency)
  if (new Set(cashRows.map((item) => item.currency)).size !== cashRows.length) {
    ElMessage.warning('同一币种只能填写一次现金余额')
    return
  }
  const incompletePosition = snapshotForm.positionRows.some(
    (item) => (item.symbol || item.market) && !(item.symbol && item.market)
  )
  if (incompletePosition) {
    ElMessage.warning('请补全持仓的证券代码和市场，或移除该行')
    return
  }
  snapshotDialog.saving = true
  try {
    const payload = {
      broker_account_id: snapshotForm.broker_account_id,
      snapshot_date: snapshotForm.snapshot_date,
      source_filename: snapshotForm.source_filename || null,
      cash_balances: Object.fromEntries(
        cashRows.map((item) => [item.currency, Number(item.amount || 0)])
      ),
      positions: snapshotForm.positionRows
        .filter((item) => item.symbol && item.market)
        .map((item) => ({
          symbol: item.symbol.trim(),
          market: item.market,
          quantity: Number(item.quantity || 0),
          currency: item.currency || null
        })),
      notes: snapshotForm.notes
    }
    if (snapshotDialog.id) await api.updateReconciliationSnapshot(snapshotDialog.id, payload)
    else await api.createReconciliationSnapshot(payload)
    ElMessage.success(snapshotDialog.id ? '核对记录已更新' : '核对记录已新增')
    snapshotDialog.visible = false
    await props.reload()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '核对记录保存失败'))
  } finally {
    snapshotDialog.saving = false
  }
}

const removeSnapshot = makeRemover<SnapshotRow>({
  title: '删除核对记录',
  message: '确认删除这条月末核对记录？',
  request: (row) => api.deleteReconciliationSnapshot(row.id),
  successMessage: '核对记录已删除',
  failureMessage: '核对记录删除失败',
  reload: () => props.reload()
})

const snapshotStatusLabel = (row: SnapshotRow | null | undefined) => {
  const status = String(row?.status || '').toUpperCase()
  // 分范围对账单不比对现金，绿色语义限定为"持仓一致"，避免误读为整体对账完成
  if (status === 'MATCHED' && row?.statement_scope) return '持仓一致'
  return (
    (
      {
        PENDING: '待比对',
        MATCHED: '比对一致',
        MISMATCHED: '有差异'
      } as Record<string, string>
    )[status] ||
    row?.status ||
    '待比对'
  )
}
const snapshotStatusTag = (status: string | undefined) => {
  const value = String(status).toUpperCase()
  if (value === 'MATCHED') return 'success'
  if (value === 'MISMATCHED') return 'danger'
  return 'warning'
}

// 后端 JSON 字段可能以字符串形式返回，解析失败回退默认值；调用方按各自形状收窄
const normalizeJson = (value: unknown, fallback: unknown): unknown => {
  if (value == null) return fallback
  if (typeof value === 'string') {
    try {
      return JSON.parse(value)
    } catch {
      return fallback
    }
  }
  return value
}
const jsonSummary = (value: unknown) => {
  const data = normalizeJson(value, {})
  if (!data || Array.isArray(data) || typeof data !== 'object') return '-'
  const entries = Object.entries(data as Record<string, number | string>)
  if (!entries.length) return '-'
  return entries.map(([currency, amount]) => `${currency} ${formatNumber(amount)}`).join(' · ')
}
const positionSummary = (value: unknown) => {
  const data = normalizeJson(value, [])
  if (Array.isArray(data)) return data.length ? `${data.length} 个标的` : '-'
  if (data && typeof data === 'object') return `${Object.keys(data).length} 个标的`
  return '-'
}
</script>

<template>
  <div>
    <div class="section-toolbar">
      <div>
        <h2>月末核对</h2>
        <p>记录“是否和券商对得上”，不在这里重建复杂会计账本。</p>
      </div>
      <el-button
        type="primary"
        :icon="Plus"
        :disabled="!accounts.length"
        @click="openSnapshotDialog()"
      >
        新增核对
      </el-button>
    </div>

    <div v-if="!isMobileView" class="responsive-table desktop-data-table">
      <el-table :data="snapshots" v-loading="loading" stripe row-key="id">
        <template #empty>
          <el-empty description="暂无月末核对记录" :image-size="88" />
        </template>
        <el-table-column prop="snapshot_date" label="核对日期" width="125">
          <template #default="{ row }">{{ formatDate(row.snapshot_date) }}</template>
        </el-table-column>
        <el-table-column label="账户" min-width="180">
          <template #default="{ row }">{{
            accountLabel(row.broker_account_id || row.account_id)
          }}</template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag
              :type="snapshotStatusTag(row.status)"
              size="small"
              :class="{ 'diff-tag-clickable': row.diff_detail }"
              @click="row.diff_detail && openDiffDialog(row)"
            >
              {{ snapshotStatusLabel(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="现金摘要" min-width="180">
          <template #default="{ row }">{{
            jsonSummary(row.cash_balances || row.reported_cash)
          }}</template>
        </el-table-column>
        <el-table-column label="持仓摘要" min-width="150">
          <template #default="{ row }">{{
            positionSummary(row.positions || row.reported_positions)
          }}</template>
        </el-table-column>
        <el-table-column
          prop="source_filename"
          label="来源文件"
          min-width="180"
          show-overflow-tooltip
        />
        <el-table-column prop="notes" label="备注" min-width="200" show-overflow-tooltip />
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button text :loading="comparingSnapshotId === row.id" @click="compareSnapshot(row)"
              >重新比对</el-button
            >
            <template v-if="!row.import_batch_id">
              <el-button type="primary" text @click="openSnapshotDialog(row)">编辑</el-button>
              <el-button type="danger" text @click="removeSnapshot(row)">删除</el-button>
            </template>
            <el-tag v-else type="info" size="small">导入生成 · 只读</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-else v-loading="loading" class="mobile-card-list">
      <el-empty v-if="!snapshots.length" description="暂无月末核对记录" :image-size="88" />
      <article
        v-for="row in snapshots"
        :key="row.id"
        class="mobile-card"
        data-testid="snapshot-card"
      >
        <div class="mobile-card-head">
          <div class="mobile-card-title">
            <span class="mobile-card-symbol">{{ formatDate(row.snapshot_date) }}</span>
            <span class="mobile-card-name">
              {{ accountLabel(row.broker_account_id || row.account_id) }}
            </span>
          </div>
          <div class="mobile-card-tags">
            <el-tag
              :type="snapshotStatusTag(row.status)"
              size="small"
              :class="{ 'diff-tag-clickable': row.diff_detail }"
              @click="row.diff_detail && openDiffDialog(row)"
            >
              {{ snapshotStatusLabel(row) }}
            </el-tag>
          </div>
        </div>

        <div class="mobile-card-meta">
          <span>现金 {{ jsonSummary(row.cash_balances || row.reported_cash) }}</span>
          <span>持仓 {{ positionSummary(row.positions || row.reported_positions) }}</span>
          <span v-if="row.source_filename">{{ row.source_filename }}</span>
          <span v-if="row.notes">{{ row.notes }}</span>
        </div>

        <div class="mobile-card-actions">
          <el-button
            size="small"
            text
            :loading="comparingSnapshotId === row.id"
            @click="compareSnapshot(row)"
          >
            重新比对
          </el-button>
          <template v-if="!row.import_batch_id">
            <el-button type="primary" size="small" text @click="openSnapshotDialog(row)">
              编辑
            </el-button>
            <el-button type="danger" size="small" text @click="removeSnapshot(row)">
              删除
            </el-button>
          </template>
          <el-tag v-else type="info" size="small">导入生成 · 只读</el-tag>
        </div>
      </article>
    </div>

    <el-dialog
      v-model="snapshotDialog.visible"
      :title="snapshotDialog.id ? '编辑月末核对' : '新增月末核对'"
      width="720px"
    >
      <el-alert
        title="根据券商月结单核对后，记录报表余额、核对状态和差异说明。"
        type="info"
        :closable="false"
        show-icon
        class="dialog-alert"
      />
      <el-form
        ref="snapshotFormRef"
        :model="snapshotForm"
        :rules="snapshotRules"
        label-width="100px"
      >
        <el-form-item label="账户" prop="broker_account_id">
          <el-select v-model="snapshotForm.broker_account_id" placeholder="选择账户">
            <el-option
              v-for="account in accounts"
              :key="account.id"
              :label="accountName(account)"
              :value="account.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="核对日期" prop="snapshot_date">
          <el-date-picker
            v-model="snapshotForm.snapshot_date"
            type="date"
            value-format="YYYY-MM-DD"
          />
        </el-form-item>
        <el-form-item label="来源文件">
          <el-input v-model="snapshotForm.source_filename" placeholder="例如：2026-06 月结单.pdf" />
        </el-form-item>
        <el-form-item label="现金余额">
          <div class="repeatable-fields">
            <div
              v-for="(item, index) in snapshotForm.cashRows"
              :key="`cash-${index}`"
              class="repeatable-row cash-row"
            >
              <el-select v-model="item.currency" placeholder="币种">
                <el-option
                  v-for="currency in currencyOptions"
                  :key="currency"
                  :label="currency"
                  :value="currency"
                />
              </el-select>
              <el-input-number
                v-model="item.amount"
                :min="0"
                :precision="2"
                controls-position="right"
                placeholder="报表余额"
              />
              <el-button
                type="danger"
                text
                :disabled="snapshotForm.cashRows.length === 1"
                @click="snapshotForm.cashRows.splice(index, 1)"
              >
                移除
              </el-button>
            </div>
            <el-button plain :icon="Plus" @click="addCashRow">添加币种</el-button>
          </div>
        </el-form-item>
        <el-form-item label="持仓数量">
          <div class="repeatable-fields">
            <div
              v-for="(item, index) in snapshotForm.positionRows"
              :key="`position-${index}`"
              class="repeatable-row position-row"
            >
              <SecuritySelect
                v-model="item.symbol"
                :resolve="false"
                placeholder="证券代码"
                @select="item.market = $event.market"
              />
              <el-select v-model="item.market" placeholder="市场">
                <el-option
                  v-for="market in marketOptions"
                  :key="market"
                  :label="market"
                  :value="market"
                />
              </el-select>
              <el-input-number
                v-model="item.quantity"
                :min="0"
                :precision="8"
                controls-position="right"
                placeholder="数量"
              />
              <el-select v-model="item.currency" clearable placeholder="币种">
                <el-option
                  v-for="currency in currencyOptions"
                  :key="currency"
                  :label="currency"
                  :value="currency"
                />
              </el-select>
              <el-button type="danger" text @click="snapshotForm.positionRows.splice(index, 1)">
                移除
              </el-button>
            </div>
            <el-button plain :icon="Plus" @click="addPositionRow">添加持仓</el-button>
          </div>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="snapshotForm.notes"
            type="textarea"
            :rows="3"
            placeholder="记录差异原因或凭证位置"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="snapshotDialog.visible = false">取消</el-button>
          <el-button type="primary" :loading="snapshotDialog.saving" @click="saveSnapshot"
            >保存</el-button
          >
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="diffDialog.visible" title="对账比对详情" width="720px">
      <template v-if="diffDialog.row">
        <div class="diff-meta">
          <el-tag :type="snapshotStatusTag(diffDialog.row.status)" size="small">
            {{ snapshotStatusLabel(diffDialog.row) }}
          </el-tag>
          <span>快照日 {{ formatDate(diffDialog.row.snapshot_date) }}</span>
          <span v-if="diffDialog.row.compared_at"
            >比对于 {{ formatDateTime(diffDialog.row.compared_at) }}</span
          >
        </div>

        <h4>持仓比对</h4>
        <el-table :data="diffDialog.row.diff_detail?.positions || []" size="small" stripe>
          <template #empty><el-empty description="无持仓数据" :image-size="88" /></template>
          <el-table-column prop="symbol" label="代码" width="110" />
          <el-table-column prop="market" label="市场" width="90" />
          <el-table-column label="快照数量" width="110" align="right">
            <template #default="{ row }">{{ row.snapshot_quantity ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="系统数量" width="110" align="right">
            <template #default="{ row }">{{ row.system_quantity ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="结果" min-width="120">
            <template #default="{ row }">
              <el-tag :type="diffItemTag(row.status)" size="small">
                {{ diffItemLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>

        <h4>现金比对</h4>
        <el-alert
          v-if="diffDialog.row.diff_detail?.summary?.cash_compared === false"
          type="info"
          :closable="false"
          title="分范围对账单的现金余额只属于该报表范围，不与账户级推导现金比对。"
        />
        <el-table v-else :data="diffDialog.row.diff_detail?.cash || []" size="small" stripe>
          <template #empty><el-empty description="无现金数据" :image-size="88" /></template>
          <el-table-column prop="currency" label="币种" width="90" />
          <el-table-column prop="snapshot_balance" label="快照余额" width="130" align="right" />
          <el-table-column prop="derived_balance" label="推导余额" width="130" align="right" />
          <el-table-column label="结果" min-width="110">
            <template #default="{ row }">
              <el-tag :type="row.status === 'MATCH' ? 'success' : 'warning'" size="small">
                {{ row.status === 'MATCH' ? '一致' : '差异' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>

        <template v-if="(diffDialog.row.diff_detail?.replay_inconsistent || []).length">
          <h4>账户归属矛盾</h4>
          <ul class="diff-notes">
            <li
              v-for="item in diffDialog.row.diff_detail?.replay_inconsistent || []"
              :key="`${item.symbol}-${item.market}`"
            >
              {{ item.symbol }}（{{ item.market }}）：{{ item.reason }}
            </li>
          </ul>
        </template>

        <el-collapse>
          <el-collapse-item title="比对口径说明">
            <ul class="diff-notes">
              <li
                v-for="(note, index) in diffDialog.row.diff_detail?.methodology_notes || []"
                :key="index"
              >
                {{ note }}
              </li>
            </ul>
          </el-collapse-item>
        </el-collapse>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.repeatable-fields {
  display: grid;
  gap: 10px;
  width: 100%;
}

.repeatable-fields > .el-button {
  justify-self: start;
}

.repeatable-row {
  display: grid;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px;
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-radius-inner);
  background: var(--app-surface-muted);
}

.repeatable-row :deep(.el-select),
.repeatable-row :deep(.el-input-number) {
  width: 100%;
}

.cash-row {
  grid-template-columns: 110px minmax(160px, 1fr) auto;
}

.position-row {
  grid-template-columns: minmax(120px, 1fr) 115px minmax(150px, 1fr) 100px auto;
}

.dialog-alert {
  margin-bottom: 20px;
}

.diff-tag-clickable {
  cursor: pointer;
}

.diff-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
  color: var(--app-text-muted);
  font-size: 13px;
}

.diff-notes {
  margin: 4px 0;
  padding-left: 18px;
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.7;
}

@media (max-width: 640px) {
  .cash-row,
  .position-row {
    grid-template-columns: 1fr;
  }

  .repeatable-row > .el-button {
    width: 100%;
  }
}
</style>
