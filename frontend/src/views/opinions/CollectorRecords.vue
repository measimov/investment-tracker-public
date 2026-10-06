<script setup lang="ts">
import { computed, h } from 'vue'
import { NButton, NCheckbox, NDataTable, NEmpty, NTag, type DataTableColumns } from 'naive-ui'
import { useMediaQuery } from '@/composables/useMediaQuery'
import type { CollectorAuthor, CollectorCube, CollectorStatus } from '@/types'
import { EMPTY, formatDateTime } from '@/utils/helpers'
import { runStatusLabel, runStatusType, xueqiuCubeUrl, xueqiuProfileUrl } from './collectorStatus'
const props = defineProps<{ status: CollectorStatus; isAdmin: boolean; saving: boolean }>()
const emit = defineEmits<{
  toggleAuthor: [author: CollectorAuthor, enabled: boolean]
  removeAuthor: [author: CollectorAuthor]
  toggleCube: [cube: CollectorCube, enabled: boolean]
  removeCube: [cube: CollectorCube]
}>()
const isMobile = useMediaQuery('(max-width:640px)')
const emptyTheme = { textColor: 'var(--app-text-muted)' }
function statusTag(status: string | null | undefined) {
  const type = runStatusType(status)
  return h(
    NTag,
    {
      size: 'small',
      type: type === 'danger' ? 'error' : type === 'info' ? 'default' : type,
      bordered: false
    },
    () => runStatusLabel(status)
  )
}
function externalLink(url: string, name: string, id: string) {
  return h('div', { class: 'record-identity' }, [
    h(
      'a',
      { href: url, target: '_blank', rel: 'noopener noreferrer', class: 'source-link' },
      name || id
    ),
    name ? h('span', { class: 'muted' }, id) : null
  ])
}
function message(text: string | null | undefined) {
  return text && text.length > 80
    ? h('details', { class: 'run-message' }, [h('summary', {}, '查看完整信息'), h('p', {}, text)])
    : h('span', { class: 'run-message' }, text || EMPTY)
}
function checkbox(label: string, value: boolean, onChange: (v: boolean) => void) {
  return h(NCheckbox, {
    class: 'enable-checkbox',
    'aria-label': label,
    checked: value,
    disabled: !props.isAdmin || props.saving,
    'aria-disabled': !props.isAdmin || props.saving,
    onUpdateChecked: (next: boolean | string | number) => onChange(next === true)
  })
}
const authorColumns = computed<DataTableColumns<CollectorAuthor>>(() => [
  {
    title: '作者',
    key: 'author',
    width: 200,
    render: (a) =>
      externalLink(xueqiuProfileUrl(a.xueqiu_user_id), a.display_name, a.xueqiu_user_id)
  },
  {
    title: '启用',
    key: 'enabled',
    width: 80,
    render: (a) =>
      checkbox(`启用作者 ${a.display_name || a.xueqiu_user_id}`, a.enabled, (v) =>
        emit('toggleAuthor', a, v)
      )
  },
  {
    title: '上次采集',
    key: 'last_run_at',
    width: 160,
    render: (a) => formatDateTime(a.last_run_at)
  },
  {
    title: '结果',
    key: 'last_status',
    width: 280,
    render: (a) =>
      h('div', { class: 'result-cell' }, [
        a.last_status ? statusTag(a.last_status) : null,
        message(a.last_message)
      ])
  },
  ...(props.isAdmin
    ? [
        {
          title: '操作',
          key: 'actions',
          width: 80,
          fixed: 'right' as const,
          render: (a: CollectorAuthor) =>
            h(
              NButton,
              {
                text: true,
                type: 'error',
                'aria-label': `移出作者 ${a.display_name || a.xueqiu_user_id}`,
                disabled: props.saving,
                onClick: () => emit('removeAuthor', a)
              },
              () => '移出'
            )
        }
      ]
    : [])
])
const cubeColumns = computed<DataTableColumns<CollectorCube>>(() => [
  {
    title: '组合',
    key: 'cube',
    width: 200,
    render: (c) => externalLink(xueqiuCubeUrl(c.cube_id), c.display_name, c.cube_id)
  },
  {
    title: '启用',
    key: 'enabled',
    width: 80,
    render: (c) =>
      checkbox(`启用组合 ${c.display_name || c.cube_id}`, c.enabled, (v) =>
        emit('toggleCube', c, v)
      )
  },
  {
    title: '上次采集',
    key: 'last_run_at',
    width: 160,
    render: (c) => formatDateTime(c.last_run_at)
  },
  {
    title: '结果',
    key: 'last_status',
    width: 280,
    render: (c) =>
      h('div', { class: 'result-cell' }, [
        c.last_status ? statusTag(c.last_status) : null,
        message(c.last_message)
      ])
  },
  ...(props.isAdmin
    ? [
        {
          title: '操作',
          key: 'actions',
          width: 80,
          fixed: 'right' as const,
          render: (c: CollectorCube) =>
            h(
              NButton,
              {
                text: true,
                type: 'error',
                'aria-label': `移出组合 ${c.display_name || c.cube_id}`,
                disabled: props.saving,
                onClick: () => emit('removeCube', c)
              },
              () => '移出'
            )
        }
      ]
    : [])
])
type Run = CollectorStatus['recent_runs'][number]
const runColumns: DataTableColumns<Run> = [
  { title: '开始', key: 'started_at', width: 165, render: (r) => formatDateTime(r.started_at) },
  { title: '作者', key: 'author_user_id', width: 115 },
  { title: '结果', key: 'status', width: 100, render: (r) => statusTag(r.status) },
  {
    title: '候选 / 回复 / 发言',
    key: 'counts',
    width: 160,
    render: (r) => `${r.candidate_count} / ${r.reply_count} / ${r.utterance_count}`
  },
  { title: '错误', key: 'error_message', width: 280, render: (r) => message(r.error_message) }
]
</script>
<template>
  <section class="collector-records" aria-label="采集器名单与运行">
    <h3>关注作者</h3>
    <div data-testid="collector-authors-table">
      <NDataTable
        v-if="!isMobile && status.authors.length"
        class="records-table"
        :columns="authorColumns"
        :data="status.authors"
        :row-key="(a) => a.xueqiu_user_id"
        :scroll-x="isAdmin ? 800 : 720"
        size="small"
      />
      <div v-else-if="isMobile && status.authors.length" class="record-cards">
        <article v-for="a in status.authors" :key="a.xueqiu_user_id" class="record-card">
          <header>
            <a
              :href="xueqiuProfileUrl(a.xueqiu_user_id)"
              target="_blank"
              rel="noopener noreferrer"
              class="source-link"
              >{{ a.display_name || a.xueqiu_user_id }}</a
            ><span class="enable-control"
              ><NCheckbox
                class="enable-checkbox"
                :aria-label="`启用作者 ${a.display_name || a.xueqiu_user_id}`"
                :checked="a.enabled"
                :disabled="!isAdmin || saving"
                :aria-disabled="!isAdmin || saving"
                @update:checked="emit('toggleAuthor', a, $event === true)"
                ><span
                  ><span>启用</span
                  ><span class="control-context"
                    >作者 {{ a.display_name || a.xueqiu_user_id }}</span
                  ></span
                ></NCheckbox
              ></span
            >
          </header>
          <p v-if="a.display_name" class="muted">{{ a.xueqiu_user_id }}</p>
          <p class="muted">上次采集 {{ formatDateTime(a.last_run_at) }}</p>
          <NTag
            v-if="a.last_status"
            size="small"
            :type="
              runStatusType(a.last_status) === 'danger'
                ? 'error'
                : runStatusType(a.last_status) === 'info'
                  ? 'default'
                  : (runStatusType(a.last_status) as 'success' | 'warning' | 'primary')
            "
            :bordered="false"
            >{{ runStatusLabel(a.last_status) }}</NTag
          >
          <details v-if="a.last_message" class="run-message">
            <summary>查看运行信息</summary>
            <p>{{ a.last_message }}</p>
          </details>
          <NButton
            v-if="isAdmin"
            text
            type="error"
            :disabled="saving"
            :aria-label="`移出作者 ${a.display_name || a.xueqiu_user_id}`"
            @click="emit('removeAuthor', a)"
            >移出</NButton
          >
        </article>
      </div>
      <NEmpty v-else description="关注名单为空" :theme-overrides="emptyTheme" />
    </div>
    <slot name="add-author" />
    <h3>跟踪组合</h3>
    <p class="section-note">调仓记录随按标的采集每日一轮。</p>
    <div data-testid="collector-cubes-table">
      <NDataTable
        v-if="!isMobile && status.cubes.length"
        class="records-table"
        :columns="cubeColumns"
        :data="status.cubes"
        :row-key="(c) => c.cube_id"
        :scroll-x="isAdmin ? 800 : 720"
        size="small"
      />
      <div v-else-if="isMobile && status.cubes.length" class="record-cards">
        <article v-for="c in status.cubes" :key="c.cube_id" class="record-card">
          <header>
            <a
              :href="xueqiuCubeUrl(c.cube_id)"
              target="_blank"
              rel="noopener noreferrer"
              class="source-link"
              >{{ c.display_name || c.cube_id }}</a
            ><span class="enable-control"
              ><NCheckbox
                class="enable-checkbox"
                :aria-label="`启用组合 ${c.display_name || c.cube_id}`"
                :checked="c.enabled"
                :disabled="!isAdmin || saving"
                :aria-disabled="!isAdmin || saving"
                @update:checked="emit('toggleCube', c, $event === true)"
                ><span
                  ><span>启用</span
                  ><span class="control-context">组合 {{ c.display_name || c.cube_id }}</span></span
                ></NCheckbox
              ></span
            >
          </header>
          <p v-if="c.display_name" class="muted">{{ c.cube_id }}</p>
          <p class="muted">上次采集 {{ formatDateTime(c.last_run_at) }}</p>
          <NTag
            v-if="c.last_status"
            size="small"
            :type="
              runStatusType(c.last_status) === 'danger'
                ? 'error'
                : runStatusType(c.last_status) === 'info'
                  ? 'default'
                  : (runStatusType(c.last_status) as 'success' | 'warning' | 'primary')
            "
            :bordered="false"
            >{{ runStatusLabel(c.last_status) }}</NTag
          >
          <details v-if="c.last_message" class="run-message">
            <summary>查看运行信息</summary>
            <p>{{ c.last_message }}</p>
          </details>
          <NButton
            v-if="isAdmin"
            text
            type="error"
            :disabled="saving"
            :aria-label="`移出组合 ${c.display_name || c.cube_id}`"
            @click="emit('removeCube', c)"
            >移出</NButton
          >
        </article>
      </div>
      <NEmpty v-else description="没有跟踪的组合" :theme-overrides="emptyTheme" />
    </div>
    <slot name="add-cube" />
    <h3>最近运行（作者采集）</h3>
    <NDataTable
      v-if="!isMobile && status.recent_runs.length"
      class="records-table"
      :columns="runColumns"
      :data="status.recent_runs"
      :scroll-x="820"
      size="small"
    />
    <div v-else-if="isMobile && status.recent_runs.length" class="record-cards">
      <article v-for="(run, index) in status.recent_runs" :key="index" class="record-card">
        <header>
          <span>{{ formatDateTime(run.started_at) }}</span
          ><NTag
            size="small"
            :type="
              runStatusType(run.status) === 'danger'
                ? 'error'
                : runStatusType(run.status) === 'info'
                  ? 'default'
                  : (runStatusType(run.status) as 'success' | 'warning' | 'primary')
            "
            :bordered="false"
            >{{ runStatusLabel(run.status) }}</NTag
          >
        </header>
        <p class="muted">作者 {{ run.author_user_id || EMPTY }}</p>
        <p>
          候选 {{ run.candidate_count }} · 回复 {{ run.reply_count }} · 发言
          {{ run.utterance_count }}
        </p>
        <details v-if="run.error_message" class="run-message">
          <summary>查看完整错误</summary>
          <p>{{ run.error_message }}</p>
        </details>
      </article>
    </div>
    <NEmpty v-else description="采集器还没有运行记录" :theme-overrides="emptyTheme" />
  </section>
