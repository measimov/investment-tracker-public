import { computed, ref, shallowRef, watch } from 'vue'
import { useLatestRequest } from '@/composables/useLatestRequest'
import type { BrokerImportResult } from '@/types'
import type { BrokerImportMode } from './brokerImports'

export interface ImportSelection {
  file: File
  mode: BrokerImportMode
  accountId: number
  confirmed: string[]
}

function sameSelection(left: ImportSelection | null, right: ImportSelection | null) {
  return (
    left !== null &&
    right !== null &&
    left.file === right.file &&
    left.mode === right.mode &&
    left.accountId === right.accountId &&
    left.confirmed.length === right.confirmed.length &&
    left.confirmed.every((hash, index) => hash === right.confirmed[index])
  )
}

/** A successful preview authorizes only the exact selection that was previewed. */
export function useBrokerImportPreview(
  selection: () => ImportSelection | null,
  request: (selection: ImportSelection) => Promise<{ data: BrokerImportResult }>
) {
  const latest = useLatestRequest()
  const preview = shallowRef<BrokerImportResult | null>(null)
  const previewSelection = shallowRef<ImportSelection | null>(null)
  const loading = ref(false)

  function invalidate() {
    latest.invalidate()
    preview.value = null
    previewSelection.value = null
    loading.value = false
  }
  watch(selection, invalidate, { flush: 'sync' })

  const acceptedSelection = computed(() =>
    preview.value && sameSelection(previewSelection.value, selection())
      ? previewSelection.value
      : null
  )

  async function refresh(): Promise<boolean> {
    const selected = selection()
    if (!selected) return false
    const snapshot = { ...selected, confirmed: [...selected.confirmed] }
    const token = latest.begin()
    preview.value = null
    previewSelection.value = null
    loading.value = true
    try {
      const response = await request(snapshot)
      if (!latest.isCurrent(token) || !sameSelection(snapshot, selection())) return false
      preview.value = response.data
      previewSelection.value = snapshot
      return true
    } catch (error) {
      if (latest.isCurrent(token) && sameSelection(snapshot, selection())) throw error
      return false
    } finally {
      if (latest.isCurrent(token)) loading.value = false
    }
  }

  return { preview, loading, acceptedSelection, refresh, invalidate }
}
