<template>
  <div class="login-container">
    <section class="login-shell login-card">
      <div class="login-brand">
        <span class="brand-mark" aria-hidden="true">
          <span></span>
          <span></span>
          <span></span>
        </span>
        <div>
          <h1>投资追踪系统</h1>
          <p>用户登录</p>
        </div>
      </div>

      <el-form
        ref="loginFormRef"
        class="login-form"
        :model="loginForm"
        :rules="rules"
        label-position="top"
        @submit.prevent="handleLogin"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="loginForm.username"
            placeholder="请输入用户名"
            :prefix-icon="User"
            size="large"
          />
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="loginForm.password"
            type="password"
            placeholder="请输入密码"
            :prefix-icon="Lock"
            size="large"
            show-password
            @keyup.enter="handleLogin"
          />
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            size="large"
            :loading="loading"
            @click="handleLogin"
            style="width: 100%"
          >
            {{ loading ? '登录中...' : '登录' }}
          </el-button>
        </el-form-item>

        <el-alert
          v-if="errorMessage"
          :title="errorMessage"
          type="error"
          show-icon
          :closable="false"
          style="margin-top: 10px"
        />
      </el-form>

      <component :is="LoginMockHint" v-if="LoginMockHint" @fill-account="fillAccount" />
    </section>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { User, Lock } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const USE_MOCK = import.meta.env.MODE === 'mock' && import.meta.env.VITE_USE_MOCK === 'true'
const LoginMockHint = USE_MOCK
  ? defineAsyncComponent(() => import('../components/LoginMockHint.vue'))
  : null

const router = useRouter()
const authStore = useAuthStore()

const loginFormRef = ref(null)
const loading = ref(false)
const errorMessage = ref('')

const loginForm = reactive({
  username: '',
  password: ''
})

const fillAccount = (username, password) => {
  loginForm.username = username
  loginForm.password = password
}

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码长度至少为6位', trigger: 'blur' }
  ]
}

const handleLogin = async () => {
  if (!loginFormRef.value) return

  try {
    // Validate form
    await loginFormRef.value.validate()

    loading.value = true
    errorMessage.value = ''

    // Attempt login
    const result = await authStore.login(loginForm.username, loginForm.password)

    if (result.success) {
      ElMessage.success('登录成功')
      // Redirect to home page
      router.push('/')
    } else {
      errorMessage.value = result.message
    }
  } catch (error) {
    // Form validation failed
    console.error('Login error:', error)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: calc(100vh - 52px);
  padding: 48px 20px;
}

.login-shell {
  width: 100%;
  max-width: 400px;
  padding: 36px;
  background: var(--app-surface);
  border-radius: 14px;
  box-shadow: var(--app-shadow-md);
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 20px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--app-separator);
}

.brand-mark {
  display: grid;
  grid-template-columns: repeat(3, 6px);
  align-items: end;
  gap: 3px;
  width: 34px;
  height: 34px;
  padding: 6px;
  background: var(--app-surface-secondary);
  border: 1px solid var(--app-border);
  border-radius: 8px;
}

.brand-mark span {
  display: block;
  width: 6px;
  border-radius: 2px 2px 0 0;
}

.brand-mark span:nth-child(1) {
  height: 13px;
  background: var(--app-success);
}

.brand-mark span:nth-child(2) {
  height: 20px;
  background: var(--app-primary);
}

.brand-mark span:nth-child(3) {
  height: 10px;
  background: var(--app-danger);
}

.login-brand h1 {
  margin: 0 0 3px;
  color: var(--app-text);
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.022em;
  line-height: 1.2;
}

.login-brand p {
  margin: 0;
  color: var(--app-text-muted);
  font-size: 14px;
}

.login-form :deep(.el-form-item) {
  margin-bottom: 18px;
}

.login-form :deep(.el-form-item__label) {
  justify-content: flex-start;
  padding-bottom: 4px;
  color: var(--app-text-muted);
  font-weight: 500;
  font-size: 13px;
}

.login-form :deep(.el-button) {
  height: 44px;
  font-size: 16px;
  font-weight: 500;
}

@media (max-width: 640px) {
  .login-container {
    align-items: flex-start;
    padding-top: 24px;
  }

  .login-shell {
    padding: 24px;
  }
}
</style>
