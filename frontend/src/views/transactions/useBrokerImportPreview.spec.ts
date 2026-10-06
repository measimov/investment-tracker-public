// @vitest-environment jsdom
import { effectScope, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { BrokerImportResult } from '@/types'
import { useBrokerImportPreview, type ImportSelection } from './useBrokerImportPreview'

function deferred() {
  let resolve!: (value: { data: BrokerImportResult }) => void
  let reject!: (error: Error) => void
  const promise = new Promise<{ data: BrokerImportResult }>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}
const result = { data: { imported_transactions: 1 } as BrokerImportResult }

function setup() {
  const selection = ref<ImportSelection | null>({
    file: new File(['first'], 'first.csv'),
    mode: 'ibkr',
    accountId: 1,
    confirmed: []
  })
  const scope = effectScope()
  const request = vi.fn<(input: ImportSelection) => Promise<{ data: BrokerImportResult }>>()
  const state = scope.run(() =>
    useBrokerImportPreview(
      () =>
        selection.value ? { ...selection.value, confirmed: [...selection.value.confirmed] } : null,
      request
    )
  )!
  return { selection, request, state, scope }
}

describe('broker preview authorization', () => {
  it.each(['file', 'mode', 'accountId', 'confirmed', 'close'])(
    'discards a response when %s changed and requires a new preview',
    async (change) => {
      const { selection, request, state, scope } = setup()
      const pending = deferred()
      request.mockReturnValueOnce(pending.promise)
      const refresh = state.refresh()
      if (change === 'close') selection.value = null
      else if (change === 'file') selection.value!.file = new File(['second'], 'second.csv')
      else if (change === 'mode') selection.value!.mode = 'cmb'
      else if (change === 'accountId') selection.value!.accountId = 2
      else selection.value!.confirmed.push('confirmed-row')
      pending.resolve(result)
      expect(await refresh).toBe(false)
      expect(state.preview.value).toBeNull()
      expect(state.acceptedSelection.value).toBeNull()
      expect(state.loading.value).toBe(false)
      scope.stop()
    }
  )

  it('a stale failure/finally cannot hide a newer request or show an error', async () => {
    const { selection, request, state, scope } = setup()
    const old = deferred()
    const current = deferred()
    request.mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
    const oldRefresh = state.refresh()
    selection.value!.accountId = 2
    const currentRefresh = state.refresh()
    old.reject(new Error('old error'))
    expect(await oldRefresh).toBe(false)
    expect(state.loading.value).toBe(true)
    current.resolve(result)
    expect(await currentRefresh).toBe(true)
    expect(state.acceptedSelection.value?.accountId).toBe(2)
    scope.stop()
  })

  it('invalidates on close/reopen even when the same file and account are selected', async () => {
    const { selection, request, state, scope } = setup()
    const old = deferred()
    const original = selection.value!
    request.mockReturnValueOnce(old.promise)
    const refresh = state.refresh()
    selection.value = null
    selection.value = original
    old.resolve(result)
    expect(await refresh).toBe(false)
    expect(state.acceptedSelection.value).toBeNull()
    scope.stop()
  })

  it('current errors block commit until a successful retry and use copied confirmations', async () => {
    const { selection, request, state, scope } = setup()
    request.mockRejectedValueOnce(new Error('current failure')).mockResolvedValueOnce(result)
    await expect(state.refresh()).rejects.toThrow('current failure')
    expect(state.acceptedSelection.value).toBeNull()
    selection.value!.confirmed = ['first-hash']
    expect(await state.refresh()).toBe(true)
    const accepted = state.acceptedSelection.value!
    expect(accepted.file).toBe(selection.value!.file)
    expect(accepted.confirmed).not.toBe(selection.value!.confirmed)
    selection.value!.confirmed.push('another-hash')
    expect(accepted.confirmed).toEqual(['first-hash'])
    expect(state.acceptedSelection.value).toBeNull()
    scope.stop()
  })

  it('does not update preview after its view is disposed', async () => {
    const { request, state, scope } = setup()
    const pending = deferred()
    request.mockReturnValueOnce(pending.promise)
    const refresh = state.refresh()
    scope.stop()
    pending.resolve(result)
    expect(await refresh).toBe(false)
    expect(state.preview.value).toBeNull()
  })
})
