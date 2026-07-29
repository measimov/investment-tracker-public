<template>
  <div class="account-data-page">
    <section class="page-intro">
      <div>
        <p class="eyebrow">DATA FOUNDATION</p>
        <h1>账户数据</h1>
        <p>把券商账户、现金活动和月末核对放在一起，先保证数据可信，再看收益。</p>
      </div>
      <el-button :icon="Refresh" :loading="refreshing" @click="refreshAll">刷新数据</el-button>
    </section>

    <div class="summary-grid">
      <div class="summary-item">
        <span>券商账户</span>
        <strong>{{ accounts.length }}</strong>
        <small>{{ activeAccountCount }} 个启用</small>
      </div>
      <div class="summary-item">
        <span>现金事件</span>
        <strong>{{ cashEvents.length }}</strong>
        <small>入金、出金及账户费用</small>
      </div>
      <div class="summary-item">
        <span>最近导入</span>
        <strong class="summary-date">{{ latestBatchDate }}</strong>
        <small>{{ importBatches.length }} 个可追溯批次</small>
      </div>
      <div class="summary-item">
        <span>月末核对</span>
        <strong>{{ reconciledCount }}/{{ snapshots.length }}</strong>
        <small>持仓数量一致；自动快照不核验现金</small>
      </div>
    </div>

    <el-card shadow="never" class="content-card">
      <el-tabs v-model="activeTab" class="data-tabs">
        <el-tab-pane name="accounts">
          <template #label>
            <span class="tab-label"><Wallet />账户</span>
          </template>

          <div class="section-toolbar">
            <div>
              <h2>券商账户</h2>
              <p>为交易以及后续的账户级持仓、收益统计建立明确归属。</p>
            </div>
            <el-button type="primary" :icon="Plus" @click="openAccountDialog()">新增账户</el-button>
          </div>

          <div class="responsive-table">
            <el-table :data="accounts" v-loading="loading.accounts" stripe row-key="id">
              <template #empty>
                <el-empty description="尚未登记券商账户">
                  <el-button type="primary" @click="openAccountDialog()">新增第一个账户</el-button>
                </el-empty>
              </template>
              <el-table-column label="账户" min-width="200">
                <template #default="{ row }">
                  <div class="primary-cell">
                    <strong>{{ accountName(row) }}</strong>
                    <span>{{ row.account_number_masked || '未填写尾号' }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="broker" label="券商" min-width="140">
                <template #default="{ row }">{{ row.broker || row.broker_name || '-' }}</template>
              </el-table-column>
              <el-table-column prop="base_currency" label="基础币种" width="105" />
              <el-table-column prop="is_active" label="状态" width="90">
                <template #default="{ row }">
                  <el-tag :type="row.is_active === false ? 'info' : 'success'" size="small">
                    {{ row.is_active === false ? '停用' : '启用' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="notes" label="备注" min-width="180" show-overflow-tooltip />
              <el-table-column label="操作" width="140" fixed="right">
                <template #default="{ row }">
                  <el-button type="primary" text @click="openAccountDialog(row)">编辑</el-button>
                  <el-button type="danger" text @click="removeAccount(row)">删除空账户</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>

        <el-tab-pane name="cash">
          <template #label>
            <span class="tab-label"><Coin />现金事件</span>
          </template>

          <div class="section-toolbar">
            <div>
              <h2>现金事件</h2>
              <p>保存真实资金变化，为后续校准账户收益；当前收益统计仍是估算口径。</p>
            </div>
            <el-button
              type="primary"
              :icon="Plus"
              :disabled="!accounts.length"
              @click="openCashDialog()"
            >
              新增事件
            </el-button>
          </div>

          <el-form :inline="true" class="compact-filter">
            <el-form-item label="账户">
              <el-select v-model="cashFilters.accountId" clearable placeholder="全部账户">
                <el-option
                  v-for="account in accounts"
                  :key="account.id"
                  :label="accountName(account)"
                  :value="account.id"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="类型">
              <el-select v-model="cashFilters.eventType" clearable placeholder="全部类型">
                <el-option
                  v-for="item in cashTypeOptions"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-select>
            </el-form-item>
          </el-form>

          <div class="responsive-table">
            <el-table :data="filteredCashEvents" v-loading="loading.cash" stripe row-key="id">
              <template #empty>
                <el-empty description="暂无现金事件" />
              </template>
              <el-table-column prop="event_date" label="日期" width="120">
                <template #default="{ row }">{{
                  formatDate(row.event_date || row.occurred_at)
                }}</template>
              </el-table-column>
              <el-table-column label="账户" min-width="170">
                <template #default="{ row }">{{
                  accountLabel(row.broker_account_id || row.account_id)
                }}</template>
              </el-table-column>
              <el-table-column prop="event_type" label="类型" width="110">
                <template #default="{ row }">
                  <el-tag :type="cashTypeTag(row.event_type)" size="small">
                    {{ cashTypeLabel(row.event_type) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="金额" min-width="130" align="right">
                <template #default="{ row }">
                  <strong :class="amountClass(row)">{{ signedAmount(row) }}</strong>
                </template>
              </el-table-column>
              <el-table-column prop="notes" label="备注" min-width="200" show-overflow-tooltip />
              <el-table-column label="操作" width="140" fixed="right">
                <template #default="{ row }">
                  <el-button type="primary" text @click="openCashDialog(row)">编辑</el-button>
                  <el-button type="danger" text @click="removeCashEvent(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>

        <el-tab-pane name="imports">
          <template #label>
            <span class="tab-label"><Files />导入批次</span>
          </template>

          <div class="section-toolbar">
            <div>
              <h2>导入批次</h2>
              <p>这里是只读来源记录；成交文件仍从“交易记录”页面导入。</p>
            </div>
          </div>

          <div class="responsive-table">
            <el-table :data="importBatches" v-loading="loading.batches" stripe row-key="id">
              <template #empty>
                <el-empty description="暂无可追溯的导入批次" />
              </template>
              <el-table-column label="导入时间" min-width="160">
                <template #default="{ row }">{{
                  formatDateTime(row.created_at || row.imported_at)
                }}</template>
              </el-table-column>
              <el-table-column label="账户" min-width="160">
                <template #default="{ row }">{{
                  accountLabel(row.broker_account_id || row.account_id)
                }}</template>
              </el-table-column>
              <el-table-column label="文件" min-width="230" show-overflow-tooltip>
                <template #default="{ row }">
                  <div class="primary-cell">
                    <strong>{{
                      row.source_filename || row.original_filename || '未命名来源'
                    }}</strong>
                    <span>{{ reportPeriod(row) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="结果" min-width="300">
                <template #default="{ row }">
                  {{ row.archived_count ?? 0 }} 来源归档 /
                  {{ row.imported_count ?? row.rows_imported ?? 0 }} 本批入账 /
                  {{ row.duplicate_count ?? 0 }} 已有重复 /
                  {{ row.skipped_count ?? row.rows_skipped ?? 0 }} 未入账
                </template>
              </el-table-column>
              <el-table-column label="状态" width="100">
                <template #default="{ row }">
                  <el-tag :type="batchStatusTag(row.status)" size="small">
                    {{ batchStatusLabel(row.status) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="90" fixed="right">
                <template #default="{ row }">
                  <el-button type="primary" text @click="openBatchDetails(row)">详情</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>

        <el-tab-pane name="reconciliation">
          <template #label>
            <span class="tab-label"><CircleCheck />月末核对</span>
          </template>

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

          <div class="responsive-table">
            <el-table :data="snapshots" v-loading="loading.snapshots" stripe row-key="id">
              <template #empty>
                <el-empty description="暂无月末核对记录" />
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
                  <el-button
                    text
                    :loading="comparingSnapshotId === row.id"
                    @click="compareSnapshot(row)"
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
        </el-tab-pane>
        <el-tab-pane name="exclusions">
          <template #label>
            <span class="tab-label"><Remove />排除清单</span>
          </template>

          <div class="section-toolbar">
            <div>
              <h2>现金管理标的排除清单</h2>
              <p>清单内标的（如货币基金）导入时只归档不入账，月末核对双侧忽略。</p>
            </div>
            <el-button type="primary" :icon="Plus" @click="openExclusionDialog">
              新增排除
            </el-button>
          </div>

          <div class="responsive-table">
            <el-table :data="exclusions" v-loading="loading.exclusions" stripe row-key="id">
              <template #empty>
                <el-empty description="暂无排除标的" />
              </template>
              <el-table-column prop="symbol" label="代码" width="140" />
              <el-table-column prop="market" label="市场" width="120" />
              <el-table-column prop="note" label="备注" min-width="220" show-overflow-tooltip />
              <el-table-column label="创建时间" min-width="170">
                <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="100" fixed="right">
                <template #default="{ row }">
                  <el-button type="danger" text @click="removeExclusion(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <el-dialog
      v-model="accountDialog.visible"
      :title="accountDialog.id ? '编辑券商账户' : '新增券商账户'"
      width="560px"
    >
      <el-form ref="accountFormRef" :model="accountForm" :rules="accountRules" label-width="100px">
        <el-form-item label="账户名称" prop="account_name">
          <el-input v-model="accountForm.account_name" placeholder="例如：IBKR 主账户" />
        </el-form-item>
        <el-form-item label="券商" prop="broker">
          <el-select
            v-model="accountForm.broker"
            filterable
            allow-create
            default-first-option
            placeholder="选择或输入券商"
          >
            <el-option
              v-for="broker in brokerOptions"
              :key="broker"
              :label="broker"
              :value="broker"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="账户尾号">
          <el-input
            v-model="accountForm.account_number_masked"
            placeholder="只保存脱敏标识，例如 ****1234 / ****5678"
          />
          <span class="field-hint">一份对账单有多个股东代码时，请把各尾号都填在这里。</span>
        </el-form-item>
        <el-form-item label="基础币种" prop="base_currency">
          <el-select v-model="accountForm.base_currency">
            <el-option
              v-for="currency in currencyOptions"
              :key="currency"
              :label="currency"
              :value="currency"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="accountForm.is_active" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="accountForm.notes"
            type="textarea"
            :rows="3"
            placeholder="不保存密码、完整账号或报表访问令牌"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="accountDialog.visible = false">取消</el-button>
          <el-button type="primary" :loading="accountDialog.saving" @click="saveAccount"
            >保存</el-button
          >
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="cashDialog.visible"
      :title="cashDialog.id ? '编辑现金事件' : '新增现金事件'"
      width="600px"
    >
      <el-form ref="cashFormRef" :model="cashForm" :rules="cashRules" label-width="100px">
        <el-form-item label="账户" prop="broker_account_id">
          <el-select v-model="cashForm.broker_account_id" placeholder="选择账户">
            <el-option
              v-for="account in accounts"
              :key="account.id"
              :label="accountName(account)"
              :value="account.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="日期" prop="event_date">
          <el-date-picker v-model="cashForm.event_date" type="date" value-format="YYYY-MM-DD" />
        </el-form-item>
        <el-form-item label="类型" prop="event_type">
          <el-select v-model="cashForm.event_type">
            <el-option
              v-for="item in cashTypeOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="金额" prop="amount">
          <el-input-number
            v-model="cashForm.amount"
            :min="0.01"
            :precision="2"
            :step="100"
            controls-position="right"
          />
          <span class="field-hint">金额始终填正数，资金方向由事件类型表达</span>
        </el-form-item>
        <el-form-item label="币种" prop="currency">
          <el-select v-model="cashForm.currency">
            <el-option
              v-for="currency in currencyOptions"
              :key="currency"
              :label="currency"
              :value="currency"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="cashForm.notes"
            type="textarea"
            :rows="3"
            placeholder="例如：由银行账户转入"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="cashDialog.visible = false">取消</el-button>
          <el-button type="primary" :loading="cashDialog.saving" @click="saveCashEvent"
            >保存</el-button
          >
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="snapshotDialog.visible"
      :title="snapshotDialog.id ? '编辑月末核对' : '新增月末核对'"
      width="700px"
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
        label-width="110px"
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
              <el-input v-model="item.symbol" placeholder="证券代码" />
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

    <el-drawer v-model="batchDrawerVisible" title="导入批次详情" size="min(520px, 92%)">
      <el-descriptions v-if="selectedBatch" :column="1" border>
        <el-descriptions-item label="文件">{{
          selectedBatch.source_filename || selectedBatch.original_filename || '-'
        }}</el-descriptions-item>
        <el-descriptions-item label="账户">{{
          accountLabel(selectedBatch.broker_account_id || selectedBatch.account_id)
        }}</el-descriptions-item>
        <el-descriptions-item label="导入时间">{{
          formatDateTime(selectedBatch.created_at || selectedBatch.imported_at)
        }}</el-descriptions-item>
        <el-descriptions-item label="报表区间">{{
          reportPeriod(selectedBatch)
        }}</el-descriptions-item>
        <el-descriptions-item label="来源类型">{{
          selectedBatch.source_type || '-'
        }}</el-descriptions-item>
        <el-descriptions-item label="解析器">
          {{
            [selectedBatch.parser_name, selectedBatch.parser_version].filter(Boolean).join(' ') ||
            '-'
          }}
        </el-descriptions-item>
        <el-descriptions-item label="文件哈希">
          <code class="hash-value">{{
            selectedBatch.source_sha256 || selectedBatch.file_sha256 || '-'
          }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="总行数">{{
          selectedBatch.row_count ?? selectedBatch.total_rows ?? '-'
        }}</el-descriptions-item>
        <el-descriptions-item label="来源归档">{{
          selectedBatch.archived_count ?? 0
        }}</el-descriptions-item>
        <el-descriptions-item label="本批入账">{{
          selectedBatch.imported_count ?? 0
        }}</el-descriptions-item>
        <el-descriptions-item label="已有重复">{{
          selectedBatch.duplicate_count ?? 0
        }}</el-descriptions-item>
        <el-descriptions-item label="未入账">{{
          selectedBatch.skipped_count ?? 0
        }}</el-descriptions-item>
        <el-descriptions-item label="错误">{{
          selectedBatch.error_count ?? 0
        }}</el-descriptions-item>
        <el-descriptions-item label="错误信息">{{
          selectedBatch.error_message || '-'
        }}</el-descriptions-item>
      </el-descriptions>
    </el-drawer>

    <el-dialog v-model="exclusionDialog.visible" title="新增排除标的" width="420px">
      <el-form
        ref="exclusionFormRef"
        :model="exclusionForm"
        :rules="exclusionRules"
        label-width="80px"
      >
        <el-form-item label="代码" prop="symbol">
          <el-input v-model="exclusionForm.symbol" placeholder="如 511880" />
        </el-form-item>
        <el-form-item label="市场" prop="market">
          <el-select v-model="exclusionForm.market" style="width: 100%">
            <el-option v-for="m in marketOptions" :key="m" :label="m" :value="m" />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="exclusionForm.note" placeholder="如 货币基金，专注股票投资回报" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="exclusionDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="exclusionDialog.saving" @click="saveExclusion">
          保存
        </el-button>
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
          <template #empty><el-empty description="无持仓数据" :image-size="60" /></template>
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
          <template #empty><el-empty description="无现金数据" :image-size="60" /></template>
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
              v-for="item in diffDialog.row.diff_detail.replay_inconsistent"
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

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, Coin, Files, Plus, Refresh, Remove, Wallet } from '@element-plus/icons-vue'
import api from '@/api'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { formatDate, formatDateTime, formatNumber } from '@/utils/helpers'

const localDateString = (date) =>
  [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, '0'),
    String(date.getDate()).padStart(2, '0')
  ].join('-')
const today = () => localDateString(new Date())
const monthEnd = () => {
  const now = new Date()
  return localDateString(new Date(now.getFullYear(), now.getMonth(), 0))
}
const rowsFrom = (payload) => {
  const data = payload?.data ?? payload
  return Array.isArray(data) ? data : data?.items || data?.results || []
}

const activeTab = ref('accounts')
const accounts = ref([])
const cashEvents = ref([])
const importBatches = ref([])
const snapshots = ref([])
const exclusions = ref([])
const refreshing = ref(false)
const loading = reactive({
  accounts: false,
  cash: false,
  batches: false,
  snapshots: false,
  exclusions: false
})
const batchDrawerVisible = ref(false)
const selectedBatch = ref(null)

const brokerOptions = ['招商证券', '东方财富证券', 'IBKR', '汇丰香港']
const currencyOptions = ['CNY', 'HKD', 'USD', 'SGD']
const marketOptions = ['A股', 'B股', '港股', '美股', '新加坡股']
const cashTypeOptions = [
  { label: '入金', value: 'DEPOSIT' },
  { label: '出金', value: 'WITHDRAWAL' },
  { label: '利息', value: 'INTEREST' },
  { label: '费用', value: 'FEE' },
  { label: '税费', value: 'TAX' },
  { label: '转入', value: 'TRANSFER_IN' },
  { label: '转出', value: 'TRANSFER_OUT' },
  { label: '换汇转入', value: 'FX_IN' },
  { label: '换汇转出', value: 'FX_OUT' },
  { label: '其他', value: 'OTHER' }
]

const accountDialog = reactive({ visible: false, id: null, saving: false })
const cashDialog = reactive({ visible: false, id: null, saving: false })
const snapshotDialog = reactive({ visible: false, id: null, saving: false })
const exclusionDialog = reactive({ visible: false, saving: false })
const accountFormRef = ref()
const cashFormRef = ref()
const snapshotFormRef = ref()
const exclusionFormRef = ref()
const accountForm = reactive({})
const cashForm = reactive({})
const snapshotForm = reactive({})
const exclusionForm = reactive({ symbol: '', market: 'A股', note: '' })
const exclusionRules = {
  symbol: [{ required: true, message: '请输入证券代码', trigger: 'blur' }],
  market: [{ required: true, message: '请选择市场', trigger: 'change' }]
}
const diffDialog = reactive({ visible: false, row: null })
const comparingSnapshotId = ref(null)

const DIFF_ITEM_LABELS = {
  MATCH: '一致',
  QUANTITY_MISMATCH: '数量差',
  MISSING_IN_SYSTEM: '系统缺记录',
  MISSING_IN_SNAPSHOT: '快照缺记录'
}

const diffItemLabel = (status) => DIFF_ITEM_LABELS[status] || status
const diffItemTag = (status) => (status === 'MATCH' ? 'success' : 'danger')

function openDiffDialog(row) {
  diffDialog.row = row
  diffDialog.visible = true
}

async function compareSnapshot(row) {
  comparingSnapshotId.value = row.id
  try {
    const response = await api.compareReconciliationSnapshot(row.id)
    ElMessage.success(
      response.data.status === 'MATCHED' ? '比对一致' : '比对发现差异，点击状态查看明细'
    )
    await loadSnapshots()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '比对失败'))
  } finally {
    comparingSnapshotId.value = null
  }
}
const cashFilters = reactive({ accountId: null, eventType: '' })

const accountRules = {
  account_name: [{ required: true, message: '请输入账户名称', trigger: 'blur' }],
  broker: [{ required: true, message: '请选择券商', trigger: 'change' }],
  base_currency: [{ required: true, message: '请选择基础币种', trigger: 'change' }]
}
const cashRules = {
  broker_account_id: [{ required: true, message: '请选择账户', trigger: 'change' }],
  event_date: [{ required: true, message: '请选择日期', trigger: 'change' }],
  event_type: [{ required: true, message: '请选择类型', trigger: 'change' }],
  amount: [{ required: true, message: '请输入金额', trigger: 'blur' }],
  currency: [{ required: true, message: '请选择币种', trigger: 'change' }]
}
const snapshotRules = {
  broker_account_id: [{ required: true, message: '请选择账户', trigger: 'change' }],
  snapshot_date: [{ required: true, message: '请选择日期', trigger: 'change' }]
}

const accountName = (account) =>
  account?.account_name ||
  account?.name ||
  [account?.broker, account?.account_number_masked].filter(Boolean).join(' ') ||
  '未命名账户'
const accountLabel = (id) => {
  const account = accounts.value.find((item) => String(item.id) === String(id))
  return account ? accountName(account) : '未关联账户'
}
const activeAccountCount = computed(
  () => accounts.value.filter((item) => item.is_active !== false).length
)
const reconciledCount = computed(
  () => snapshots.value.filter((item) => String(item.status).toUpperCase() === 'MATCHED').length
)
const latestBatchDate = computed(() => {
  const dates = importBatches.value
    .map((item) => item.created_at || item.imported_at)
    .filter(Boolean)
    .sort()
  return dates.length ? formatDate(dates.at(-1)) : '尚无'
})
const filteredCashEvents = computed(() =>
  cashEvents.value.filter((item) => {
    const accountId = item.broker_account_id ?? item.account_id
    return (
      (!cashFilters.accountId || String(accountId) === String(cashFilters.accountId)) &&
      (!cashFilters.eventType || item.event_type === cashFilters.eventType)
    )
  })
)

function resetAccountForm(row = {}) {
  Object.assign(accountForm, {
    account_name: row.account_name || row.name || '',
    broker: row.broker || row.broker_name || '',
    account_number_masked: row.account_number_masked || '',
    base_currency: row.base_currency || 'CNY',
    is_active: row.is_active !== false,
    notes: row.notes || ''
  })
}

function resetCashForm(row = {}) {
  Object.assign(cashForm, {
    broker_account_id: row.broker_account_id || row.account_id || accounts.value[0]?.id || null,
    event_date: (row.event_date || row.occurred_at || today()).slice(0, 10),
    event_type: row.event_type || 'DEPOSIT',
    amount: row.amount == null ? null : Math.abs(Number(row.amount)),
    currency:
      row.currency ||
      accounts.value.find((item) => item.id === (row.broker_account_id || row.account_id))
        ?.base_currency ||
      'CNY',
    notes: row.notes || ''
  })
}

function resetSnapshotForm(row = {}) {
  const cashBalances = normalizeJson(row.cash_balances || row.reported_cash, {})
  const positions = normalizeJson(row.positions || row.reported_positions, [])
  Object.assign(snapshotForm, {
    broker_account_id: row.broker_account_id || row.account_id || accounts.value[0]?.id || null,
    snapshot_date: (row.snapshot_date || monthEnd()).slice(0, 10),
    source_filename: row.source_filename || '',
    cashRows: Object.entries(cashBalances).length
      ? Object.entries(cashBalances).map(([currency, amount]) => ({
          currency,
          amount: Number(amount)
        }))
      : [{ currency: 'CNY', amount: 0 }],
    positionRows: Array.isArray(positions)
      ? positions.map((item) => ({ ...item, quantity: Number(item.quantity) }))
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

function openAccountDialog(row) {
  accountDialog.id = row?.id || null
  resetAccountForm(row)
  accountDialog.visible = true
}

function openCashDialog(row) {
  cashDialog.id = row?.id || null
  resetCashForm(row)
  cashDialog.visible = true
}

function openSnapshotDialog(row) {
  snapshotDialog.id = row?.id || null
  resetSnapshotForm(row)
  snapshotDialog.visible = true
}

async function loadAccounts() {
  loading.accounts = true
  try {
    accounts.value = rowsFrom(await api.getBrokerAccounts())
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '账户加载失败'))
  } finally {
    loading.accounts = false
  }
}

async function loadCashEvents() {
  loading.cash = true
  try {
    cashEvents.value = rowsFrom(await api.getCashEvents({ limit: 1000 }))
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '现金事件加载失败'))
  } finally {
    loading.cash = false
  }
}