</template>
<style scoped>
h3 {
  font-size: 15px;
  margin: 24px 0 12px;
}
.section-note,
.muted {
  font-size: 12px;
  color: var(--app-text-muted);
}
.section-note {
  margin: -4px 0 12px;
}
.records-table :deep(.record-identity) {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.records-table :deep(.source-link),
.source-link {
  color: var(--app-primary-strong);
  text-decoration: none;
  overflow-wrap: anywhere;
}
.records-table :deep(.source-link:hover),
.source-link:hover {
  text-decoration: underline;
}
.records-table :deep(.muted) {
  color: var(--app-text-muted);
  font-size: 12px;
}
.records-table :deep(.result-cell) {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}
.records-table :deep(.run-message),
.run-message {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.75;
}
.records-table :deep(.run-message summary),
.run-message summary {
  cursor: pointer;
}
.records-table :deep(.run-message p),
.run-message p {
  margin: 8px 0;
}
.records-table :deep(.n-button) {
  min-height: 24px;
}
.records-table :deep(.enable-checkbox) {
  min-width: 24px;
  min-height: 24px;
  justify-content: center;
}
.record-card {
  padding: 16px 0;
  border-bottom: 1px solid var(--app-border-soft);
}
.record-card header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.record-card .source-link {
  font-size: 16px;
  min-height: 44px;
  align-content: center;
}
.enable-control {
  display: flex;
  gap: 8px;
  align-items: center;
  min-height: 44px;
  flex-shrink: 0;
  color: var(--app-text-muted);
  font-size: 13px;
}
.enable-control :deep(.enable-checkbox) {
  min-width: 44px;
  min-height: 44px;
  justify-content: center;
}
.control-context {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
.record-card .n-button {
  min-height: 44px;
  min-width: 44px;
}
.record-card .run-message summary {
  min-height: 44px;
  align-content: center;
}
.source-link:focus-visible {
  outline: 2px solid var(--app-primary-strong);
  outline-offset: 3px;
}
</style>
