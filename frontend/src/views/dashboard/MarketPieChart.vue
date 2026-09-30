<template>
  <v-chart :option="option" autoresize />
</template>

<script setup lang="ts">
// 仪表盘的市场分布饼图：echarts 约 190KB（gzip），首页首屏不需要它——
// 由 Dashboard 以 defineAsyncComponent 引入，数据到齐后才下载（#285）
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { PieChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import type { EChartsCoreOption } from 'echarts/core'

use([CanvasRenderer, PieChart, TitleComponent, TooltipComponent, LegendComponent])

defineProps<{ option: EChartsCoreOption }>()
</script>
