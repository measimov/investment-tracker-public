<template>
  <div class="not-found-page">
    <el-result icon="warning" :sub-title="`没有找到「${route.path}」对应的页面`">
      <template #title>
        <h1>页面不存在</h1>
      </template>
      <template #extra>
        <el-button type="primary" @click="router.push('/')">返回首页</el-button>
      </template>
    </el-result>
  </div>
</template>

<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
</script>

<style scoped>
.not-found-page {
  display: flex;
  justify-content: center;
  min-width: 0;
  max-width: 100%;
  padding: 48px 16px;
}
.not-found-page :deep(.el-result) {
  min-width: 0;
  max-width: 100%;
}
.not-found-page :deep(.el-result__subtitle) {
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
}
h1 {
  margin: 0;
  font-family: var(--app-font-serif);
  font-size: 32px;
  line-height: 1.3;
  font-weight: 600;
}
</style>