async function loadImportBatches() {
  loading.batches = true
  try {
    importBatches.value = rowsFrom(await api.getImportBatches({ limit: 1000 }))
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '导入批次加载失败'))
  } finally {
    loading.batches = false
  }
}

async function loadSnapshots() {
  loading.snapshots = true
  try {
    snapshots.value = rowsFrom(await api.getReconciliationSnapshots({ limit: 1000 }))
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '月末核对加载失败'))
  } finally {
    loading.snapshots = false
  }
}

async function loadExclusions() {
  loading.exclusions = true
  try {
    exclusions.value = rowsFrom(await api.getExcludedSecurities())
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '排除清单加载失败'))
  } finally {
    loading.exclusions = false
  }
}

function openExclusionDialog() {
  Object.assign(exclusionForm, { symbol: '', market: 'A股', note: '' })
  exclusionDialog.visible = true
}

async function saveExclusion() {
  if (!(await exclusionFormRef.value?.validate().catch(() => false))) return
  exclusionDialog.saving = true
  try {
    await api.createExcludedSecurity({ ...exclusionForm })
    ElMessage.success('已加入排除清单；导入与对账比对将忽略该标的')
    exclusionDialog.visible = false
    await loadExclusions()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '排除标的保存失败'))
  } finally {
    exclusionDialog.saving = false
  }
}

