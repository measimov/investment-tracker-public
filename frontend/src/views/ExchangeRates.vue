<template>
  <div class="exchange-rates-page">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>汇率管理</span>
          <div class="header-actions">
            <el-button type="primary" @click="refreshFromAPI" :loading="refreshing">
              从API更新汇率
            </el-button>
            <el-button type="success" @click="showAddDialog">
              手动添加汇率
            </el-button>
          </div>
        </div>
      </template>

      <!-- 当前汇率展示 -->
      <div class="current-rates" v-if="latestRates">
        <h3>当前汇率（基准货币：{{ latestRates.base_currency }}）</h3>
        <el-row :gutter="20">
          <el-col :xs="24" :sm="12" :md="6" v-for="(rate, currency) in latestRates.rates" :key="currency">
            <el-card shadow="hover" v-if="currency !== 'CNY'">
              <el-statistic
                :title="`1 ${currency} =`"
                :value="rate"
                :precision="4"
                suffix="CNY"
              >
                <template #prefix>
                  <span class="currency-code">{{ currency }}</span>
                </template>
              </el-statistic>
              <div class="rate-info">
                <el-text size="small" type="info">
                  更新: {{ formatDate(latestRates.effective_date) }}
                </el-text>
                <el-tag size="small" :type="getSourceType(latestRates.source)">
                  {{ latestRates.source }}
                </el-tag>
              </div>
            </el-card>
          </el-col>
        </el-row>
      </div>

      <el-divider />

      <!-- 汇率历史记录 -->
      <div class="rate-history">
        <h3>汇率历史记录</h3>
        <div class="responsive-table">
          <el-table :data="rateHistory" stripe style="width: 100%">
            <el-table-column prop="from_currency" label="源币种" width="100" />
            <el-table-column prop="to_currency" label="目标币种" width="100" />
            <el-table-column prop="rate" label="汇率" width="150">
              <template #default="{ row }">
                {{ Number(row.rate).toFixed(4) }}
              </template>
            </el-table-column>
            <el-table-column prop="effective_date" label="生效日期" width="120" />
            <el-table-column prop="source" label="来源" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="getSourceType(row.source)">
                  {{ row.source }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="is_active" label="状态" width="80">
              <template #default="{ row }">
                <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
                  {{ row.is_active ? '启用' : '禁用' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="创建时间" width="180">
              <template #default="{ row }">
                {{ formatDateTime(row.created_at) }}
              </template>
            </el-table-column>
            <el-table-column label="操作" width="150">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="editRate(row)">
                  编辑
                </el-button>
                <el-button size="small" type="danger" @click="deleteRate(row.id)">
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </el-card>

    <!-- 添加/编辑汇率对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingRate ? '编辑汇率' : '添加汇率'"
      width="500px"
    >
      <el-form :model="rateForm" :rules="rules" ref="rateFormRef" label-width="100px">
        <el-form-item label="源币种" prop="from_currency">
          <el-select
            v-model="rateForm.from_currency"
            placeholder="请选择源币种"
            :disabled="editingRate"
          >
            <el-option
              v-for="curr in currencies"
              :key="curr.code"
              :label="`${curr.name} (${curr.code})`"
              :value="curr.code"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="目标币种" prop="to_currency">
          <el-select
            v-model="rateForm.to_currency"
            placeholder="请选择目标币种"
            :disabled="editingRate"
          >
            <el-option
              v-for="curr in currencies"
              :key="curr.code"
              :label="`${curr.name} (${curr.code})`"
              :value="curr.code"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="汇率" prop="rate">
          <el-input-number
            v-model="rateForm.rate"
            :precision="4"
            :step="0.0001"
            :min="0"
            placeholder="请输入汇率"
          />
        </el-form-item>

        <el-form-item label="生效日期" prop="effective_date">
          <el-date-picker
            v-model="rateForm.effective_date"
            type="date"
            placeholder="选择生效日期"
            format="YYYY-MM-DD"
            value-format="YYYY-MM-DD"
            :disabled="editingRate"
          />
        </el-form-item>

        <el-form-item label="来源" prop="source">
          <el-select v-model="rateForm.source" placeholder="请选择来源">
            <el-option label="手动输入" value="manual" />
            <el-option label="API获取" value="api" />
            <el-option label="系统默认" value="system" />
          </el-select>
        </el-form-item>

        <el-form-item label="状态" prop="is_active">
          <el-switch v-model="rateForm.is_active" />
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="mobile-dialog-footer">
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitRate" :loading="submitting">
          确定
        </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { CURRENCIES } from '@/utils/currency'

const latestRates = ref(null)
const rateHistory = ref([])
const dialogVisible = ref(false)
const refreshing = ref(false)
const submitting = ref(false)
const editingRate = ref(null)

const rateFormRef = ref(null)
const rateForm = ref({
  from_currency: '',
  to_currency: 'CNY',
  rate: null,
  effective_date: new Date().toISOString().split('T')[0],
  source: 'manual',
  is_active: true
})

const currencies = CURRENCIES

const rules = {
  from_currency: [{ required: true, message: '请选择源币种', trigger: 'change' }],
  to_currency: [{ required: true, message: '请选择目标币种', trigger: 'change' }],
  rate: [{ required: true, message: '请输入汇率', trigger: 'blur' }],
  effective_date: [{ required: true, message: '请选择生效日期', trigger: 'change' }],
  source: [{ required: true, message: '请选择来源', trigger: 'change' }]
}

// 加载最新汇率
const loadLatestRates = async () => {
  try {
    const response = await api.getLatestRates()
    latestRates.value = response.data
  } catch (error) {
    ElMessage.error('加载最新汇率失败')
    console.error(error)
  }
}

// 加载历史汇率
const loadRateHistory = async () => {
  try {
    const response = await api.getExchangeRates()
    rateHistory.value = response.data
  } catch (error) {
    ElMessage.error('加载汇率历史失败')
    console.error(error)
  }
}

// 从API刷新汇率
const refreshFromAPI = async () => {
  try {
    refreshing.value = true
    const response = await api.refreshRatesFromAPI()
    ElMessage.success(`成功更新 ${response.data.count} 个汇率`)
    await loadLatestRates()
    await loadRateHistory()
  } catch (error) {
    ElMessage.error('从API更新汇率失败: ' + (error.response?.data?.detail || error.message))
    console.error(error)
  } finally {
    refreshing.value = false
  }
}

// 显示添加对话框
const showAddDialog = () => {
  editingRate.value = null
  rateForm.value = {
    from_currency: '',
    to_currency: 'CNY',
    rate: null,
    effective_date: new Date().toISOString().split('T')[0],
    source: 'manual',
    is_active: true
  }
  dialogVisible.value = true
}

// 编辑汇率
const editRate = (rate) => {
  editingRate.value = rate
  rateForm.value = {
    from_currency: rate.from_currency,
    to_currency: rate.to_currency,
    rate: Number(rate.rate),
    effective_date: rate.effective_date,
    source: rate.source,
    is_active: rate.is_active
  }
  dialogVisible.value = true
}

// 提交汇率
const submitRate = async () => {
  if (!rateFormRef.value) return

  await rateFormRef.value.validate(async (valid) => {
    if (!valid) return

    try {
      submitting.value = true

      if (editingRate.value) {
        // 更新
        await api.updateExchangeRate(editingRate.value.id, {
          rate: rateForm.value.rate,
          source: rateForm.value.source,
          is_active: rateForm.value.is_active
        })
        ElMessage.success('汇率更新成功')
      } else {
        // 创建
        await api.createOrUpdateExchangeRate(rateForm.value)
        ElMessage.success('汇率添加成功')
      }

      dialogVisible.value = false
      await loadLatestRates()
      await loadRateHistory()
    } catch (error) {
      ElMessage.error('操作失败: ' + (error.response?.data?.detail || error.message))
      console.error(error)
    } finally {
      submitting.value = false
    }
  })
}

// 删除汇率
const deleteRate = async (id) => {
  try {
    await ElMessageBox.confirm('确定要删除这条汇率记录吗？', '警告', {
      type: 'warning'
    })

    await api.deleteExchangeRate(id)
    ElMessage.success('删除成功')
    await loadRateHistory()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('删除失败')
      console.error(error)
    }
  }
}

// 格式化日期
const formatDate = (dateStr) => {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleDateString('zh-CN')
}

// 格式化日期时间
const formatDateTime = (dateStr) => {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString('zh-CN')
}

// 获取来源类型
const getSourceType = (source) => {
  const types = {
    'api': 'success',
    'manual': 'warning',
    'system': 'info'
  }
  return types[source] || 'info'
}

onMounted(() => {
  loadLatestRates()
  loadRateHistory()
})
</script>

<style scoped>
.exchange-rates-page {
  width: 100%;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.current-rates {
  margin-bottom: 20px;
}

.current-rates h3,
.rate-history h3 {
  margin-bottom: 15px;
  font-size: 16px;
  font-weight: 600;
}

.currency-code {
  font-weight: bold;
  color: var(--app-primary);
  margin-right: 5px;
}

.rate-info {
  margin-top: 10px;
  display: flex;
  gap: 8px;
  justify-content: space-between;
  align-items: center;
}

.rate-history {
  margin-top: 20px;
}

:deep(.el-statistic__head) {
  font-size: 13px;
  color: var(--app-text-muted);
}

:deep(.el-statistic__content) {
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.022em;
}

@media (max-width: 900px) {
  .header-actions {
    width: 100%;
    justify-content: flex-start;
  }

  .rate-info {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
