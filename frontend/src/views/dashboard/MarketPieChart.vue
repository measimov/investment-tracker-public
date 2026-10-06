<template>
  <v-chart
    :update-options="{ notMerge: false, replaceMerge: ['series'] }"
    :option="fontOption"
    autoresize
  />
</template>

<script setup lang="ts">
// 仪表盘的市场分布饼图：echarts 约 190KB（gzip），首页首屏不需要它——
// 由 Dashboard 以 defineAsyncComponent 引入，数据到齐后才下载（#285）
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { PieChart, type PieSeriesOption } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { useChartFontOption } from '@/composables/useChartFontOption'
import type { EChartsCoreOption } from 'echarts/core'

use([CanvasRenderer, PieChart, TitleComponent, TooltipComponent, LegendComponent])

const props = defineProps<{ option: EChartsCoreOption }>()
const fontOption = useChartFontOption(
  (fonts) => ({
    ...props.option,
    series: (props.option.series as PieSeriesOption[]).map((series) => ({
      ...series,
      label: {
        ...series.label,
        rich: {
          ...series.label?.rich,
          name: { ...series.label?.rich?.name, fontFamily: fonts.text },
          value: { ...series.label?.rich?.value, fontFamily: fonts.number }
        }
      }
    }))
  }),
  { serifNumbers: true }
)
</script>
