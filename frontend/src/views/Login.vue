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
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { User, Lock } from '@element-plus/icons-vue'
import { ElMessage, type FormInstance } from 'element-plus'

const router = useRouter()
const authStore = useAuthStore()

const loginFormRef = ref<FormInstance | null>(null)
const loading = ref(false)
const errorMessage = ref('')

const loginForm = reactive({
  username: '',
  password: ''
})

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
  background:
    radial-gradient(720px 380px at 15% 8%, rgba(99, 102, 241, 0.16), transparent 60%),
    radial-gradient(640px 340px at 88% 16%, rgba(139, 92, 246, 0.14), transparent 55%),
    radial-gradient(560px 380px at 50% 105%, rgba(16, 185, 129, 0.1), transparent 55%);
}

.login-shell {
  width: 100%;
  max-width: 400px;
  padding: 36px;
  background: rgba(255, 255, 255, 0.78);
  border: 1px solid rgba(255, 255, 255, 0.7);
  border-radius: 22px;
  box-shadow: var(--app-shadow-lg);
  backdrop-filter: saturate(160%) blur(24px);
  -webkit-backdrop-filter: saturate(160%) blur(24px);
  animation: loginIn 0.5s var(--apple-spring);
}

@keyframes loginIn {
  from {
    opacity: 0;
    transform: translateY(14px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
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
  width: 38px;
  height: 38px;
  padding: 7px;
  background: var(--app-primary-gradient);
  border: none;
  border-radius: 11px;
  box-shadow: 0 6px 14px -3px var(--app-primary-shadow);
}

.brand-mark span {
  display: block;
  width: 6px;
  border-radius: 2px 2px 0 0;
  background: rgba(255, 255, 255, 0.95);
}

.brand-mark span:nth-child(1) {
  height: 14px;
  opacity: 0.75;
}

.brand-mark span:nth-child(2) {
  height: 22px;
}

.brand-mark span:nth-child(3) {
  height: 10px;
  opacity: 0.6;
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
  height: 46px;
  font-size: 16px;
  font-weight: 600;
  border-radius: 12px;
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
