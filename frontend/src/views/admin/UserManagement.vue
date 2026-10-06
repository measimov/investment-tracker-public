<template>
  <div class="user-management-page">
    <header class="page-heading">
      <div>
        <h1 class="page-title">用户管理</h1>
        <p class="page-intro page-description">维护用户资料、激活状态与管理员权限。</p>
      </div>
      <NButton type="primary" @click="handleAdd">添加用户</NButton>
    </header>
    <section class="users-section" aria-label="用户列表" :aria-busy="loading">
      <div class="section-heading">
        <h2>
          当前列表 <span>{{ hasLoaded ? `${users.length} 位用户` : '—' }}</span>
        </h2>
        <NButton :loading="loading" aria-label="重新加载用户列表" @click="loadUsers"
          >重新加载</NButton
        >
      </div>
      <NAlert v-if="loadError" type="warning" :show-icon="false" title="用户列表加载失败">
        {{
          hasLoaded ? '保留上次成功的用户列表，尚未确认最新结果。' : '尚未确认用户列表，请重试。'
        }}
        <NButton text type="primary" aria-label="重试加载用户列表" @click="loadUsers"
          >重试加载</NButton
        >
      </NAlert>
      <p v-else-if="loading && hasLoaded" class="read-note" role="status">
        正在重新加载，以下为上次成功的用户列表。
      </p>
      <p v-if="users.length >= 100" class="read-note">
        当前最多显示 100 位用户，不代表全部用户数量。
      </p>
      <UsersTable
        :users="users"
        :loading="loading"
        :empty-description="emptyDescription"
        @edit="handleEdit"
        @reset-password="handleResetPassword"
        @delete="handleDelete"
      />
    </section>

    <!-- Create/Edit User Dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑用户' : '添加用户'"
      width="560px"
      :close-on-click-modal="false"
      :close-on-press-escape="!submitting"
      :show-close="!submitting"
      @closed="resetForm"
    >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="form.username"
            aria-label="用户名"
            placeholder="请输入用户名"
            :disabled="isEdit"
          />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input
            v-model="form.email"
            aria-label="邮箱"
            placeholder="选填"
            type="email"
            clearable
          />
        </el-form-item>
        <el-form-item label="密码" prop="password" v-if="!isEdit">
          <el-input
            v-model="form.password"
            aria-label="密码"
            placeholder="请输入密码"
            type="password"
            show-password
          />
        </el-form-item>
        <el-form-item>
          <NCheckbox
            v-model:checked="form.is_active"
            aria-label="激活状态"
            :disabled="submitting"
            :aria-disabled="submitting"
            >激活状态</NCheckbox
          >
        </el-form-item>
        <el-form-item>
          <NCheckbox
            v-model:checked="form.is_admin"
            aria-label="管理员权限"
            :disabled="submitting"
            :aria-disabled="submitting"
            >管理员权限</NCheckbox
          >
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button :disabled="submitting" @click="dialogVisible = false">取消</el-button>
          <NButton
            type="primary"
            @click="handleSubmit"
            aria-label="保存"
            :loading="submitting"
            :aria-busy="submitting"
            :aria-disabled="submitting"
            >保存</NButton
          >
        </div>
      </template>
    </el-dialog>

    <!-- Reset Password Dialog -->
    <el-dialog
      v-model="resetPasswordVisible"
      title="重置密码"
      width="420px"
      :close-on-click-modal="false"
      :close-on-press-escape="!resettingPassword"
      :show-close="!resettingPassword"
      @closed="clearResetPassword"
    >
      <el-form
        :model="resetPasswordForm"
        :rules="resetPasswordRules"
        ref="resetPasswordFormRef"
        label-width="100px"
      >
        <el-form-item label="新密码" prop="new_password">
          <el-input
            v-model="resetPasswordForm.new_password"
            aria-label="新密码"
            placeholder="请输入新密码"
            type="password"
            show-password
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm_password">
          <el-input
            v-model="resetPasswordForm.confirm_password"
            aria-label="确认密码"
            placeholder="请再次输入新密码"
            type="password"
            show-password
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button :disabled="resettingPassword" @click="resetPasswordVisible = false"
            >取消</el-button
          >
          <NButton
            type="primary"
            @click="handleResetPasswordSubmit"
            aria-label="重置密码"
            :loading="resettingPassword"
            :aria-busy="resettingPassword"
            :aria-disabled="resettingPassword"
            >重置密码</NButton
          >
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { makeConfirmedAction } from '@/composables/useConfirmAction'
import { ref, reactive, computed, onMounted } from 'vue'
import { NAlert, NButton, NCheckbox } from 'naive-ui'
import { useLatestRequest } from '@/composables/useLatestRequest'
import UsersTable from './users/UsersTable.vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import api from '../../api'
import type { User } from '../../types'
import { showApiError } from '../../utils/showApiError'