async function removeExclusion(row) {
  try {
    await ElMessageBox.confirm(
      `移出排除清单后，${row.symbol} 在后续导入时会重新入账。确认移出？`,
      '移出排除清单',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await api.deleteExcludedSecurity(row.id)
    ElMessage.success('已移出排除清单')
    await loadExclusions()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '移出失败'))
  }
}

async function refreshAll() {
  refreshing.value = true
  await Promise.all([
    loadAccounts(),
    loadCashEvents(),
    loadImportBatches(),
    loadSnapshots(),
    loadExclusions()
  ])
  refreshing.value = false
}

async function saveAccount() {
  if (!(await accountFormRef.value?.validate().catch(() => false))) return
  accountDialog.saving = true
  try {
    const payload = { ...accountForm }
    if (accountDialog.id) await api.updateBrokerAccount(accountDialog.id, payload)
    else await api.createBrokerAccount(payload)
    ElMessage.success(accountDialog.id ? '账户已更新' : '账户已新增')
    accountDialog.visible = false
    await loadAccounts()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '账户保存失败'))
  } finally {
    accountDialog.saving = false
  }
}

async function saveCashEvent() {
  if (!(await cashFormRef.value?.validate().catch(() => false))) return
  cashDialog.saving = true
  try {
    const payload = { ...cashForm }
    if (cashDialog.id) await api.updateCashEvent(cashDialog.id, payload)
    else await api.createCashEvent(payload)
    ElMessage.success(cashDialog.id ? '现金事件已更新' : '现金事件已新增')
    cashDialog.visible = false
    await loadCashEvents()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '现金事件保存失败'))
  } finally {
    cashDialog.saving = false
  }
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
    await loadSnapshots()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '核对记录保存失败'))
  } finally {
    snapshotDialog.saving = false
  }
}

