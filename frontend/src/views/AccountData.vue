<template>
  <div class="account-data-page">
    <section class="page-intro">
      <div>
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

          <div v-if="!isMobileView" class="responsive-table desktop-data-table">
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

          <div v-else v-loading="loading.accounts" class="mobile-card-list">
            <el-empty v-if="!accounts.length" description="尚未登记券商账户" :image-size="88">
              <el-button type="primary" @click="openAccountDialog()">新增第一个账户</el-button>
            </el-empty>
            <article
              v-for="row in accounts"
              :key="row.id"
              class="mobile-card"
              data-testid="account-card"
            >
              <div class="mobile-card-head">
                <div class="mobile-card-title">
                  <span class="mobile-card-symbol">{{ accountName(row) }}</span>
                  <span class="mobile-card-name">
                    {{ row.account_number_masked || '未填写尾号' }}
                  </span>
                </div>
                <div class="mobile-card-tags">
                  <el-tag :type="row.is_active === false ? 'info' : 'success'" size="small">
                    {{ row.is_active === false ? '停用' : '启用' }}
                  </el-tag>
                </div>
              </div>

              <div class="mobile-card-meta">
                <span>{{ row.broker || row.broker_name || '未填写券商' }}</span>
                <span>{{ row.base_currency }}</span>
                <span v-if="row.notes">{{ row.notes }}</span>
              </div>

              <div class="mobile-card-actions">
                <el-button type="primary" size="small" text @click="openAccountDialog(row)">
                  编辑
                </el-button>
                <el-button type="danger" size="small" text @click="removeAccount(row)">
                  删除空账户
                </el-button>
              </div>
            </article>
          </div>
        </el-tab-pane>

        <el-tab-pane name="cash">
          <template #label>
            <span class="tab-label"><Coin />现金事件</span>
          </template>

          <div class="section-toolbar">
            <div>
              <h2>现金事件</h2>
              <p>
                保存真实资金变化，用于现金对账与出入金追踪；收益统计为权益仓口径（仅证券投入，不含账户现金）。
              </p>
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

          <div v-if="!isMobileView" class="responsive-table desktop-data-table">
            <el-table :data="filteredCashEvents" v-loading="loading.cash" stripe row-key="id">
              <template #empty>
                <el-empty description="暂无现金事件" :image-size="88" />
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
                  <template v-if="!row.imported">
                    <el-button type="primary" text @click="openCashDialog(row)">编辑</el-button>
                    <el-button type="danger" text @click="removeCashEvent(row)">删除</el-button>
                  </template>
                  <el-tag v-else type="info" size="small">导入生成 · 只读</el-tag>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div v-else v-loading="loading.cash" class="mobile-card-list">
            <el-empty
              v-if="!filteredCashEvents.length"
              description="暂无现金事件"
              :image-size="88"
            />
            <article
              v-for="row in filteredCashEvents"
              :key="row.id"
              class="mobile-card"
              data-testid="cash-event-card"
            >
              <div class="mobile-card-head">
                <div class="mobile-card-title">
                  <span class="mobile-card-symbol" :class="amountClass(row)">
                    {{ signedAmount(row) }}
                  </span>
                  <span class="mobile-card-name">
                    {{ accountLabel(row.broker_account_id || row.account_id) }}
                  </span>
                </div>
                <div class="mobile-card-tags">
                  <el-tag :type="cashTypeTag(row.event_type)" size="small">
                    {{ cashTypeLabel(row.event_type) }}
                  </el-tag>
                </div>
              </div>

              <div class="mobile-card-meta">
                <span>{{ formatDate(row.event_date || row.occurred_at) }}</span>
                <span v-if="row.notes">{{ row.notes }}</span>
              </div>

              <div class="mobile-card-actions">
                <template v-if="!row.imported">
                  <el-button type="primary" size="small" text @click="openCashDialog(row)">
                    编辑
                  </el-button>
                  <el-button type="danger" size="small" text @click="removeCashEvent(row)">
                    删除
                  </el-button>
                </template>
                <el-tag v-else type="info" size="small">导入生成 · 只读</el-tag>
              </div>
            </article>
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
                <el-empty description="暂无可追溯的导入批次" :image-size="88" />
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

          <div v-if="!isMobileView" class="responsive-table desktop-data-table">
            <el-table :data="snapshots" v-loading="loading.snapshots" stripe row-key="id">
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

          <div v-else v-loading="loading.snapshots" class="mobile-card-list">
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
        </el-tab-pane>
        <el-tab-pane name="exclusions">
          <template #label>
            <span class="tab-label"><Remove />特例规则</span>
          </template>

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
            <el-table :data="securityRules" v-loading="loading.rules" stripe row-key="id">
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
      width="560px"
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
          <el-input
            v-model="ruleForm.symbol"
            :placeholder="
              ruleForm.rule_type === 'CMB_CASH_BUSINESS' ? '如 招现宝收益' : '如 511880'
            "
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
            <el-input v-model="ruleForm.new_symbol" placeholder="如 PCT" />
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

