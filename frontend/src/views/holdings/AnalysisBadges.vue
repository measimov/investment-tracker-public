<script setup lang="ts">
/**
 * AI 分析标签：风险等级 +（可选）观点标签 + 「可能过期」（#284：桌面 AI 列与移动卡片共用）。
 * 风险用 success/warning/danger，观点标签用中性 info：两者不再同为橙色。
 */
import type { HoldingRow } from './useHoldingsTable'
import type { SecurityBadgesFeature } from './useSecurityBadges'
import { displayAnalysisTag } from './display'

const props = defineProps<{
  badges: SecurityBadgesFeature
  row: HoldingRow
  /** 同时显示观点标签（移动卡片空间小，只显示风险与过期） */
  withTag?: boolean
  /** 风险标签上的 data-testid（移动卡片沿用 E2E 的 ai-tags 选择器） */
  riskTestId?: string
}>()

function riskText(level: string) {
  return `${props.badges.riskLabels[level] || level}风险`
}
</script>

<template>
  <template v-if="badges.analysisFor(row)">
    <el-tag
      :type="badges.riskTagType(badges.analysisFor(row)!.risk_level)"
      size="small"
      effect="light"
      :data-testid="riskTestId"
    >
      {{ riskText(badges.analysisFor(row)!.risk_level) }}
    </el-tag>
    <el-tag
      v-if="withTag && displayAnalysisTag(badges.analysisFor(row)!.tags)"
      size="small"
      effect="plain"
      type="info"
    >
      {{ displayAnalysisTag(badges.analysisFor(row)!.tags) }}
    </el-tag>
    <el-tag
      v-if="badges.analysisOutdated(row)"
      size="small"
      effect="plain"
      type="warning"
      data-testid="ai-outdated"
    >
      可能过期
    </el-tag>
  </template>
</template>