async function confirmDelete(title, message) {
  await ElMessageBox.confirm(message, title, {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消'
  })
}

async function removeAccount(row) {
  try {
    await confirmDelete(
      '删除账户',
      `仅空账户可以删除。若“${accountName(row)}”已有交易或审计记录，请编辑账户并将其停用。`
    )
    await api.deleteBrokerAccount(row.id)
    ElMessage.success('账户已删除')
    await loadAccounts()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close')
      ElMessage.error(getApiErrorMessage(error, '账户删除失败'))
  }
}

async function removeCashEvent(row) {
  try {
    await confirmDelete('删除现金事件', '该操作会影响后续账户收益校准，确认删除？')
    await api.deleteCashEvent(row.id)
    ElMessage.success('现金事件已删除')
    await loadCashEvents()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close')
      ElMessage.error(getApiErrorMessage(error, '现金事件删除失败'))
  }
}

async function removeSnapshot(row) {
  try {
    await confirmDelete('删除核对记录', '确认删除这条月末核对记录？')
    await api.deleteReconciliationSnapshot(row.id)
    ElMessage.success('核对记录已删除')
    await loadSnapshots()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close')
      ElMessage.error(getApiErrorMessage(error, '核对记录删除失败'))
  }
}

async function openBatchDetails(row) {
  selectedBatch.value = row
  batchDrawerVisible.value = true
  try {
    const response = await api.getImportBatch(row.id)
    selectedBatch.value = response?.data || row
  } catch {
    // The list already contains enough information for the drawer.
  }
}