<script setup lang="ts">
import { computed, onMounted, reactive, ref, type Ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { CircleCheck, Coin, Files, Plus, Refresh, Remove, Wallet } from '@element-plus/icons-vue'
import api from '@/api'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { formatDate, formatDateTime, formatNumber, todayLocalISODate } from '@/utils/helpers'
import { formatLocalDate } from '@/utils/dateRange'
import { useMediaQuery } from '@/composables/useMediaQuery'

interface AccountRow {
  id: number
  account_name?: string
  name?: string
  broker?: string
  broker_name?: string
  account_number_masked?: string
  base_currency?: string
  is_active?: boolean
  notes?: string
  [key: string]: unknown
}

interface CashEventRow {
  id: number
  broker_account_id?: number | null
  account_id?: number | null
  event_date?: string
  occurred_at?: string
  event_type?: string
  amount?: number | string | null
  currency?: string | null
  notes?: string | null
  [key: string]: unknown
}

interface ImportBatchRow {
  id: number
  broker_account_id?: number | null
  account_id?: number | null
  created_at?: string
  imported_at?: string
  period_start?: string
  period_end?: string
  statement_start_date?: string
  statement_end_date?: string
  status?: string
  source_filename?: string | null
  original_filename?: string | null
  source_type?: string | null
  parser_name?: string | null
  parser_version?: string | null
  source_sha256?: string | null
  file_sha256?: string | null
  row_count?: number | null
  total_rows?: number | null
  archived_count?: number | null
  imported_count?: number | null
  duplicate_count?: number | null
  skipped_count?: number | null
  error_count?: number | null
  error_message?: string | null
  [key: string]: unknown
}

interface DiffDetail {
  positions?: Array<Record<string, unknown>>
  cash?: Array<Record<string, unknown>>
  summary?: { cash_compared?: boolean; [key: string]: unknown }
  replay_inconsistent?: Array<{ symbol?: string; market?: string; reason?: string }>
  methodology_notes?: string[]
  [key: string]: unknown
}

interface SnapshotRow {
  id: number
  broker_account_id?: number | null
  account_id?: number | null
  snapshot_date?: string
  source_filename?: string | null
  cash_balances?: unknown
  reported_cash?: unknown
  positions?: unknown
  reported_positions?: unknown
  status?: string
  statement_scope?: string | null
  diff_detail?: DiffDetail | null
  compared_at?: string | null
  notes?: string | null
  [key: string]: unknown
}

interface SecurityRuleRow {
  id: number
  rule_type: string
  symbol: string
  market: string | null
  payload: Record<string, unknown> | null
  note: string | null
  created_at: string
  [key: string]: unknown
}

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

interface DialogState {
  visible: boolean
  id: number | null
  saving: boolean
}

const today = () => todayLocalISODate()
const monthEnd = () => {
  const now = new Date()
  return formatLocalDate(new Date(now.getFullYear(), now.getMonth(), 0))
}
const rowsFrom = <T,>(payload: unknown): T[] => {
  const data = (payload as { data?: unknown } | null | undefined)?.data ?? payload
  if (Array.isArray(data)) return data as T[]
  const container = data as { items?: T[]; results?: T[] } | null | undefined
  return container?.items || container?.results || []
}

const isMobileView = useMediaQuery('(max-width: 640px)')
const activeTab = ref('accounts')
const accounts = ref<AccountRow[]>([])
const cashEvents = ref<CashEventRow[]>([])
const importBatches = ref<ImportBatchRow[]>([])
const snapshots = ref<SnapshotRow[]>([])
const securityRules = ref<SecurityRuleRow[]>([])
const refreshing = ref(false)
const loading = reactive({
  accounts: false,
  cash: false,
  batches: false,
  snapshots: false,
  rules: false
})
const batchDrawerVisible = ref(false)
const selectedBatch = ref<ImportBatchRow | null>(null)

const brokerOptions = ['招商证券', '东方财富证券', 'IBKR', '汇丰香港']
const currencyOptions = ['CNY', 'HKD', 'USD', 'SGD']
// 与后端 VALID_MARKETS 对齐（快照持仓行与特例规则表单共用）
const marketOptions = ['A股', 'B股', '港股', '美股', '新加坡股', '加密货币']
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

const accountDialog = reactive<DialogState>({ visible: false, id: null, saving: false })
const cashDialog = reactive<DialogState>({ visible: false, id: null, saving: false })
const snapshotDialog = reactive<DialogState>({ visible: false, id: null, saving: false })
const ruleDialog = reactive({ visible: false, saving: false })
const accountFormRef = ref<FormInstance>()
const cashFormRef = ref<FormInstance>()
const snapshotFormRef = ref<FormInstance>()
const ruleFormRef = ref<FormInstance>()
const accountForm = reactive<{
  account_name?: string
  broker?: string
  account_number_masked?: string
  base_currency?: string
  is_active?: boolean
  notes?: string
}>({})
const cashForm = reactive<{
  broker_account_id?: number | null
  event_date?: string
  event_type?: string
  amount?: number | null
  currency?: string
  notes?: string
}>({})
const snapshotForm = reactive<{
  broker_account_id?: number | null
  snapshot_date?: string
  source_filename?: string
  cashRows: SnapshotCashRowInput[]
  positionRows: SnapshotPositionRowInput[]
  notes?: string
}>({ cashRows: [], positionRows: [] })
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
const ruleFilters = reactive<{ ruleType: string }>({ ruleType: '' })
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
    await loadSnapshots()
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error, '比对失败'))
  } finally {
    comparingSnapshotId.value = null
  }
}
const cashFilters = reactive<{ accountId: number | null; eventType: string }>({
  accountId: null,
  eventType: ''
})

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

