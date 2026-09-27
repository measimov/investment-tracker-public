<template>
  <el-dialog
    :model-value="modelValue"
    title="修改密码"
    width="420px"
    append-to-body
    @update:model-value="emit('update:modelValue', $event)"
    @closed="resetForm"
  >
    <el-form
      ref="formRef"
      :model="form"
      :rules="rules"
      label-width="90px"
      data-testid="change-password-form"
      @submit.prevent
    >
      <el-form-item label="原密码" prop="old_password">
        <el-input
          v-model="form.old_password"
          type="password"
          placeholder="请输入当前密码"
          autocomplete="current-password"
          show-password
        />
      </el-form-item>
      <el-form-item label="新密码" prop="new_password">
        <el-input
          v-model="form.new_password"
          type="password"
          :placeholder="`至少 ${MIN_PASSWORD_LENGTH} 位`"
          autocomplete="new-password"
          show-password
        />
      </el-form-item>
      <el-form-item label="确认密码" prop="confirm_password">
        <el-input
          v-model="form.confirm_password"
          type="password"
          placeholder="请再次输入新密码"
          autocomplete="new-password"
          show-password
          @keyup.enter="submit"
        />
      </el-form-item>
    </el-form>
    <el-alert
      title="修改成功后，所有设备上的登录会话都会退出，需要用新密码重新登录。"
      type="info"
      :closable="false"
      show-icon
    />
    <template #footer>
      <div class="mobile-dialog-footer">
        <el-button @click="emit('update:modelValue', false)">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">确认修改</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import { showApiError } from '../utils/showApiError'

defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

// 必须与后端 schemas/user.py 的 MIN_PASSWORD_LENGTH 一致（UserManagement.vue 同一常量）
const MIN_PASSWORD_LENGTH = 10

type ValidatorCallback = (error?: Error) => void

const router = useRouter()
const authStore = useAuthStore()
const formRef = ref<FormInstance | null>(null)
const submitting = ref(false)
const form = reactive({ old_password: '', new_password: '', confirm_password: '' })

const rules: FormRules = {
  old_password: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  new_password: [
    {
      required: true,
      trigger: 'blur',
      validator: (_rule: unknown, value: string, callback: ValidatorCallback) => {
        if (!value) callback(new Error('请输入新密码'))
        else if (value.length < MIN_PASSWORD_LENGTH)
          callback(new Error(`新密码长度至少${MIN_PASSWORD_LENGTH}位`))
        else if (value === form.old_password) callback(new Error('新密码不能与原密码相同'))
        else callback()
      }
    }
  ],
  confirm_password: [
    {
      required: true,
      trigger: 'blur',
      validator: (_rule: unknown, value: string, callback: ValidatorCallback) => {
        if (!value) callback(new Error('请再次输入新密码'))
        else if (value !== form.new_password) callback(new Error('两次输入的密码不一致'))
        else callback()
      }
    }
  ]
}

function resetForm() {
  form.old_password = ''
  form.new_password = ''
  form.confirm_password = ''
  formRef.value?.clearValidate()
}

async function submit() {
  if (submitting.value) return
  try {
    await formRef.value?.validate()
  } catch {
    return // 校验失败：错误已显示在输入框下方
  }

  submitting.value = true
  try {
    await api.changePassword(form.old_password, form.new_password)
  } catch (error) {
    showApiError(error, '修改密码失败')
    return
  } finally {
    submitting.value = false
  }

  // 后端已吊销该用户的全部会话（含当前这个）：只清本地状态，不再调登出接口
  authStore.endLocalSession()
  emit('update:modelValue', false)
  ElMessage.success('密码已修改，已退出所有会话，请重新登录')
  router.push('/login')
}
</script>