// 后端 User schema 为准（此前手写副本把 email 写成必填非空，已漂移）
type UserRow = User

type ValidatorCallback = (error?: Error) => void

const loading = ref(false)
const hasLoaded = ref(false)
const loadError = ref(false)
const usersRequest = useLatestRequest()
const emptyDescription = computed(() =>
  loading.value ? '正在加载用户列表' : !hasLoaded.value ? '用户列表尚未加载成功' : '暂无用户'
)
const users = ref<UserRow[]>([])
const dialogVisible = ref(false)
const isEdit = ref(false)
const formRef = ref<FormInstance | null>(null)
const submitting = ref(false)
const resetPasswordVisible = ref(false)
const resetPasswordFormRef = ref<FormInstance | null>(null)
const resettingPassword = ref(false)
const currentUserId = ref<number | null>(null)

const form = reactive<{
  id?: number
  username: string
  email: string
  password: string
  is_active: boolean
  is_admin: boolean
}>({
  username: '',
  email: '',
  password: '',
  is_active: true,
  is_admin: false
})

const resetPasswordForm = reactive({
  new_password: '',
  confirm_password: ''
})

// 必须与后端 schemas/user.py 的 MIN_PASSWORD_LENGTH 一致：前端卡得比后端松
// 的话，用户过了这层校验再吃一个 422，错在哪只能猜。
const MIN_PASSWORD_LENGTH = 10

const validatePassword = (rule: unknown, value: string, callback: ValidatorCallback) => {
  if (!value) {
    callback(new Error('请输入密码'))
  } else if (value.length < MIN_PASSWORD_LENGTH) {
    callback(new Error(`密码长度至少${MIN_PASSWORD_LENGTH}位`))
  } else {
    callback()
  }
}

const validateConfirmPassword = (rule: unknown, value: string, callback: ValidatorCallback) => {
  if (!value) {
    callback(new Error('请再次输入密码'))
  } else if (value !== resetPasswordForm.new_password) {
    callback(new Error('两次输入的密码不一致'))
  } else {
    callback()
  }
}

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 50, message: '用户名长度应为3-50个字符', trigger: 'blur' }
  ],
  // 邮箱非必填（后端 Optional）：只在填了的时候校验格式。此前必填，
  // 编辑没有邮箱的种子用户时只能编造一个
  email: [{ type: 'email', message: '请输入有效的邮箱地址', trigger: 'blur' }],
  password: [{ required: true, validator: validatePassword, trigger: 'blur' }]
}

const resetPasswordRules: FormRules = {
  new_password: [{ required: true, validator: validatePassword, trigger: 'blur' }],
  confirm_password: [{ required: true, validator: validateConfirmPassword, trigger: 'blur' }]
}

async function loadUsers() {
  const request = usersRequest.begin()
  loading.value = true
  loadError.value = false
  try {
    const response = await api.getUsers()
    if (!usersRequest.isCurrent(request)) return
    users.value = response.data
    hasLoaded.value = true
  } catch (error) {
    if (!usersRequest.isCurrent(request)) return
    loadError.value = true
    showApiError(error, '加载用户列表失败')
  } finally {
    if (usersRequest.isCurrent(request)) loading.value = false
  }
}

function handleAdd() {
  isEdit.value = false
  resetForm()
  dialogVisible.value = true
}

function handleEdit(row: UserRow) {
  isEdit.value = true
  Object.assign(form, {
    id: row.id,
    username: row.username,
    email: row.email ?? '',
    is_active: row.is_active,
    is_admin: row.is_admin
  })
  // 上一次校验的红字不带进本次编辑
  formRef.value?.clearValidate()
  dialogVisible.value = true
}

