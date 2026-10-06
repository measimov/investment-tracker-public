<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from 'vue'
import type { Dependency } from './types'

interface GraphNode {
  path: string
  directory: boolean
  count: number
}
const props = defineProps<{
  nodes: GraphNode[]
  links: [string, { source: string; target: string; evidence: Dependency[] }][]
  colors?: Record<string, string>
  selected: string | null
}>()
const emit = defineEmits<{ openNode: [node: GraphNode] }>()
const prefix = `uml-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
const canvas = ref<HTMLElement | null>(null)
const drawing = ref<HTMLElement | null>(null)
const svg = ref('')
const loading = ref(false)
const failed = ref(false)
const zoom = ref(1)
const size = ref({ width: 600, height: 360 })
const retry = ref(0)
const edgeLimit = 120
let sequence = 0

const commonDirectory = computed(() => {
  const parts = props.nodes[0]?.path.split('/').slice(0, -1) ?? []
  while (
    parts.length &&
    !props.nodes.every((node) => node.path.startsWith(`${parts.join('/')}/`))
  ) {
    parts.pop()
  }
  return parts.length ? `${parts.join('/')}/` : ''
})
function visibleLabel(path: string) {
  const relative = path.slice(commonDirectory.value.length)
  return (relative.length > 64 ? `…${relative.slice(-63)}` : relative).replace(
    /[\u0000-\u001f\u007f]/g,
    ' '
  )
}
// Only generated identifiers and numeric entities enter the Mermaid grammar.
function label(path: string) {
  return Array.from(visibleLabel(path))
    .map((character) =>
      /[\p{L}\p{N}_./ -]/u.test(character) ? character : `#${character.codePointAt(0)};`
    )
    .join('')
}
const definition = computed(() => {
  const ids = new Map(props.nodes.map((node, index) => [node.path, `n${index}`]))
  const lines = ['classDiagram', 'direction LR']
  props.nodes.forEach((node, index) => {
    const id = `n${index}`
    const color = props.colors?.[node.path]
    const fill =
      color && /^#[0-9a-f]{6}$/i.test(color) ? color : node.directory ? '#edf5f0' : '#ffffff'
    lines.push(
      `class ${id}["${label(node.path)}"]`,
      `<<${node.directory ? 'package' : 'module'}>> ${id}`,
      `cssClass "${id}" uml_node_${index}`,
      `style ${id} fill:${fill},stroke:#527a6c,color:#243b35,stroke-width:${node.path === props.selected ? 3 : 1}px`
    )
  })
  for (const [, link] of props.links.slice(0, edgeLimit)) {
    const source = ids.get(link.source),
      target = ids.get(link.target)
    if (source && target) lines.push(`${source} ..> ${target} : ${link.evidence.length} 条导入`)
  }
  return lines.join('\n')
})

watch(
  [definition, () => props.nodes.map((node) => node.path), retry],
  async ([text]) => {
    const request = ++sequence
    svg.value = ''
    failed.value = false
    loading.value = props.nodes.length > 0
    zoom.value = 1
    if (!props.nodes.length) return
    try {
      const { default: mermaid } = await import('mermaid')
      if (request !== sequence) return
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        htmlLabels: false,
        theme: 'base',
        look: 'classic',
        layout: 'dagre',
        fontFamily: 'system-ui, sans-serif',
        themeVariables: {
          fontSize: '14px',
          primaryColor: '#edf5f0',
          primaryTextColor: '#243b35',
          lineColor: '#527a6c'
        },
        class: {
          defaultRenderer: 'dagre-wrapper',
          hideEmptyMembersBox: true,
          useMaxWidth: false,
          nodeSpacing: 35,
          rankSpacing: 65
        },
        maxEdges: edgeLimit,
        suppressErrorRendering: true
      })
      const result = await mermaid.render(`${prefix}-${request}`, text)
      if (request !== sequence) return
      svg.value = result.svg
      await nextTick()
      if (request !== sequence) return
      const element = drawing.value?.querySelector('svg')
      const bounds = element?.viewBox.baseVal
      if (bounds?.width && bounds.height)
        size.value = { width: bounds.width, height: bounds.height }
      props.nodes.forEach((node, index) => {
        // Documented cssClass annotations identify nodes; paths never become selectors.
        const target = element?.querySelector(`.uml_node_${index}`)
        if (!target) return
        target.setAttribute('data-uml-node', String(index))
        target.setAttribute('tabindex', '0')
        target.setAttribute('role', 'button')
        target.setAttribute(
          'aria-label',
          `${node.directory ? '打开目录' : '查看文件'} ${node.path}`
        )
        const title = document.createElementNS('http://www.w3.org/2000/svg', 'title')
        title.textContent = node.path
        target.prepend(title)
        // Mermaid's SVG text renderer can retain entity escapes or parse underscores
        // as Markdown. Restore the filename using text nodes, never an HTML decode.
        const rows = target.querySelectorAll<SVGTSpanElement>('.label-group .text-outer-tspan')
        if (rows[0]) {
          rows[0].textContent = visibleLabel(node.path)
          for (const row of [...rows].slice(1)) row.remove()
          const group = target.querySelector<SVGGElement>('.label-group')
          const transform = group?.transform.baseVal.getItem(0)
          if (group && transform)
            transform.setTranslate(-group.getBBox().width / 2, transform.matrix.f)
        }
      })
      canvas.value?.scrollTo(0, 0)
    } catch {
      if (request === sequence) failed.value = true
    } finally {
      if (request === sequence) loading.value = false
    }
  },
  { immediate: true }
)

