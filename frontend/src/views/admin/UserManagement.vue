<template>
  <div class="user-management-page">
    <el-card>
      <template #header>
        <div class="page-header">
          <span>用户管理</span>
          <div class="header-actions">
            <el-button type="primary" :icon="Plus" @click="handleAdd">添加用户</el-button>
          </div>
        </div>
      </template>

      <!-- Users Table -->
      <div class="responsive-table">
        <el-table :data="users" v-loading="loading" stripe>
          <el-table-column prop="id" label="ID" width="80" />
          <el-table-column prop="username" label="用户名" min-width="130" show-overflow-tooltip />
          <el-table-column prop="email" label="邮箱" min-width="170" show-overflow-tooltip>
            <template #default="{ row }">{{ row.email || EMPTY }}</template>
          </el-table-column>
          <el-table-column prop="is_active" label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="row.is_active ? 'success' : 'danger'" size="small">
                {{ row.is_active ? '激活' : '禁用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="is_admin" label="管理员" width="100">
            <template #default="{ row }">
              <el-tag :type="row.is_admin ? 'warning' : 'info'" size="small">
                {{ row.is_admin ? '是' : '否' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间" min-width="150" sortable>
            <template #default="{ row }">
              {{ formatDateTime(row.created_at) }}
            </template>
          </el-table-column>
          <el-table-column label="操作" width="280" fixed="right">
            <template #default="{ row }">
              <el-button type="primary" size="small" text @click="handleEdit(row)">编辑</el-button>
              <el-button type="warning" size="small" text @click="handleResetPassword(row)"
                >重置密码</el-button
              >
              <el-button type="danger" size="small" text @click="handleDelete(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>

    <!-- Create/Edit User Dialog -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑用户' : '添加用户'" width="560px">
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" :disabled="isEdit" />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input v-model="form.email" placeholder="选填" type="email" clearable />
        </el-form-item>
        <el-form-item label="密码" prop="password" v-if="!isEdit">
          <el-input
            v-model="form.password"
            placeholder="请输入密码"
            type="password"
            show-password
          />
        </el-form-item>
        <el-form-item label="激活状态">
          <el-switch v-model="form.is_active" />
        </el-form-item>
        <el-form-item label="管理员权限">
          <el-switch v-model="form.is_admin" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" @click="handleSubmit" :loading="submitting">确定</el-button>
        </div>
      </template>
    </el-dialog>

    <!-- Reset Password Dialog -->
    <el-dialog v-model="resetPasswordVisible" title="重置密码" width="420px">
      <el-form
        :model="resetPasswordForm"
        :rules="resetPasswordRules"
        ref="resetPasswordFormRef"
        label-width="100px"
      >
        <el-form-item label="新密码" prop="new_password">
          <el-input
            v-model="resetPasswordForm.new_password"
            placeholder="请输入新密码"
            type="password"
            show-password
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm_password">
          <el-input
            v-model="resetPasswordForm.confirm_password"
            placeholder="请再次输入新密码"
            type="password"
            show-password
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="mobile-dialog-footer">
          <el-button @click="resetPasswordVisible = false">取消</el-button>
          <el-button type="primary" @click="handleResetPasswordSubmit" :loading="resettingPassword"
            >确定</el-button
          >
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import api from '../../api'
import type { User } from '../../types'
import { EMPTY, formatDateTime } from '../../utils/helpers'
import { showApiError } from '../../utils/showApiError'

// 后端 User schema 为准（此前手写副本把 email 写成必填非空，已漂移）
type UserRow = User

type ValidatorCallback = (error?: Error) => void

const loading = ref(false)
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
  loading.value = true
  try {
    const response = await api.getUsers()
    users.value = response.data
  } catch (error) {
    showApiError(error, '加载用户列表失败')
  } finally {
    loading.value = false
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
  // validate() 校验不通过时是 reject 而不是返回 false：不接住会变成未处理的 Promise 拒绝
  try {
    await formRef.value?.validate()
  } catch {
    return
  }

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
      ElMessage.success('更新用户成功')
    } else {
      await api.createUser({
        username: form.username,
        email,
        password: form.password,
        is_active: form.is_active,
        is_admin: form.is_admin
      })
      ElMessage.success('创建用户成功')
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

function handleResetPassword(row: UserRow) {
  currentUserId.value = row.id
  resetPasswordForm.new_password = ''
  resetPasswordForm.confirm_password = ''
  resetPasswordFormRef.value?.clearValidate()
  resetPasswordVisible.value = true
}

async function handleResetPasswordSubmit() {
  try {
    await resetPasswordFormRef.value?.validate()
  } catch {
    return
  }

  resettingPassword.value = true
  try {
    await api.resetUserPassword(currentUserId.value as number, resetPasswordForm.new_password)
    ElMessage.success('重置密码成功')
    resetPasswordVisible.value = false
  } catch (error) {
    showApiError(error, '重置密码失败')
  } finally {
    resettingPassword.value = false
  }
}

function handleDelete(row: UserRow) {
  ElMessageBox.confirm(`确定要删除用户 "${row.username}" 吗？`, '提示', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning'
  })
    .then(async () => {
      try {
        await api.deleteUser(row.id)
        ElMessage.success('删除用户成功')
        loadUsers()
      } catch (error) {
        showApiError(error, '删除用户失败')
      }
    })
    .catch(() => {
      // User cancelled
    })
}

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
}

@media (max-width: 900px) {
  .header-actions {
    width: 100%;
  }
}
</style>
