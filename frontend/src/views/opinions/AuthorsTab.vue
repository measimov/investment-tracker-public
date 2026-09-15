<script setup lang="ts">
import OpinionAuthorFeed from '@/components/OpinionAuthorFeed.vue'
import type { OpinionFeedAuthor } from '@/types'

defineProps<{
  authors: OpinionFeedAuthor[]
  loading: boolean
}>()
</script>

<template>
  <OpinionAuthorFeed :authors="authors" :loading="loading" />
</template>