function openNode(event: MouseEvent | KeyboardEvent) {
  if (event instanceof KeyboardEvent && !['Enter', ' '].includes(event.key)) return
  const element = event.target instanceof Element ? event.target.closest('[data-uml-node]') : null
  if (!element || loading.value) return
  const node = props.nodes[Number(element.getAttribute('data-uml-node'))]
  if (node) {
    event.preventDefault()
    emit('openNode', node)
  }
}
function fit() {
  if (canvas.value)
    zoom.value = Math.max(
      0.1,
      Math.min(
        1,
        (canvas.value.clientWidth - 32) / size.value.width,
        (canvas.value.clientHeight - 32) / size.value.height
      )
    )
}
onBeforeUnmount(() => sequence++)
</script>

<template>
  <section class="uml-view" aria-label="UML 模块依赖图">
    <p class="uml-legend">«package» 目录 · «module» 文件 · 虚线箭头指向被依赖方</p>
    <p class="uml-note">按导入关系分层排列；文件节点表示模块，不表示业务类或继承关系。</p>
    <div class="uml-controls" aria-label="UML 图缩放">
      <button
        type="button"
        :disabled="!svg || zoom <= 0.25"
        aria-label="缩小 UML 图"
        @click="zoom = Math.max(0.25, zoom - 0.25)"
      >
        −
      </button>
      <span>{{ Math.round(zoom * 100) }}%</span>
      <button
        type="button"
        :disabled="!svg || zoom >= 2"
        aria-label="放大 UML 图"
        @click="zoom = Math.min(2, zoom + 0.25)"
      >
        +
      </button>
      <button type="button" :disabled="!svg" @click="fit">适合窗口</button>
      <button type="button" :disabled="!svg" @click="zoom = 1">原始尺寸</button>
    </div>
    <p v-if="loading" class="uml-message" role="status">正在绘制 UML 图…</p>
    <div v-else-if="failed" class="uml-message" role="alert">
      <p>UML 图暂时无法绘制，仍可使用目录和下方依赖列表浏览。</p>
      <button type="button" @click="retry++">重试绘图</button>
    </div>
    <p v-else-if="!nodes.length" class="uml-message">当前快照没有可展示的节点。</p>
    <div
      ref="canvas"
      v-show="svg"
      class="uml-scroll"
      tabindex="0"
      role="region"
      aria-label="可滚动的 UML 图，节点支持点击或回车打开"
    >
      <div
        ref="drawing"
        class="uml-drawing"
        :style="{ width: `${size.width * zoom}px`, height: `${size.height * zoom}px` }"
        @click="openNode"
        @keydown="openNode"
        v-html="svg"
      ></div>
    </div>
    <p v-if="links.length > edgeLimit" class="uml-note">
      图中显示前 {{ edgeLimit }} /
      {{ links.length }} 组依赖；完整关系仍可通过下方列表查看。可进入子目录或筛选依赖类型缩小范围。
    </p>
    <p class="uml-note">点击节点下钻，使用下方列表查看导入证据；大图可滚动浏览或调整缩放。</p>
  </section>
</template>

<style scoped>
.uml-view {
  min-width: 0;
}
.uml-legend,
.uml-note {
  margin: 10px 20px;
  font-size: 12px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.uml-note {
  color: var(--muted, #75827b);
}
.uml-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 4px 20px 12px;
  font-size: 12px;
}
.uml-controls button,
.uml-message button {
  border: 1px solid var(--border, #e2e8e2);
  border-radius: 5px;
  background: var(--surface, #fcfdfb);
  color: var(--accent, #176b58);
  padding: 7px 10px;
  min-height: 34px;
  font: inherit;
  cursor: pointer;
}
.uml-controls button:disabled {
  color: var(--muted, #75827b);
  cursor: default;
}
.uml-controls span {
  min-width: 36px;
  text-align: center;
}
.uml-scroll {
  width: 100%;
  height: clamp(440px, 70vh, 760px);
  overflow: auto;
  padding: 16px;
  box-sizing: border-box;
  overscroll-behavior: contain;
  background: #f7faf7;
  border-block: 1px solid var(--border, #e2e8e2);
}
.uml-drawing :deep(svg) {
  width: 100%;
  height: 100%;
  max-width: none !important;
  display: block;
}
.uml-drawing :deep([data-uml-node]) {
  cursor: pointer;
}
.uml-drawing :deep([data-uml-node]:focus-visible rect) {
  stroke: #176b58 !important;
  stroke-width: 3px !important;
}
.uml-message {
  padding: 40px 20px;
  font-size: 13px;
  line-height: 1.8;
  text-align: center;
  color: var(--muted, #75827b);
}
@media (max-width: 600px) {
  .uml-scroll {
    height: 400px;
  }
  .uml-legend,
  .uml-note {
    margin-inline: 14px;
  }
  .uml-controls {
    padding-inline: 14px;
  }
}
</style>
