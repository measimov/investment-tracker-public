<script setup lang="ts">
import { NTag } from 'naive-ui'
/** 现价旁的「手工 / 陈价」标记（#284：桌面表格与移动卡片共用）。 */
defineProps<{ info: { manual?: boolean; stale?: boolean } | null | undefined }>()
</script>

<template>
  <NTag
    v-if="info?.manual"
    type="default"
    size="small"
    :bordered="true"
    class="price-flag"
    data-testid="price-manual-tag"
  >
    手工
  </NTag>
  <NTag
    v-if="info?.stale"
    type="warning"
    size="small"
    :bordered="true"
    class="price-flag"
    data-testid="price-stale-tag"
  >
    陈价
  </NTag>
</template>

<style scoped>
.price-flag {
  flex-shrink: 0;
}
</style>
