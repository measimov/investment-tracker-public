<script setup lang="ts">
import { computed, h, ref } from 'vue'
import { useMediaQuery } from '@vueuse/core'
import { NButton, NDataTable, NEmpty, NSpin, NTag, type DataTableColumns } from 'naive-ui'
import type { User } from '@/types'
import { EMPTY, formatDateTime } from '@/utils/helpers'
const props = defineProps<{ users: User[]; loading: boolean; emptyDescription: string }>()
const emit = defineEmits<{ edit: [row: User]; resetPassword: [row: User]; delete: [row: User] }>()
const mobile = useMediaQuery('(max-width: 640px)')
const sortOrder = ref<'ascend' | 'descend' | false>(false)
function cycleSort() {
  sortOrder.value =
    sortOrder.value === false ? 'ascend' : sortOrder.value === 'ascend' ? 'descend' : false
}
const sortLabel = computed(() =>
  sortOrder.value === 'ascend' ? '升序' : sortOrder.value === 'descend' ? '降序' : '未排序'
)
const rows = computed(() => {
  if (!sortOrder.value) return props.users
  const direction = sortOrder.value === 'ascend' ? 1 : -1
  return [...props.users].sort(
    (a, b) => (new Date(a.created_at).getTime() - new Date(b.created_at).getTime()) * direction
  )
})
const columns: DataTableColumns<User> = [
  { key: 'id', title: 'ID', width: 70 },
  {
    key: 'username',
    title: '用户名',
    width: 190,
    render: (row) => h('span', { class: 'username' }, row.username)
  },
  {
    key: 'email',
    title: '邮箱',
    width: 260,
    render: (row) => h('span', { class: 'email' }, row.email || EMPTY)
  },
  {
    key: 'is_active',
    title: '状态',
    width: 80,
    render: (row) =>
      h(
        NTag,
        { type: row.is_active ? 'success' : 'error', size: 'small', bordered: false },
        { default: () => (row.is_active ? '激活' : '禁用') }
      )
  },
  {
    key: 'is_admin',
    title: '管理员',
    width: 80,
    render: (row) =>
      h(
        NTag,
        { type: row.is_admin ? 'warning' : 'default', size: 'small', bordered: false },
        { default: () => (row.is_admin ? '是' : '否') }
      )
  },
  {
    key: 'created_at',
    title: () =>
      h(
        'button',
        {
          type: 'button',
          class: 'sort-button',
          'aria-label': `创建时间排序：${sortLabel.value}`,
          onClick: cycleSort
        },
        `创建时间 ${sortOrder.value === 'ascend' ? '↑' : sortOrder.value === 'descend' ? '↓' : '↕'}`
      ),
    width: 160,
    render: (row) => formatDateTime(row.created_at)
  },
  {
    key: 'actions',
    title: '操作',
    width: 222,
    fixed: 'right',
    render: (row) =>
      h('div', { class: 'row-actions' }, [
        h(
          NButton,
          {
            text: true,
            type: 'primary',
            size: 'small',
            'aria-label': `编辑用户 ${row.username}`,
            onClick: () => emit('edit', row)
          },
          { default: () => '编辑' }
        ),
        h(
          NButton,
          {
            text: true,
            type: 'primary',
            size: 'small',
            'aria-label': `重置用户 ${row.username} 的密码`,
            onClick: () => emit('resetPassword', row)
          },
          { default: () => '重置密码' }
        ),
        h(
          NButton,
          {
            text: true,
            type: 'error',
            size: 'small',
            'aria-label': `删除用户 ${row.username}`,
            onClick: () => emit('delete', row)
          },
          { default: () => '删除' }
        )
      ])
  }
]
</script>
<template>
  <NSpin :show="loading">
    <div v-if="!mobile" class="desktop-users">
      <NDataTable
        :columns="columns"
        :data="rows"
        :row-key="(row: User) => row.id"
        :scroll-x="1162"
        :bordered="true"
        :single-line="true"
        aria-label="用户列表"
        data-testid="users-table"
      >
        <template #empty
          ><NEmpty
            :description="emptyDescription"
            :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
        /></template>
      </NDataTable>
      <p class="scroll-note">表格可横向滚动查看完整资料与操作。</p>
    </div>
    <div v-else class="mobile-users">
      <button
        v-if="users.length"
        type="button"
        class="sort-button mobile-sort"
        :aria-label="`创建时间排序：${sortLabel}`"
        @click="cycleSort"
      >
        创建时间：{{ sortLabel }} <span aria-hidden="true">↕</span>
      </button>
      <NEmpty
        v-if="!rows.length"
        :description="emptyDescription"
        :theme-overrides="{ textColor: 'var(--app-text-muted)' }"
      />
      <article v-for="row in rows" :key="row.id" class="user-card" data-testid="user-card">
        <header>
          <h3>{{ row.username }}</h3>
          <NTag :type="row.is_active ? 'success' : 'error'" size="small" :bordered="false">{{
            row.is_active ? '激活' : '禁用'
          }}</NTag>
        </header>
        <dl>
          <div>
            <dt>ID</dt>
            <dd>{{ row.id }}</dd>
          </div>
          <div>
            <dt>邮箱</dt>
            <dd class="email">{{ row.email || EMPTY }}</dd>
          </div>
          <div>
            <dt>管理员</dt>
            <dd>
              <NTag :type="row.is_admin ? 'warning' : 'default'" size="small" :bordered="false">{{
                row.is_admin ? '是' : '否'
              }}</NTag>
            </dd>
          </div>
          <div>
            <dt>创建时间</dt>
            <dd>{{ formatDateTime(row.created_at) }}</dd>
          </div>
        </dl>
        <footer class="row-actions">
          <NButton
            text
            type="primary"
            :aria-label="`编辑用户 ${row.username}`"
            @click="emit('edit', row)"
            >编辑</NButton
          >
          <NButton
            text
            type="primary"
            :aria-label="`重置用户 ${row.username} 的密码`"
            @click="emit('resetPassword', row)"
            >重置密码</NButton
          >
          <NButton
            text
            type="error"
            :aria-label="`删除用户 ${row.username}`"
            @click="emit('delete', row)"
            >删除</NButton
          >
        </footer>
      </article>
    </div>
  </NSpin>