const cashTypeLabel = (type) =>
  cashTypeOptions.find((item) => item.value === type)?.label || type || '其他'
const cashTypeTag = (type) => {
  if (['DEPOSIT', 'INTEREST', 'TRANSFER_IN', 'FX_IN'].includes(type)) return 'success'
  if (['WITHDRAWAL', 'FEE', 'TAX', 'TRANSFER_OUT', 'FX_OUT'].includes(type)) return 'warning'
  return 'info'
}
const cashDirection = (row) =>
  ['WITHDRAWAL', 'FEE', 'TAX', 'TRANSFER_OUT', 'FX_OUT'].includes(row.event_type) ? -1 : 1
const signedAmount = (row) => {
  const value = Math.abs(Number(row.amount || 0)) * cashDirection(row)
  const prefix = value > 0 ? '+' : ''
  return `${prefix}${formatNumber(value)} ${row.currency || ''}`
}
const amountClass = (row) => ({
  'amount-positive': cashDirection(row) > 0,
  'amount-negative': cashDirection(row) < 0
})
const reportPeriod = (row) => {
  const start = row.period_start || row.statement_start_date
  const end = row.period_end || row.statement_end_date
  return start || end ? `${start || '?'} 至 ${end || '?'}` : '未声明报表区间'
}
const batchStatusLabel = (status) =>
  ({
    COMPLETED: '完成',
    SUCCESS: '完成',
    FAILED: '失败',
    PARTIAL: '部分完成',
    PROCESSING: '处理中',
    PENDING: '等待中'
  })[String(status).toUpperCase()] ||
  status ||
  '未知'
