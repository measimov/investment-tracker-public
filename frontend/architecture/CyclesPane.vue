<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { CycleHint, SourceFile } from './types'

const props = defineProps<{
  cycles: CycleHint[] | undefined
  truncated: boolean
  files: SourceFile[]
}>()
const emit = defineEmits<{
  'open-source': [location: { path: string; line: number; sha256: string }]
}>()

const selectedKind = ref<CycleHint['kind']>('runtime')
const visibleLimit = ref(20)
const kinds: { value: CycleHint['kind']; label: string; description: string }[] = [
  {
    value: 'runtime',
    label: '运行时',
    description: '包含延迟 / 动态导入；静态闭环不表示程序启动一定失败。'
  },
  { value: 'type-only', label: '纯类型', description: '闭环中的全部导入均为类型依赖。' },
  { value: 'mixed', label: '混合', description: '闭环同时包含类型依赖和运行时依赖。' }
]
const selection = computed(() => kinds.find((kind) => kind.value === selectedKind.value)!)
const counts = computed(() => {
  const result = { runtime: 0, 'type-only': 0, mixed: 0 }
  for (const cycle of props.cycles ?? []) result[cycle.kind]++
  return result
})
const filtered = computed(() =>
  (props.cycles ?? []).filter((cycle) => cycle.kind === selectedKind.value)
)
const hashes = computed(() => new Map(props.files.map((file) => [file.path, file.sha256])))
watch([selectedKind, () => props.cycles], () => (visibleLimit.value = 20))

function evidenceLocation(edge: CycleHint['edges'][number]) {
  const sha256 = hashes.value.get(edge.source)
  return sha256 && edge.line !== null && Number.isInteger(edge.line) && edge.line >= 1
    ? { path: edge.source, line: edge.line, sha256 }
    : null
}

function openEvidence(edge: CycleHint['edges'][number]) {
  const location = evidenceLocation(edge)
  if (location) emit('open-source', location)
}

function kindLabel(kind: CycleHint['edges'][number]['kind']) {
  return { runtime: '运行时', type: '仅类型', dynamic: '延迟 / 动态' }[kind]
}
</script>

<template>
  <main id="cycles-workspace" class="cycles-pane" tabindex="-1" aria-labelledby="cycles-title">
    <header class="cycles-heading">
      <h2 id="cycles-title">循环依赖提示</h2>
      <span class="observation-label">观察项</span>
    </header>
    <p class="cycles-intro">
      分析器提供的原生闭环示例，非穷举结果。用于调查依赖关系，不作为检查失败条件。
    </p>
    <p v-if="truncated" class="cycles-truncated" role="status">
      结果已截断或未完整取得；以下仅展示已返回的部分闭环示例。
    </p>
    <p v-if="!cycles" class="cycles-empty">当前快照未提供循环分析结果，请重新扫描后查看。</p>
    <template v-else>
      <div class="cycles-filters" role="group" aria-label="循环类型">
        <button
          v-for="kind in kinds"
          :key="kind.value"
          type="button"
          :class="{ selected: selectedKind === kind.value }"
          :aria-pressed="selectedKind === kind.value"
          @click="selectedKind = kind.value"
        >
          {{ kind.label }} <span>{{ counts[kind.value] }}</span>
        </button>
      </div>
      <p class="cycles-description">{{ selection.description }}</p>
      <p v-if="!filtered.length" class="cycles-empty">
        当前快照没有返回{{ selection.label }}闭环示例。
      </p>
      <div v-else class="cycles-list">
        <details
          v-for="(cycle, index) in filtered.slice(0, visibleLimit)"
          :key="`${cycle.tool}:${cycle.kind}:${cycle.paths.join('|')}`"
          class="cycle-card"
          :open="index === 0"
        >
          <summary>
            <span class="cycle-number">示例 {{ index + 1 }}</span>
            <span class="cycle-start">{{ cycle.paths[0] }}</span>
            <span class="cycle-tool">{{ cycle.tool }}</span>
          </summary>
          <div class="cycle-evidence">
            <p class="cycle-path" aria-label="原生闭环路径">{{ cycle.paths.join(' → ') }}</p>
            <ol class="cycle-edges" aria-label="闭环导入证据">
              <li v-for="(edge, edgeIndex) in cycle.edges" :key="edgeIndex">
                <span class="edge-kind">{{ kindLabel(edge.kind) }}</span>
                <button
                  type="button"
                  class="evidence-link"
                  :disabled="!evidenceLocation(edge)"
                  :aria-label="`查看 ${edge.source}${edge.line === null ? '，原始行号未知' : ` 第 ${edge.line} 行`}`"
                  @click="openEvidence(edge)"
                >
                  {{ edge.source }}
                  <span>{{ edge.line === null ? ' · 原始行号未知' : `:${edge.line}` }}</span>
                </button>
                <p class="edge-target">导入 → {{ edge.target }}</p>
                <small v-if="!hashes.has(edge.source)" class="evidence-unavailable">
                  此文件不在当前源码索引中，无法定位。
                </small>
              </li>
            </ol>
          </div>
        </details>
      </div>
      <button
        v-if="filtered.length > visibleLimit"
        class="cycles-more"
        type="button"
        @click="visibleLimit += 20"
      >
        继续显示（已显示 {{ visibleLimit }} / {{ filtered.length }} 个示例）
      </button>
    </template>
  </main>