</template>
<style scoped>
.desktop-users,
.mobile-users {
  min-width: 0;
}
.desktop-users :deep(.username) {
  font-weight: 600;
  overflow-wrap: anywhere;
}
.desktop-users :deep(.email),
.email {
  overflow-wrap: anywhere;
}
.desktop-users :deep(.row-actions),
.row-actions {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.desktop-users :deep(.row-actions .n-button) {
  min-height: 24px;
}
.desktop-users :deep(.sort-button),
.sort-button {
  font: inherit;
  border: 0;
  background: transparent;
  color: inherit;
  padding: 0;
  min-height: 24px;
  cursor: pointer;
}
.desktop-users :deep(.sort-button:focus-visible),
.sort-button:focus-visible {
  outline: 2px solid var(--app-primary);
  outline-offset: 4px;
}
.scroll-note {
  color: var(--app-text-muted);
  font-size: 12px;
  margin: 10px 0 0;
}
.mobile-sort {
  display: flex;
  gap: 12px;
  min-height: 44px;
  color: var(--app-text-muted);
  margin-bottom: 12px;
}
.user-card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 12px;
}
.user-card header {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  justify-content: space-between;
}
h3 {
  margin: 0;
  font-size: 16px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
dl {
  margin: 16px 0 12px;
  font-size: 13px;
  line-height: 1.7;
}
dl > div {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 12px;
  margin-top: 8px;
}
dt {
  color: var(--app-text-muted);
}
dd {
  margin: 0;
}
.user-card .row-actions {
  border-top: 1px solid var(--app-border);
  padding-top: 8px;
}
.user-card .row-actions :deep(.n-button) {
  min-height: 44px;
  min-width: 44px;
}
</style>