const batchStatusTag = (status) => {
  const value = String(status).toUpperCase()
  if (['COMPLETED', 'SUCCESS'].includes(value)) return 'success'
  if (value === 'FAILED') return 'danger'
  if (value === 'PARTIAL') return 'warning'
  return 'info'
}
const snapshotStatusLabel = (row) => {
  const status = String(row?.status || '').toUpperCase()
  // 分范围对账单不比对现金，绿色语义限定为"持仓一致"，避免误读为整体对账完成
  if (status === 'MATCHED' && row?.statement_scope) return '持仓一致'
  return (
    {
      PENDING: '待比对',
      MATCHED: '比对一致',
      MISMATCHED: '有差异'
    }[status] ||
    row?.status ||
    '待比对'
  )
}
const snapshotStatusTag = (status) => {
  const value = String(status).toUpperCase()
  if (value === 'MATCHED') return 'success'
  if (value === 'MISMATCHED') return 'danger'
  return 'warning'
}
const normalizeJson = (value, fallback) => {
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
const jsonSummary = (value) => {
  const data = normalizeJson(value, {})
  if (!data || Array.isArray(data) || typeof data !== 'object') return '-'
  const entries = Object.entries(data)
  if (!entries.length) return '-'
  return entries.map(([currency, amount]) => `${currency} ${formatNumber(amount)}`).join(' · ')
}
const positionSummary = (value) => {
  const data = normalizeJson(value, [])
  if (Array.isArray(data)) return data.length ? `${data.length} 个标的` : '-'
  if (data && typeof data === 'object') return `${Object.keys(data).length} 个标的`
  return '-'
}

onMounted(refreshAll)
</script>

<style scoped>
.account-data-page {
  display: grid;
  gap: 20px;
}

.page-intro {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
}

.eyebrow {
  margin: 0 0 6px;
  color: var(--app-primary);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
}

.page-intro h1 {
  margin: 0;
  color: var(--app-text);
  font-size: clamp(25px, 4vw, 34px);
  letter-spacing: -0.04em;
}

.page-intro p:last-child,
.section-toolbar p {
  margin: 7px 0 0;
  color: var(--app-text-soft);
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}

.summary-item {
  display: grid;
  gap: 5px;
  min-width: 0;
  padding: 18px 20px;
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-radius);
  background: var(--app-surface);
  box-shadow: var(--app-shadow-sm);
}

