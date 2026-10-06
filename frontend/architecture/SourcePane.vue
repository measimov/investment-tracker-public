<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { SourceSymbol } from './types'

const props = defineProps<{
  filePath: string | null
  fileHash: string | null
  symbols: SourceSymbol[]
  line: number | null
}>()

type SourceFile = { path: string; language: string; sha256: string; content: string }

const source = ref<SourceFile | null>(null)
const loading = ref(false)
const failure = ref('')
const stale = ref(false)
const retry = ref(0)
const selectedLine = ref<number | null>(null)
const codeArea = ref<HTMLElement | null>(null)
const lines = computed(() => source.value?.content.split(/\r\n|\n|\r/) ?? [])
const fileSymbols = computed(() =>
  props.symbols.filter((symbol) => symbol.path === props.filePath).sort((a, b) => a.line - b.line)
)

async function revealLine(line: number | null) {
  selectedLine.value = line
  await nextTick()
  if (line !== null && Number.isInteger(line) && line >= 1) {
    codeArea.value
      ?.querySelector<HTMLElement>(`[data-source-line="${line}"]`)
      ?.scrollIntoView({ block: 'center', inline: 'nearest' })
  }
}

watch(() => props.line, revealLine)

watch(
  [() => props.filePath, () => props.fileHash, retry],
  async ([filePath, fileHash], _, onCleanup) => {
    const controller = new AbortController()
    onCleanup(() => controller.abort())
    source.value = null
    failure.value = ''
    stale.value = false
    loading.value = Boolean(filePath)
    selectedLine.value = props.line
    if (!filePath) return
    if (!fileHash) {
      stale.value = true
      failure.value = '当前页面缺少此文件的索引指纹，请重新扫描后再查看。'
      loading.value = false
      return
    }

    try {
      const response = await fetch(
        `/api/source?${new URLSearchParams({ path: filePath, sha256: fileHash })}`,
        { signal: controller.signal }
      )
      if (controller.signal.aborted) return
      if (response.status === 409) {
        stale.value = true
        failure.value = '源码已变化或无法安全读取。请先使用页面上的“重新扫描”，再查看此文件。'
        return
      }
      if (!response.ok) {
        failure.value =
          response.status === 404 ? '文件不在当前索引中，请重新扫描。' : '源码读取失败，请重试。'
        return
      }
      const data: SourceFile = await response.json()
      if (controller.signal.aborted) return
      if (
        data?.path !== filePath ||
        typeof data.content !== 'string' ||
        typeof data.language !== 'string' ||
        typeof data.sha256 !== 'string'
      ) {
        failure.value = '源码响应不完整，请重试。'
        return
      }
      if (data.sha256 !== fileHash) {
        stale.value = true
        failure.value = '源码与当前页面的索引不一致，请重新扫描后再查看。'
        return
      }
      source.value = data
      loading.value = false
      await revealLine(props.line)
    } catch {
      if (!controller.signal.aborted) failure.value = '无法读取源码，请检查本地浏览服务后重试。'
    } finally {
      if (!controller.signal.aborted) loading.value = false
    }
  },
  { immediate: true }
)
</script>

<template>
  <section class="source-pane" aria-labelledby="source-title" :aria-busy="loading">
    <header class="source-header">
      <h2 id="source-title">源码</h2>
      <p v-if="filePath" class="source-path">{{ filePath }}</p>
      <p v-if="source" class="source-meta">{{ source.language }} · {{ lines.length }} 行 · 只读</p>
    </header>

    <p v-if="!filePath" class="source-message">选择文件后查看源码，点击函数或类可定位到定义。</p>
    <p v-else-if="loading" class="source-message" role="status">正在读取源码…</p>
    <div v-else-if="failure" class="source-message source-error" role="alert">
      <p>{{ failure }}</p>
      <button v-if="!stale" class="source-retry" type="button" @click="retry++">重试</button>
    </div>
    <template v-else-if="source">
      <details class="symbol-directory" open>
        <summary>函数与类（{{ fileSymbols.length }}）</summary>
        <ul v-if="fileSymbols.length" class="symbol-list">
          <li v-for="symbol in fileSymbols" :key="symbol.id">
            <button
              type="button"
              class="symbol-button"
              :class="{ 'symbol-selected': selectedLine === symbol.line }"
              :aria-label="`${symbol.kind === 'class' ? '类' : '函数'} ${symbol.name}，第 ${symbol.line} 行`"
              @click="revealLine(symbol.line)"
            >
              <span class="symbol-kind">{{ symbol.kind === 'class' ? '类' : '函数' }}</span>
              <span class="symbol-name">{{ symbol.name }}</span>
              <span class="symbol-line">{{ symbol.line }}</span>
            </button>
          </li>
        </ul>
        <p v-else class="symbol-empty">此文件没有已索引的函数或类。</p>
      </details>

      <div ref="codeArea" class="source-scroll" tabindex="0" :aria-label="`${filePath} 的只读源码`">
        <pre class="source-code"><code><span
          v-for="(text, index) in lines"
          :key="index"
          class="source-row"
          :class="{ 'source-row-selected': selectedLine === index + 1 }"
          :data-source-line="index + 1"
        ><span class="source-line-number" aria-hidden="true">{{ index + 1 }}</span><span class="source-line-text">{{ text || ' ' }}</span></span></code></pre>
      </div>
    </template>
  </section>
