<script setup lang="ts">
import {
  Grid,
  Wallet,
  Sort,
  DataAnalysis,
  Star,
  Document,
  Collection,
  Setting,
  Close
} from '@element-plus/icons-vue'
defineProps<{ closable?: boolean }>()
defineEmits<{ close: []; navigate: [label: string] }>()
const navGroups = [
  {
    label: '投资组合',
    items: [
      { label: '仪表盘', icon: Grid },
      { label: '持仓', icon: Wallet },
      { label: '交易记录', icon: Sort },
      { label: '统计分析', icon: DataAnalysis }
    ]
  },
  {
    label: '投资研究',
    items: [
      { label: '观察清单', icon: Star },
      { label: '研究报告', icon: Document },
      { label: '观点与动态', icon: Collection }
    ]
  },
  {
    label: '账本管理',
    items: [
      { label: '账户与数据', icon: Setting },
      { label: '公司行动', icon: Document }
    ]
  }
]
</script>
<template>
  <div class="sidebar-content">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true"><i /><i /><i /></span><span>投资追踪</span
      ><button
        v-if="closable"
        tabindex="0"
        class="icon-button sidebar-close"
        aria-label="关闭导航"
        @click="$emit('close')"
      >
        <Close />
      </button>
    </div>
    <div class="workspace-label">个人投资账本</div>
    <nav>
      <div v-for="group in navGroups" :key="group.label" class="nav-group">
        <div class="nav-group-label">{{ group.label }}</div>
        <button
          v-for="item in group.items"
          :key="item.label"
          class="nav-item"
          tabindex="0"
          :class="{ active: item.label === '持仓' }"
          :aria-current="item.label === '持仓' ? 'page' : undefined"
          @click="$emit('navigate', item.label)"
        >
          <component :is="item.icon" /><span>{{ item.label }}</span
          ><span v-if="item.label === '持仓'" class="active-dot" />
        </button>
      </div>
    </nav>
    <div class="sidebar-bottom">
      <div class="profile-monogram">M</div>
      <div><strong>我的投资空间</strong><span>个人账本</span></div>
      <Setting class="small-icon" />
    </div>
  </div>
</template>