</template>

<style scoped>
.cycles-pane {
  padding: 24px;
  border: 1px solid var(--border, #e2e8e2);
  border-radius: 12px;
  background: var(--surface, #fcfdfb);
  color: var(--text, #243b35);
}

.cycles-heading {
  display: flex;
  align-items: center;
  gap: 12px;
}

.cycles-heading h2 {
  margin: 0;
  font-size: 18px;
}

.observation-label,
.cycle-tool,
.edge-kind {
  border-radius: 5px;
  background: #edf2ed;
  padding: 3px 7px;
  font-size: 12px;
}

.cycles-intro,
.cycles-description,
.cycles-empty,
.cycles-truncated {
  font-size: 13px;
  line-height: 1.8;
}

.cycles-intro,
.cycles-description,
.cycles-empty,
.evidence-unavailable {
  color: var(--muted, #75827b);
}

.cycles-truncated {
  padding: 10px 14px;
  border-left: 3px solid #b9914b;
  background: #f9f4e9;
  color: #775d2d;
}

.cycles-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 22px;
}

.cycles-filters button,
.cycles-more {
  min-height: 40px;
  padding: 8px 13px;
  border: 1px solid var(--border, #e2e8e2);
  border-radius: 7px;
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.cycles-filters button.selected {
  border-color: var(--accent, #176b58);
  background: #edf6f0;
  color: var(--accent, #176b58);
}

.cycles-filters span {
  margin-left: 4px;
  font-variant-numeric: tabular-nums;
}

.cycles-list {
  display: grid;
  gap: 10px;
}

.cycle-card {
  min-width: 0;
  border: 1px solid var(--border, #e2e8e2);
  border-radius: 8px;
}

.cycle-card summary {
  padding: 14px;
  cursor: pointer;
  font-size: 13px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}

.cycle-number {
  margin-right: 12px;
  color: var(--muted, #75827b);
}

.cycle-start {
  margin-right: 12px;
}

.cycle-evidence {
  padding: 0 18px 16px;
}

.cycle-path {
  margin: 0;
  padding: 12px 0;
  border-top: 1px solid var(--border, #e2e8e2);
  font-family: ui-monospace, monospace;
  font-size: 12px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}

.cycle-edges {
  margin: 0;
  padding-left: 20px;
  font-size: 12px;
}

.cycle-edges li {
  padding: 10px 0 6px;
}

.edge-kind {
  display: inline-block;
  margin-bottom: 4px;
}

.evidence-link {
  display: block;
  max-width: 100%;
  padding: 5px 0;
  border: 0;
  background: transparent;
  color: var(--accent, #176b58);
  font: inherit;
  line-height: 1.7;
  text-align: left;
  text-decoration: underline;
  text-underline-offset: 3px;
  overflow-wrap: anywhere;
  cursor: pointer;
}

.evidence-link:disabled {
  color: var(--muted, #75827b);
  text-decoration: none;
  cursor: default;
}

.edge-target {
  margin: 2px 0;
  line-height: 1.8;
  overflow-wrap: anywhere;
}

.evidence-unavailable {
  line-height: 1.8;
}

.cycles-more {
  margin-top: 16px;
}

@media (max-width: 600px) {
  .cycles-pane {
    padding: 16px 12px;
  }

  .cycle-card summary {
    padding: 12px;
  }

  .cycle-evidence {
    padding: 0 12px 12px;
  }
}
</style>