</template>

<style scoped>
.source-pane {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border, #dce2e9);
  border-radius: 12px;
  background: var(--surface, #fff);
  color: var(--text, #243247);
}

.source-header {
  padding: 18px 20px 14px;
}

.source-header h2 {
  margin: 0;
  font-size: 1rem;
}

.source-path {
  margin: 8px 0 0;
  overflow-wrap: anywhere;
  font-family: ui-monospace, monospace;
  font-size: 0.85rem;
  line-height: 1.6;
}

.source-meta,
.symbol-empty {
  margin: 8px 0 0;
  color: var(--muted, #5f6d80);
  font-size: 0.8rem;
}

.source-message {
  margin: 0;
  padding: 20px;
  line-height: 1.7;
  color: var(--muted, #5f6d80);
}

.source-error {
  color: #9b3f26;
}

.source-error p {
  margin: 0 0 12px;
}

.source-retry {
  padding: 8px 16px;
  border: 1px solid currentColor;
  border-radius: 6px;
  background: transparent;
  color: inherit;
  font: inherit;
  cursor: pointer;
}

.symbol-directory {
  padding: 12px 20px;
  border-top: 1px solid var(--border, #dce2e9);
}

.symbol-directory summary {
  font-size: 0.85rem;
  cursor: pointer;
}

.symbol-list {
  max-height: 200px;
  margin: 10px 0 0;
  padding: 0;
  overflow: auto;
  list-style: none;
}

.symbol-button {
  display: grid;
  grid-template-columns: 2.5em minmax(0, 1fr) auto;
  align-items: baseline;
  gap: 8px;
  width: 100%;
  padding: 7px 6px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 0.8rem;
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
}

.symbol-button:hover,
.symbol-selected {
  background: #edf3ee;
}

.symbol-name {
  overflow-wrap: anywhere;
  font-family: ui-monospace, monospace;
}

.symbol-kind,
.symbol-line {
  color: var(--muted, #5f6d80);
}

.source-scroll {
  max-height: 65vh;
  min-height: 120px;
  overflow: auto;
  border-top: 1px solid var(--border, #dce2e9);
  background: #f8faf8;
}

.source-code {
  width: max-content;
  min-width: 100%;
  margin: 0;
  padding: 12px 0;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 0.78rem;
  line-height: 1.7;
  tab-size: 2;
}

.source-code code {
  font: inherit;
}

.source-row {
  display: grid;
  grid-template-columns: 4.5em minmax(0, 1fr);
  border-left: 3px solid transparent;
}

.source-row-selected {
  border-left-color: var(--accent, #176b58);
  background: #e5efe5;
}

.source-line-number {
  padding-right: 1.2em;
  color: #6d798a;
  text-align: right;
  user-select: none;
}

.source-line-text {
  padding-right: 20px;
  white-space: pre;
}

.source-pane :focus-visible {
  outline: 2px solid var(--accent, #176b58);
  outline-offset: -2px;
}

@media (max-width: 640px) {
  .source-header,
  .source-message {
    padding: 14px;
  }

  .symbol-directory {
    padding: 12px 14px;
  }

  .source-scroll {
    max-height: 55vh;
  }

  .source-row {
    grid-template-columns: 3.8em minmax(0, 1fr);
  }
}
</style>
