<script setup lang="ts">
import { NTag } from 'naive-ui'
/**
 * AI 分析标签：风险等级 +（可选）观点标签 + 「可能过期」（#284：桌面 AI 列与移动卡片共用）。
 * 风险与分析标签复用详情页的文案和语义配色，未知风险为中性。
 */
import type { HoldingRow } from './useHoldingsTable'
import type { SecurityBadgesFeature } from './useSecurityBadges'
import { displayAnalysisTag } from './display'

defineProps<{
  badges: SecurityBadgesFeature
  row: HoldingRow
  /** 同时显示观点标签（移动卡片空间小，只显示风险与过期） */
  withTag?: boolean
  /** 风险标签上的 data-testid（移动卡片沿用 E2E 的 ai-tags 选择器） */
  riskTestId?: string
}>()

function naiveTagType(type: string) {
  return type === 'danger'
    ? 'error'
    : type === 'info' || !type
      ? 'default'
      : (type as 'success' | 'warning')
}
</script>

<template>
  <template v-if="badges.analysisFor(row)">
    <NTag
      :type="naiveTagType(badges.riskTagType(badges.analysisFor(row)!.risk_level))"
      size="small"
      :bordered="false"
      :data-testid="riskTestId"
    >
      风险 {{ badges.riskLabel(badges.analysisFor(row)!.risk_level) }}
    </NTag>
    <NTag
      v-if="withTag && displayAnalysisTag(badges.analysisFor(row)!.tags)"
      size="small"
      :bordered="true"
      :type="
        naiveTagType(badges.analysisTagType(displayAnalysisTag(badges.analysisFor(row)!.tags)!))
      "
    >
      {{ displayAnalysisTag(badges.analysisFor(row)!.tags) }}
    </NTag>
    <NTag
      v-if="badges.analysisOutdated(row)"
      size="small"
      :bordered="true"
      type="warning"
      data-testid="ai-outdated"
    >
      可能过期
    </NTag>
  </template>
</template>
