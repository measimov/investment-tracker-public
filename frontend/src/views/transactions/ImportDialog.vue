<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { UploadFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { getApiErrorMessage } from '@/utils/apiErrors'
import type { BrokerAccount, BrokerImportResult, SuspectedDuplicateSample } from '@/types'
import { downloadFile, todayLocalISODate } from '@/utils/helpers'
import { brokerAccountLabel } from './shared'

// 预览/导入响应以后端 BrokerImportResult 为准（生成类型；此前手写副本已漂移：
// statement_scope 的 null、诊断报告新增字段都没跟上）
type BrokerPreview = BrokerImportResult

const props = defineProps<{ brokerAccounts: BrokerAccount[]; brokerAccountsLoading: boolean }>()

// imported：入账数据已变（含"未达完整入账标准"的部分入账），壳层失效缓存并
// 刷新列表；force 对应原实现两个分支的 loadTransactions 口径
const emit = defineEmits<{ imported: [options: { force: boolean }] }>()

const visible = ref(false)
const importing = ref(false)
const uploadFile = ref<File | null>(null)
const importMode = ref('standard')
const brokerPreview = ref<BrokerPreview | null>(null)
const importBrokerAccountId = ref<number | null>(null)
// 疑似重复（#190）：用户勾选后确认为真实成交的 row_hash，随预览/导入一起回传；
// 换文件、换账户、换模式都要清空——确认是对某一份文件里某几行的决定
const confirmedSuspectedHashes = ref<string[]>([])
const selectedSuspectedRows = ref<SuspectedDuplicateSample[]>([])
const suspectedSamples = computed<SuspectedDuplicateSample[]>(
  () => brokerPreview.value?.suspected_duplicate_samples ?? []
)
const suspectedHeldCount = computed(() => brokerPreview.value?.suspected_duplicate_rows ?? 0)
const suspectedTotalCount = computed(() => suspectedSamples.value.length)
function handleSuspectedSelection(rows: SuspectedDuplicateSample[]) {
  selectedSuspectedRows.value = rows
}
async function confirmSelectedSuspected() {
  const hashes = selectedSuspectedRows.value.map((row) => row.row_hash)
  if (!hashes.length) {
    ElMessage.warning('请先勾选要确认为真实成交的行')
    return
  }
  confirmedSuspectedHashes.value = Array.from(
    new Set([...confirmedSuspectedHashes.value, ...hashes])
  )
  await handleImportPreview()
}

const importAccept = computed(() => {
  // IBKR：规范格式为 trade_history.xlsx；Activity CSV 保留供历史回填
  if (importMode.value === 'ibkr') return '.csv,.xlsx'
  if (importMode.value === 'eastmoney') return '.pdf'
  if (importMode.value === 'cmb') return '.pdf'
  return '.csv,.xlsx,.xls'
})
const isBrokerImportMode = computed(() => ['cmb', 'ibkr', 'eastmoney'].includes(importMode.value))
const requiresAccountScopedPreview = computed(() =>
  ['cmb', 'ibkr', 'eastmoney'].includes(importMode.value)
)
const brokerPreviewHasBlockingErrors = computed(
  () =>
    Boolean(brokerPreview.value?.errors?.length) ||
    brokerPreview.value?.batch_status === 'FAILED' ||
    brokerPreview.value?.reconciliation_status === 'MISMATCHED'
)
// 后端把 errors/warnings 截到 50 条，这里再截到 8 条。没有"共 N 条"的话，
// "看到 8 条"和"一共 8 条"在界面上完全无法区分——排查会建立在错误的前提上。
const PREVIEW_MESSAGE_LIMIT = 8
const messageCountSuffix = (messages?: string[], total?: number) => {
  const shown = Math.min(messages?.length || 0, PREVIEW_MESSAGE_LIMIT)
  // `||` 而非 `??`：后端漏填时 total 是 0，用 `??` 会在列出 8 条错误的同时
  // 显示"共 0 条"——比不显示更糟
  const all = total || messages?.length || 0
  return all > shown ? `（已显示 ${shown} / 共 ${all} 条）` : `（共 ${all} 条）`
}
const diagnosticsText = computed(() =>
  brokerPreview.value?.diagnostics ? JSON.stringify(brokerPreview.value.diagnostics, null, 2) : ''
)
const handleCopyDiagnostics = async () => {
  try {
    await navigator.clipboard.writeText(diagnosticsText.value)
    ElMessage.success('诊断报告已复制')
  } catch {
    // 非 HTTPS 环境下 clipboard API 不可用——报障者常常正是这种部署
    ElMessage.warning('无法自动复制，请手动选中下方文本，或改用「下载 JSON」')
  }
}
const handleDownloadDiagnostics = () => {
  downloadFile(
    new Blob([diagnosticsText.value], { type: 'application/json' }),
    `cmb-import-diagnostics-${todayLocalISODate()}.json`
  )
}
const brokerImportAccountOptions = computed(() => {
  const keywordMap: Record<string, string[]> = {
    cmb: ['招商'],
    ibkr: ['IBKR', 'INTERACTIVE'],
    eastmoney: ['东方']
  }
  const keywords = keywordMap[importMode.value]
  if (!keywords) return []
  return props.brokerAccounts.filter(
    (account) =>
      account.is_active !== false &&
      keywords.some((keyword) =>
        String(account.broker || '')
          .toUpperCase()
          .includes(keyword)
      )
  )
})

function open() {
  visible.value = true
}

function resetConfirmedSuspected() {
  confirmedSuspectedHashes.value = []
  selectedSuspectedRows.value = []
}

function handleFileChange(file: { raw?: File }) {
  uploadFile.value = file.raw ?? null
  brokerPreview.value = null
  resetConfirmedSuspected()
}

function handleFileRemove() {
  uploadFile.value = null
  brokerPreview.value = null
  resetConfirmedSuspected()
}

watch(importMode, () => {
  uploadFile.value = null
  brokerPreview.value = null
  importBrokerAccountId.value = null
  resetConfirmedSuspected()
})

watch(importBrokerAccountId, () => {
  brokerPreview.value = null
  resetConfirmedSuspected()
})

async function handleImportPreview() {
  if (!uploadFile.value) {
    ElMessage.warning('请选择文件')
    return
  }
  if (!isBrokerImportMode.value) {
    return
  }
  if (requiresAccountScopedPreview.value && !importBrokerAccountId.value) {
    ElMessage.warning('请选择匹配账户后再预览')
    return
  }

  importing.value = true
  try {
    let response
    if (importMode.value === 'ibkr') {
      response = await api.previewIbkrActivity(uploadFile.value, importBrokerAccountId.value)
    } else if (importMode.value === 'eastmoney') {
      response = await api.previewEastmoneyStatement(uploadFile.value, importBrokerAccountId.value)
    } else {
      response = await api.previewCmbFundFlows(
        uploadFile.value,
        importBrokerAccountId.value,
        confirmedSuspectedHashes.value
      )
    }
    brokerPreview.value = response.data
    selectedSuspectedRows.value = []
    ElMessage.success('预览完成')
  } catch (error) {
    ElMessage.error('预览失败：' + getApiErrorMessage(error))
  } finally {
    importing.value = false
  }
}

async function handleImport() {
  if (!uploadFile.value) {
    ElMessage.warning('请选择文件')
    return
  }
  if (isBrokerImportMode.value && !importBrokerAccountId.value) {
    ElMessage.warning('正式券商导入必须选择匹配账户')
    return
  }
  importing.value = true
  try {
    let response
    let successMessage
    // 券商导入结果单独持有：response 是跨模式联合类型，isBrokerImportMode
    // 标志收窄不了它；三个券商分支内的赋值经流程分析拿到精确类型
    let brokerResult: BrokerImportResult | null = null
    if (importMode.value === 'cmb') {
      response = await api.importCmbFundFlows(
        uploadFile.value,
        importBrokerAccountId.value,
        confirmedSuspectedHashes.value
      )
      brokerResult = response.data
      successMessage =
        `导入交易 ${response.data.imported_transactions} 条，` +
        `公司行动 ${response.data.imported_corporate_actions} 条，` +
        `红利税调整 ${response.data.imported_tax_adjustments} 条，` +
        `现金收益 ${response.data.imported_cash_events || 0} 条，` +
        `跳过重复 ${response.data.duplicate_rows} 条`
    } else if (importMode.value === 'ibkr') {
      response = await api.importIbkrActivity(uploadFile.value, importBrokerAccountId.value)
      brokerResult = response.data
      successMessage =
        `导入交易 ${response.data.imported_transactions} 条，` +
        `公司行动 ${response.data.imported_corporate_actions} 条，` +
        `预扣税调整 ${response.data.imported_tax_adjustments} 条，` +
        `跳过重复 ${response.data.duplicate_rows} 条`
    } else if (importMode.value === 'eastmoney') {
      response = await api.importEastmoneyStatement(uploadFile.value, importBrokerAccountId.value)
      brokerResult = response.data
      successMessage =
        `导入交易 ${response.data.imported_transactions} 条，` +
        `公司行动 ${response.data.imported_corporate_actions} 条，` +
        `红利税调整 ${response.data.imported_tax_adjustments} 条，` +
        `组合费 ${response.data.imported_cash_events || 0} 条，` +
        `跳过重复 ${response.data.duplicate_rows} 条`
    } else if (importMode.value === 'corporate_actions') {
      const isExcel =
        uploadFile.value.name.endsWith('.xlsx') || uploadFile.value.name.endsWith('.xls')
      response = isExcel
        ? await api.importCorporateActionsExcel(uploadFile.value, importBrokerAccountId.value)
        : await api.importCorporateActionsCSV(uploadFile.value, importBrokerAccountId.value)
      successMessage = response.data.message
    } else {
      const isExcel =
        uploadFile.value.name.endsWith('.xlsx') || uploadFile.value.name.endsWith('.xls')
      response = isExcel
        ? await api.importExcel(uploadFile.value, importBrokerAccountId.value)
        : await api.importCSV(uploadFile.value, importBrokerAccountId.value)
      successMessage = response.data.message
    }

    if (isBrokerImportMode.value && brokerResult) {
      const hasIssues =
        brokerResult.batch_status === 'PARTIAL' ||
        brokerResult.batch_status === 'FAILED' ||
        brokerResult.reconciliation_status === 'MISMATCHED' ||
        Boolean(brokerResult.errors?.length)
      if (hasIssues) {
        brokerPreview.value = brokerResult
        ElMessage.warning('导入未达到完整入账标准，请按页面提示处理后再导入')
        emit('imported', { force: true })
        return
      }
    }

    ElMessage.success(successMessage)
    visible.value = false
    uploadFile.value = null
    brokerPreview.value = null
    importBrokerAccountId.value = null
    resetConfirmedSuspected()
    emit('imported', { force: false })
  } catch (error) {
    ElMessage.error('导入失败：' + getApiErrorMessage(error))
  } finally {
    importing.value = false
  }
}

defineExpose({ open })
</script>

<template>
  <el-dialog v-model="visible" title="导入数据" width="720px">
    <el-tabs v-model="importMode" class="import-tabs">
      <el-tab-pane label="标准交易文件" name="standard" />
      <el-tab-pane label="标准公司行动文件" name="corporate_actions" />
      <el-tab-pane label="招商证券对账单" name="cmb" />
      <el-tab-pane label="IBKR 活动报表" name="ibkr" />
      <el-tab-pane label="东方财富对账单" name="eastmoney" />
    </el-tabs>
    <div v-if="isBrokerImportMode" class="import-account-field">
      <span>导入到</span>
      <el-select
        v-model="importBrokerAccountId"
        clearable
        placeholder="选择匹配的券商账户"
        :loading="brokerAccountsLoading"
      >
        <el-option
          v-for="account in brokerImportAccountOptions"
          :key="account.id"
          :label="brokerAccountLabel(account)"
          :value="account.id"
        />
      </el-select>
      <small v-if="importMode === 'eastmoney'">
        预览和导入都按所选账户去重；普通股票与港股通两份对账单必须选择同一个账户
      </small>
      <small v-else-if="importMode === 'cmb'">预览和正式导入都必须选择匹配账户</small>
      <small v-else>预览和正式导入都必须选择匹配账户</small>
    </div>
    <div v-else class="import-account-field">
      <span>归属账户</span>
      <el-select
        v-model="importBrokerAccountId"
        clearable
        placeholder="不指定账户（默认）"
        :loading="brokerAccountsLoading"
      >
        <el-option
          v-for="account in brokerAccounts"
          :key="account.id"
          :label="brokerAccountLabel(account)"
          :value="account.id"
        />
      </el-select>
      <small>
        可选：把导入的记录归属到某个券商账户（如 HSBC 手工整理的标准 CSV）；不选则落"未指定账户"
      </small>
    </div>
    <el-upload
      drag
      :auto-upload="false"
      :on-change="handleFileChange"
      :on-remove="handleFileRemove"
      :limit="1"
      :accept="importAccept"
    >
      <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
      <div class="el-upload__text">拖拽文件到此处或 <em>点击上传</em></div>
      <template #tip>
        <div v-if="importMode === 'standard'" class="el-upload__tip">
          支持 CSV 或 Excel 文件，需包含以下列：symbol, market, transaction_type, quantity, price,
          transaction_date
        </div>
        <div v-else-if="importMode === 'corporate_actions'" class="el-upload__tip">
          支持 CSV 或 Excel 文件，需包含以下列：symbol, market, action_type, ex_date；可选
          total_dividend, net_dividend, split_ratio, shares_received 等公司行动字段
        </div>
        <div v-else class="el-upload__tip">
          <span v-if="importMode === 'cmb'">
            支持招商证券营业部普通对账单（年度）PDF，含沪港通交易（以 HKD
            记账、按行内推导结算汇率换算费用）；资金流水 Excel 不再作为导入来源
          </span>
          <span v-else-if="importMode === 'ibkr'">
            支持 trade_history.xlsx（规范格式）与 Activity Statement
            CSV（历史回填），导入普通股票/ETF买卖、股息、外国预扣税，以及存款、
            利息、外汇兑换的现金入账（生成只读现金事件）；期权与「调整」跳过但归档
          </span>
          <span v-else>
            支持已解密的东方财富普通股票和港股通 PDF
            对账单；可导入股票、场内基金、港股通成交、红利、红利税和港股通组合费
          </span>
        </div>
      </template>
    </el-upload>

    <el-alert
      v-if="importMode === 'eastmoney'"
      class="preview-alert"
      type="info"
      :closable="false"
      show-icon
      title="请把两份互补对账单分别导入同一账户"
      description="先导入普通股票明细对账单，再导入港股通股票明细对账单；单独一份不能代表东方财富账户的完整交易历史。"
    />

    <div v-if="brokerPreview" class="import-preview">
      <el-descriptions :column="3" border size="small" class="responsive-descriptions">
        <el-descriptions-item label="券商">{{ brokerPreview.broker }}</el-descriptions-item>
        <el-descriptions-item v-if="importMode === 'eastmoney'" label="对账单范围">
          {{ brokerPreview.statement_scope === 'hk_connect' ? '港股通' : '普通股票' }}
        </el-descriptions-item>
        <el-descriptions-item
          v-if="importMode === 'cmb' && brokerPreview.source_account_masks?.length"
          label="单据账户尾号"
        >
          {{ brokerPreview.source_account_masks.join(' / ') }}
        </el-descriptions-item>
        <el-descriptions-item label="总行数">{{ brokerPreview.total_rows }}</el-descriptions-item>
        <el-descriptions-item v-if="importMode === 'cmb'" label="疑似重复">
          {{ brokerPreview.suspected_duplicate_rows || 0 }}
        </el-descriptions-item>
        <el-descriptions-item v-if="brokerPreview.archived_source_rows != null" label="来源归档">
          {{ brokerPreview.archived_source_rows }}
        </el-descriptions-item>
        <el-descriptions-item label="买卖记录">{{
          brokerPreview.eligible_trade_rows
        }}</el-descriptions-item>
        <el-descriptions-item label="股息分红">{{
          brokerPreview.eligible_dividend_rows
        }}</el-descriptions-item>
        <el-descriptions-item label="红利税">{{
          brokerPreview.eligible_tax_rows
        }}</el-descriptions-item>
        <el-descriptions-item label="重复跳过">{{
          brokerPreview.duplicate_rows
        }}</el-descriptions-item>
        <el-descriptions-item label="非买卖跳过">{{
          brokerPreview.skipped_non_trade_rows
        }}</el-descriptions-item>
        <el-descriptions-item label="无效跳过">{{
          brokerPreview.skipped_invalid_rows
        }}</el-descriptions-item>
        <el-descriptions-item
          v-if="importMode === 'cmb' || importMode === 'eastmoney'"
          label="排除清单跳过"
        >
          {{ brokerPreview.skipped_excluded_rows || 0 }}
        </el-descriptions-item>
        <el-descriptions-item v-if="importMode === 'ibkr'" label="期权跳过">{{
          brokerPreview.skipped_option_rows
        }}</el-descriptions-item>
        <el-descriptions-item v-if="importMode === 'ibkr'" label="现金入账">{{
          brokerPreview.eligible_cash_event_rows || 0
        }}</el-descriptions-item>
        <el-descriptions-item v-if="importMode === 'ibkr'" label="外汇入账">{{
          brokerPreview.eligible_fx_rows || 0
        }}</el-descriptions-item>
        <el-descriptions-item v-if="importMode === 'ibkr'" label="外汇跳过">{{
          brokerPreview.skipped_fx_rows
        }}</el-descriptions-item>
        <el-descriptions-item v-if="importMode === 'ibkr'" label="现金类跳过">{{
          brokerPreview.skipped_cash_rows
        }}</el-descriptions-item>
        <el-descriptions-item
          v-if="importMode === 'eastmoney' || importMode === 'cmb'"
          :label="importMode === 'cmb' ? '现金收益' : '港股通组合费'"
        >
          {{ brokerPreview.eligible_cash_rows || 0 }}
        </el-descriptions-item>
        <el-descriptions-item v-if="importMode === 'eastmoney'" label="不支持跳过">{{
          brokerPreview.skipped_unsupported_rows
        }}</el-descriptions-item>
        <el-descriptions-item v-if="importMode === 'eastmoney'" label="范围冲突">{{
          brokerPreview.skipped_conflict_rows || 0
        }}</el-descriptions-item>
        <el-descriptions-item v-if="importMode === 'eastmoney'" label="期末持仓">{{
          brokerPreview.reported_position_count || 0
        }}</el-descriptions-item>
        <el-descriptions-item label="日期范围"
          >{{ brokerPreview.date_start }} ~ {{ brokerPreview.date_end }}</el-descriptions-item
        >
      </el-descriptions>

      <el-alert
        v-if="(brokerPreview.skipped_excluded_rows || 0) > 0"
        class="preview-alert"
        type="info"
        :closable="false"
        show-icon
        :title="`${brokerPreview.skipped_excluded_rows} 条流水命中排除规则，将只归档不入账（账户数据 → 特例规则 可调整）`"
      />
      <el-alert
        v-if="(brokerPreview.duplicate_rows || 0) > 0"
        class="preview-alert"
        type="warning"
        :closable="false"
        show-icon
        :title="`发现 ${brokerPreview.duplicate_rows} 条重复流水，正式导入时会跳过`"
      />
      <el-alert
        v-else
        class="preview-alert"
        type="success"
        :closable="false"
        show-icon
        title="未发现重复的买卖流水"
      />
      <div
        v-if="suspectedTotalCount > 0"
        class="suspected-block"
        data-testid="suspected-duplicates"
      >
        <el-alert
          class="preview-alert"
          type="warning"
          :closable="false"
          show-icon
          :title="
            suspectedHeldCount > 0
              ? `${suspectedHeldCount} 条成交疑似与已入账流水重复（同日/同标的/同数量/同金额，成交价精度不同），本次不入账，待人工确认`
              : `${suspectedTotalCount} 条此前归档的疑似重复成交仍待确认（本次按重复跳过）`
          "
          description="券商新旧导出的成交价小数位不同时，同一笔成交会算出不同的流水指纹。勾选确认为「真实的另一笔成交」后重新预览，再导入即入账；不勾选则保持归档不入账。"
        />
        <el-table
          :data="suspectedSamples"
          size="small"
          row-key="row_hash"
          max-height="280"
          @selection-change="handleSuspectedSelection"
        >
          <el-table-column type="selection" width="40" />
          <el-table-column prop="trade_date" label="日期" width="105" />
          <el-table-column prop="symbol" label="代码" width="90" />
          <el-table-column prop="name" label="名称" min-width="110" show-overflow-tooltip />
          <el-table-column prop="transaction_type" label="方向" width="70" />
          <el-table-column prop="quantity" label="数量" width="100" align="right" />
          <el-table-column prop="amount" label="发生金额" width="120" align="right" />
          <el-table-column label="已入账价格 → 本单价格" min-width="150" align="right">
            <template #default="{ row }">
              {{ row.existing_price ?? '—' }} → {{ row.price }}
            </template>
          </el-table-column>
          <el-table-column prop="row_number" label="行号" width="70" align="right" />
        </el-table>
        <div class="suspected-actions">
          <span class="suspected-hint">
            已显示 {{ suspectedTotalCount }} 条
            <template v-if="confirmedSuspectedHashes.length">
              · 已确认 {{ confirmedSuspectedHashes.length }} 条
            </template>
          </span>
          <el-button
            size="small"
            type="warning"
            plain
            :loading="importing"
            @click="confirmSelectedSuspected"
          >
            确认所选为真实成交并重新预览
          </el-button>
        </div>
      </div>
      <el-alert
        v-if="brokerPreview.warnings?.length"
        class="preview-alert"
        type="warning"
        :closable="false"
        show-icon
        :title="`存在待人工复核的未入账来源，可继续导入并保留审计记录${messageCountSuffix(
          brokerPreview.warnings,
          brokerPreview.warnings_total
        )}`"
        :description="brokerPreview.warnings.slice(0, PREVIEW_MESSAGE_LIMIT).join('；')"
      />
      <el-alert
        v-if="brokerPreview.errors?.length"
        class="preview-alert"
        type="error"
        :closable="false"
        show-icon
        :title="`存在需要先处理的数据问题，当前不允许正式导入${messageCountSuffix(
          brokerPreview.errors,
          brokerPreview.errors_total
        )}`"
        :description="brokerPreview.errors.slice(0, PREVIEW_MESSAGE_LIMIT).join('；')"
      />
      <div v-if="brokerPreview.diagnostics" class="preview-diagnostics">
        <el-collapse>
          <el-collapse-item name="diagnostics">
            <template #title>
              <span>诊断报告（已脱敏，可直接发给维护者）</span>
            </template>
            <p class="preview-diagnostics__note">
              报告只含版式指纹、标签词表与数值的比值/位数：不含金额、数量、价格与证券名称； 文件名与
              PDF 元数据只回传结构分类和不可逆摘要，不含原文。
            </p>
            <div class="preview-diagnostics__actions">
              <el-button size="small" @click="handleCopyDiagnostics">复制</el-button>
              <el-button size="small" @click="handleDownloadDiagnostics">下载 JSON</el-button>
            </div>
            <pre class="preview-diagnostics__body">{{ diagnosticsText }}</pre>
          </el-collapse-item>
        </el-collapse>
      </div>
    </div>
    <template #footer>
      <div class="mobile-dialog-footer">
        <el-button @click="visible = false">取消</el-button>
        <el-button
          v-if="isBrokerImportMode && !brokerPreview"
          type="primary"
          @click="handleImportPreview"
          :loading="importing"
          :disabled="requiresAccountScopedPreview && !importBrokerAccountId"
        >
          预览
        </el-button>
        <el-button
          v-else
          type="primary"
          @click="handleImport"
          :loading="importing"
          :disabled="
            (isBrokerImportMode && !importBrokerAccountId) || brokerPreviewHasBlockingErrors
          "
        >
          导入
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.import-tabs {
  margin-bottom: 12px;
}

.import-account-field {
  display: grid;
  grid-template-columns: auto minmax(220px, 1fr);
  align-items: center;
  gap: 6px 12px;
  margin-bottom: 14px;
  padding: 12px 14px;
  border-radius: var(--app-radius-inner);
  background: var(--app-surface-muted);
}

.import-account-field > span {
  color: var(--app-text-muted);
  font-size: 14px;
  font-weight: 600;
}

.import-account-field > small {
  grid-column: 2;
  color: var(--app-text-soft);
}

.import-preview {
  margin-top: 16px;
}

.suspected-block {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.suspected-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.suspected-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.preview-alert {
  margin-top: 12px;
}

.preview-diagnostics {
  margin-top: 12px;
}

.preview-diagnostics__note {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin: 0 0 8px;
}

.preview-diagnostics__actions {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.preview-diagnostics__body {
  background: var(--el-fill-color-light);
  border-radius: 4px;
  font-size: 12px;
  margin: 0;
  max-height: 320px;
  overflow: auto;
  padding: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}

@media (max-width: 900px) {
  :deep(.el-upload-dragger) {
    width: 100%;
  }

  :deep(.responsive-descriptions .el-descriptions__body) {
    overflow-x: auto;
  }
}

@media (max-width: 640px) {
  .import-account-field {
    grid-template-columns: 1fr;
  }

  .import-account-field > small {
    grid-column: 1;
  }
}
</style>
