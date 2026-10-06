<template>
  <div class="login-container">
    <section class="login-shell login-card">
      <div class="login-brand">
        <span class="brand-mark" aria-hidden="true"></span>
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
            autocomplete="username"
            placeholder="请输入用户名"
            :prefix-icon="User"
            size="large"
          />
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="loginForm.password"
            type="password"
            autocomplete="current-password"
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
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { UserRound as User, LockKeyhole as Lock } from '@lucide/vue'
import { ElMessage, type FormInstance } from 'element-plus'

const router = useRouter()
const route = useRoute()

// 只接受站内路径（单个 / 开头）：query 可被外部拼接，'//evil.com' 这类
// 协议相对地址会变成开放重定向
function safeRedirectTarget(): string {
  const target = route.query.redirect
  if (typeof target === 'string' && target.startsWith('/') && !target.startsWith('//')) {
    return target
  }
  return '/'
}
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
  // 登录不校验长度：口令下限只约束新设口令（后端 MIN_PASSWORD_LENGTH=10），
  // 种子用户的初始口令来自环境变量、不受它约束——这里卡长度会把人挡在门外
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

const handleLogin = async () => {
  if (!loginFormRef.value) return

  try {
    await loginFormRef.value.validate()
  } catch {
    return // 表单校验未通过：错误已显示在输入框下方
  }

  loading.value = true
  errorMessage.value = ''
  try {
    const result = await authStore.login(loginForm.username, loginForm.password)

    if (result.success) {
      // 回到会话过期前的页面；无 redirect 时回首页
      await router.push(safeRedirectTarget())
      ElMessage.success('登录成功')
    } else if (!result.globallyNotified) {
      // 用户名密码错误、账号停用等只在表单下方提示一次；
      // 5xx/断网已由全局通知提示，不再重复
      errorMessage.value = result.message
    }
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
  background: var(--app-bg);
}

.login-shell {
  width: 100%;
  max-width: 400px;
  padding: 32px;
  background: var(--app-surface);
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-radius);
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
  display: block;
  flex: 0 0 38px;
  width: 38px;
  height: 38px;
  background-color: var(--app-primary);
  -webkit-mask: url('../assets/brand-mark.png') center / contain no-repeat;
  mask: url('../assets/brand-mark.png') center / contain no-repeat;
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
  height: 40px;
  font-size: 15px;
  font-weight: 600;
  border-radius: var(--app-radius-inner);
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
