<script setup lang="ts">
/** 现价旁的「手工 / 陈价」标记（#284：桌面表格与移动卡片共用）。 */
defineProps<{ info: { manual?: boolean; stale?: boolean } | null | undefined }>()
</script>

<template>
  <el-tag
    v-if="info?.manual"
    type="info"
    size="small"
    effect="plain"
    class="price-flag"
    data-testid="price-manual-tag"
  >
    手工
  </el-tag>
  <el-tag
    v-if="info?.stale"
    type="warning"
    size="small"
    effect="plain"
    class="price-flag"
    data-testid="price-stale-tag"
  >
    陈价
  </el-tag>
</template>

<style scoped>
.price-flag {
  flex-shrink: 0;
}
</style>