const accountName = (account: AccountRow | null | undefined) =>
  account?.account_name ||
  account?.name ||
  [account?.broker, account?.account_number_masked].filter(Boolean).join(' ') ||
  '未命名账户'
const accountLabel = (id: unknown) => {
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
  return dates.length ? formatDate(dates[dates.length - 1]) : '尚无'
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

function resetAccountForm(row: Partial<AccountRow> = {}) {
  Object.assign(accountForm, {
    account_name: row.account_name || row.name || '',
    broker: row.broker || row.broker_name || '',
    account_number_masked: row.account_number_masked || '',
    base_currency: row.base_currency || 'CNY',
    is_active: row.is_active !== false,
    notes: row.notes || ''
  })
}

function resetCashForm(row: Partial<CashEventRow> = {}) {
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

function resetSnapshotForm(row: Partial<SnapshotRow> = {}) {
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

function openAccountDialog(row?: AccountRow) {
  accountDialog.id = row?.id || null
  resetAccountForm(row)
  accountDialog.visible = true
}

function openCashDialog(row?: CashEventRow) {
  cashDialog.id = row?.id || null
  resetCashForm(row)
  cashDialog.visible = true
}

function openSnapshotDialog(row?: SnapshotRow) {
  snapshotDialog.id = row?.id || null
  resetSnapshotForm(row)
  snapshotDialog.visible = true
}

// 列表加载工厂：loading 键 + 目标 ref + 拉取函数 + 失败文案，形状完全一致
function makeLoader<T>(
  loadingKey: keyof typeof loading,
  target: Ref<T[]>,
  fetcher: () => Promise<unknown>,
  failureMessage: string
) {
  return async () => {
    loading[loadingKey] = true
    try {
      target.value = rowsFrom<T>(await fetcher())
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, failureMessage))
    } finally {
      loading[loadingKey] = false
    }
  }
}

const loadAccounts = makeLoader('accounts', accounts, () => api.getBrokerAccounts(), '账户加载失败')
const loadCashEvents = makeLoader(
  'cash',
  cashEvents,
  () => api.getCashEvents({ limit: 1000 }),
  '现金事件加载失败'
)
const loadImportBatches = makeLoader(
  'batches',
  importBatches,
  () => api.getImportBatches({ limit: 1000 }),
  '导入批次加载失败'
)
const loadSnapshots = makeLoader(
  'snapshots',
  snapshots,
  () => api.getReconciliationSnapshots({ limit: 1000 }),
  '月末核对加载失败'
)
// 筛选切换是竞态高发区：慢的旧响应不得覆盖新筛选的结果——捕获请求
// 令牌，只接纳仍是最新请求的响应（含 loading 归位与错误提示）
let rulesRequestToken = 0
async function loadSecurityRules() {
  rulesRequestToken += 1
  const token = rulesRequestToken
  const requestedType = ruleFilters.ruleType
  loading.rules = true
  try {
    const response = await api.getSecurityRules(
      requestedType ? { rule_type: requestedType } : undefined
    )
    if (token !== rulesRequestToken) return
    securityRules.value = rowsFrom<SecurityRuleRow>(response)
  } catch (error) {
    if (token !== rulesRequestToken) return
    ElMessage.error(getApiErrorMessage(error, '特例规则加载失败'))
  } finally {
    if (token === rulesRequestToken) loading.rules = false
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

async function refreshAll() {
  refreshing.value = true
  await Promise.all([
    loadAccounts(),
    loadCashEvents(),
    loadImportBatches(),
    loadSnapshots(),
    loadSecurityRules()
  ])
  refreshing.value = false
}

// 表单保存工厂：校验 → 创建/更新 → 按分支提示 → 关窗重载（快照另有专属校验，不并入）
function makeSaver({
  formRef,
  dialog,
  buildPayload,
  update,
  create,
  messages,
  reload
}: {
  formRef: Ref<FormInstance | undefined>
  dialog: DialogState
  buildPayload: () => Record<string, unknown>
  update: (id: number, payload: Record<string, unknown>) => Promise<unknown>
  create: (payload: Record<string, unknown>) => Promise<unknown>
  messages: { updated: string; created: string; failure: string }
  reload: () => Promise<unknown>
}) {
  return async () => {
    if (!(await formRef.value?.validate().catch(() => false))) return
    dialog.saving = true
    try {
      const payload = buildPayload()
      if (dialog.id) await update(dialog.id, payload)
      else await create(payload)
      ElMessage.success(dialog.id ? messages.updated : messages.created)
      dialog.visible = false
      await reload()
    } catch (error) {
      ElMessage.error(getApiErrorMessage(error, messages.failure))
    } finally {
      dialog.saving = false
    }
  }
}

const saveAccount = makeSaver({
  formRef: accountFormRef,
  dialog: accountDialog,
  buildPayload: () => ({ ...accountForm }),
  update: (id, payload) => api.updateBrokerAccount(id, payload),
  create: (payload) => api.createBrokerAccount(payload),
  messages: { updated: '账户已更新', created: '账户已新增', failure: '账户保存失败' },
  reload: () => loadAccounts()
})

const saveCashEvent = makeSaver({
  formRef: cashFormRef,
  dialog: cashDialog,
  buildPayload: () => ({ ...cashForm }),
  update: (id, payload) => api.updateCashEvent(id, payload),
  create: (payload) => api.createCashEvent(payload),
  messages: { updated: '现金事件已更新', created: '现金事件已新增', failure: '现金事件保存失败' },
  reload: () => loadCashEvents()
})

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

async function confirmDelete(title: string, message: string) {
  await ElMessageBox.confirm(message, title, {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消'
  })
}

// 删除处理工厂：确认 → 删除 → 成功提示 → 重载；message 支持函数以插入行数据
function makeRemover<T>({
  title,
  message,
  request,
  successMessage,
  failureMessage,
  reload
}: {
  title: string
  message: string | ((row: T) => string)
  request: (row: T) => Promise<unknown>
  successMessage: string
  failureMessage: string
  reload: () => Promise<unknown>
}) {
  return async (row: T) => {
    try {
      await confirmDelete(title, typeof message === 'function' ? message(row) : message)
      await request(row)
      ElMessage.success(successMessage)
      await reload()
    } catch (error) {
      if (error !== 'cancel' && error !== 'close')
        ElMessage.error(getApiErrorMessage(error, failureMessage))
    }
  }
}

const removeAccount = makeRemover<AccountRow>({
  title: '删除账户',
  message: (row) =>
    `仅空账户可以删除。若“${accountName(row)}”已有交易或审计记录，请编辑账户并将其停用。`,
  request: (row) => api.deleteBrokerAccount(row.id),
  successMessage: '账户已删除',
  failureMessage: '账户删除失败',
  reload: () => loadAccounts()
})

const removeCashEvent = makeRemover<CashEventRow>({
  title: '删除现金事件',
  message: '该操作会影响后续账户收益校准，确认删除？',
  request: (row) => api.deleteCashEvent(row.id),
  successMessage: '现金事件已删除',
  failureMessage: '现金事件删除失败',
  reload: () => loadCashEvents()
})

const removeSnapshot = makeRemover<SnapshotRow>({
  title: '删除核对记录',
  message: '确认删除这条月末核对记录？',
  request: (row) => api.deleteReconciliationSnapshot(row.id),
  successMessage: '核对记录已删除',
  failureMessage: '核对记录删除失败',
  reload: () => loadSnapshots()
})

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

async function openBatchDetails(row: ImportBatchRow) {
  selectedBatch.value = row
  batchDrawerVisible.value = true
  try {
    const response = await api.getImportBatch(row.id)
    selectedBatch.value = response?.data || row
  } catch {
    // The list already contains enough information for the drawer.
  }
}

const cashTypeLabel = (type: string | undefined) =>
  cashTypeOptions.find((item) => item.value === type)?.label || type || '其他'
const cashTypeTag = (type: string | undefined) => {
  if (['DEPOSIT', 'INTEREST', 'TRANSFER_IN', 'FX_IN'].includes(type || '')) return 'success'
  if (['WITHDRAWAL', 'FEE', 'TAX', 'TRANSFER_OUT', 'FX_OUT'].includes(type || '')) return 'warning'
  return 'info'
}
const cashDirection = (row: CashEventRow) =>
  ['WITHDRAWAL', 'FEE', 'TAX', 'TRANSFER_OUT', 'FX_OUT'].includes(row.event_type || '') ? -1 : 1
const signedAmount = (row: CashEventRow) => {
  const value = Math.abs(Number(row.amount || 0)) * cashDirection(row)
  const prefix = value > 0 ? '+' : ''
  return `${prefix}${formatNumber(value)} ${row.currency || ''}`
}
const amountClass = (row: CashEventRow) => ({
  'amount-positive': cashDirection(row) > 0,
  'amount-negative': cashDirection(row) < 0
})
const reportPeriod = (row: ImportBatchRow) => {
  const start = row.period_start || row.statement_start_date
  const end = row.period_end || row.statement_end_date
  return start || end ? `${start || '?'} 至 ${end || '?'}` : '未声明报表区间'
}
const batchStatusLabel = (status: string | undefined) =>
  (
    ({
      COMPLETED: '完成',
      SUCCESS: '完成',
      FAILED: '失败',
      PARTIAL: '部分完成',
      PROCESSING: '处理中',
      PENDING: '等待中'
    }) as Record<string, string>
  )[String(status).toUpperCase()] ||
  status ||
  '未知'
const batchStatusTag = (status: string | undefined) => {
  const value = String(status).toUpperCase()
  if (['COMPLETED', 'SUCCESS'].includes(value)) return 'success'
  if (value === 'FAILED') return 'danger'
  if (value === 'PARTIAL') return 'warning'
  return 'info'
}
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
// 后端 JSON 字段可能以字符串形式返回，解析失败回退默认值；返回 any 供调用方按形状使用
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const normalizeJson = (value: unknown, fallback: unknown): any => {
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

.page-intro h1 {
  margin: 0;
  color: var(--app-text);
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.02em;
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
  font-size: 22px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}

.summary-item .summary-date {
  overflow: hidden;
  font-size: 16px;
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
  font-size: 16px;
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
.rule-type-hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
</style>
