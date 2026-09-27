<template>
  <div class="not-found-page">
    <el-result icon="warning" title="页面不存在" :sub-title="`没有找到「${route.path}」对应的页面`">
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
  padding: 48px 0;
}
</style>