async function handleSubmit() {
  if (submitting.value) return
  // validate() 校验不通过时是 reject 而不是返回 false：不接住会变成未处理的 Promise 拒绝
  try {
    await formRef.value?.validate()
  } catch {
    return
  }

  if (submitting.value) return

  // 空邮箱发 null：空串过不了后端 EmailStr（422）；编辑时显式 null = 清空邮箱
  const email = form.email.trim() || null
  submitting.value = true
  try {
    if (isEdit.value) {
      const updateData = {
        email,
        is_active: form.is_active,
        is_admin: form.is_admin
      }
      await api.updateUser(form.id as number, updateData)
      ElMessage.success('用户已更新')
    } else {
      await api.createUser({
        username: form.username,
        email,
        password: form.password,
        is_active: form.is_active,
        is_admin: form.is_admin
      })
      ElMessage.success('用户已新增')
    }
    dialogVisible.value = false
    loadUsers()
  } catch (error) {
    // 422 的 detail 是数组，getApiErrorMessage（showApiError 内部）会拼成一句话
    showApiError(error, isEdit.value ? '更新用户失败' : '创建用户失败')
  } finally {
    submitting.value = false
  }
}

function clearResetPassword() {
  currentUserId.value = null
  resetPasswordForm.new_password = ''
  resetPasswordForm.confirm_password = ''
  resetPasswordFormRef.value?.clearValidate()
}

function handleResetPassword(row: UserRow) {
  clearResetPassword()
  currentUserId.value = row.id
  resetPasswordVisible.value = true
}

async function handleResetPasswordSubmit() {
  if (resettingPassword.value) return
  try {
    await resetPasswordFormRef.value?.validate()
  } catch {
    return
  }

  if (resettingPassword.value) return
  resettingPassword.value = true
  try {
    await api.resetUserPassword(currentUserId.value as number, resetPasswordForm.new_password)
    ElMessage.success('密码已重置')
    resetPasswordVisible.value = false
  } catch (error) {
    showApiError(error, '重置密码失败')
  } finally {
    resettingPassword.value = false
  }
}

const handleDelete = makeConfirmedAction<UserRow>({
  title: '删除用户',
  message: (row) =>
    `删除用户「${row.username}」将一并删除其券商账户、交易、持仓、现金事件、公司行动、导入和月末核对记录等账本数据。此操作无法撤销，确定删除吗？`,
  confirmText: '删除',
  request: (row) => api.deleteUser(row.id),
  successMessage: '用户已删除',
  failureMessage: '删除用户失败',
  reload: () => loadUsers()
})

function resetForm() {
  // 清掉 id：否则编辑过某个用户后再点「添加」，表单还带着那个用户的 id
  Object.assign(form, {
    id: undefined,
    username: '',
    email: '',
    password: '',
    is_active: true,
    is_admin: false
  })
  formRef.value?.clearValidate()
}

onMounted(() => {
  loadUsers()
})
</script>

<style scoped>
.user-management-page {
  width: 100%;
  min-width: 0;
}
.page-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  margin-bottom: 28px;
}
.users-section {
  min-width: 0;
}
.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}
h2 {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}
h2 span {
  margin-left: 12px;
  font-size: 13px;
  color: var(--app-text-muted);
  font-weight: 400;
}
.read-note {
  color: var(--app-text-muted);
  font-size: 13px;
  line-height: 1.7;
}
.users-section :deep(.n-alert) {
  margin-bottom: 16px;
}
.user-management-page :deep(.el-input) {
  --el-input-placeholder-color: var(--app-text-soft);
}
.user-management-page :deep(.n-checkbox) {
  min-height: 24px;
}
@media (max-width: 640px) {
  .page-heading {
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 16px;
    margin-bottom: 24px;
  }
  .page-heading :deep(.n-button),
  .section-heading :deep(.n-button),
  .users-section :deep(.n-alert .n-button) {
    min-height: 44px;
  }
  .user-management-page :deep(.el-input__wrapper),
  .mobile-dialog-footer :deep(.el-button),
  .mobile-dialog-footer :deep(.n-button) {
    min-height: 44px;
  }
  .user-management-page :deep(.n-checkbox) {
    min-height: 44px;
  }
}

@media (min-width: 1025px) {
  .page-heading {
    margin-bottom: 16px;
  }
  h2 {
    font-size: 18px;
  }
}
</style>