.summary-item span,
.summary-item small {
  color: var(--app-text-soft);
}

.summary-item strong {
  color: var(--app-text);
  font-size: 25px;
  letter-spacing: -0.04em;
}

.summary-item .summary-date {
  overflow: hidden;
  font-size: 18px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.content-card :deep(.el-card__body) {
  padding-top: 8px;
}

.data-tabs :deep(.el-tabs__header) {
  margin-bottom: 22px;
}

.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.tab-label svg {
  width: 16px;
}

.section-toolbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 18px;
}

.section-toolbar h2 {
  margin: 0;
  font-size: 18px;
  letter-spacing: -0.02em;
}

.section-toolbar p {
  font-size: 14px;
}

.compact-filter {
  margin-bottom: 16px;
  padding: 12px 16px 0;
  border-radius: var(--app-radius-inner);
  background: var(--app-surface-muted);
}

.responsive-table {
  width: 100%;
  overflow-x: auto;
}

.primary-cell {
  display: grid;
  gap: 3px;
}

.primary-cell strong {
  color: var(--app-text);
}

.primary-cell span {
  color: var(--app-text-soft);
  font-size: 12px;
}

.amount-positive {
  color: var(--app-success);
}

.amount-negative {
  color: var(--app-danger);
}

.field-hint {
  width: 100%;
  margin-top: 4px;
  color: var(--app-text-soft);
  font-size: 12px;
}

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

.hash-value {
  word-break: break-all;
}

@media (max-width: 900px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .account-data-page {
    gap: 16px;
  }

  .page-intro {
    align-items: stretch;
    flex-direction: column;
    gap: 12px;
  }

  .page-intro > .el-button {
    width: 100%;
  }

  .summary-grid {
    gap: 10px;
  }

  .summary-item {
    padding: 14px;
  }

  .summary-item strong {
    font-size: 21px;
  }

  .summary-item small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .content-card :deep(.el-card__body) {
    padding: 8px 14px 16px;
  }

  .data-tabs :deep(.el-tabs__nav-wrap) {
    overflow-x: auto;
  }

  .section-toolbar {
    align-items: stretch;
    flex-direction: column;
    gap: 12px;
  }

  .section-toolbar > .el-button {
    width: 100%;
  }

  .compact-filter {
    padding-bottom: 4px;
  }

  .compact-filter :deep(.el-form-item),
  .compact-filter :deep(.el-select) {
    width: 100%;
  }

  .cash-row,
  .position-row {
    grid-template-columns: 1fr;
  }

  .repeatable-row > .el-button {
    width: 100%;
  }
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
</style>
