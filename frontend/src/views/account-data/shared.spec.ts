import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { FormInstance } from 'element-plus'
import { makeSaver, reconciledAccountSummary } from './shared'

describe('reconciledAccountSummary（#286：按账户计）', () => {
  const accounts = [{ id: 1 }, { id: 2 }, { id: 3 }, { id: 4, is_active: false }]

  it('每个账户只看最近快照日，同日任一范围不一致即不算通过', () => {
    const snapshots = [
      { broker_account_id: 1, snapshot_date: '2026-08-31', status: 'MISMATCHED' },
      { broker_account_id: 1, snapshot_date: '2026-09-30', status: 'MATCHED' },
      { broker_account_id: 2, snapshot_date: '2026-09-30', status: 'MATCHED' },
      { broker_account_id: 2, snapshot_date: '2026-09-30', status: 'MISMATCHED' },
      { broker_account_id: 4, snapshot_date: '2026-09-30', status: 'MATCHED' }
    ]
    expect(reconciledAccountSummary(accounts, snapshots)).toEqual({ matched: 1, total: 3 })
  })

  it('没有快照的账户计入分母', () => {
    expect(reconciledAccountSummary(accounts, [])).toEqual({ matched: 0, total: 3 })
  })
})

vi.mock('element-plus', async (importOriginal) => ({
  ...(await importOriginal<typeof import('element-plus')>()),
  ElMessage: { success: vi.fn() }
}))
vi.mock('@/utils/showApiError', () => ({ showApiError: vi.fn() }))

it('parallel validation and pending saves cannot issue duplicate writes; failure preserves the draft for retry', async () => {
  let validated!: (result: boolean) => void, reject!: (reason: Error) => void
  const validation = new Promise<boolean>((resolve) => (validated = resolve))
  const validate = vi.fn().mockReturnValue(validation)
  const dialog = { visible: true, id: null, saving: false }
  const payload = { notes: 'UI明确虚构保留草稿' }
  const create = vi
    .fn()
    .mockReturnValueOnce(new Promise((_, fail) => (reject = fail)))
    .mockResolvedValueOnce({})
  const update = vi.fn(),
    reload = vi.fn().mockResolvedValue(undefined)
  const save = makeSaver({
    formRef: ref({ validate } as unknown as FormInstance),
    dialog,
    buildPayload: () => ({ ...payload }),
    create,
    update,
    reload,
    messages: { created: 'UI已新增', updated: 'UI已更新', failure: 'UI保存失败' }
  })
  const first = save(),
    second = save()
  expect(validate).toHaveBeenCalledTimes(2)
  validated(true)
  await vi.waitFor(() => expect(create).toHaveBeenCalledTimes(1))
  expect(create).toHaveBeenCalledWith(payload)
  expect(dialog.saving).toBe(true)
  await save()
  expect(validate).toHaveBeenCalledTimes(2)
  expect(create).toHaveBeenCalledTimes(1)
  reject(new Error('UI明确虚构503'))
  await Promise.all([first, second])
  expect(dialog.visible).toBe(true)
  expect(dialog.saving).toBe(false)
  expect(reload).not.toHaveBeenCalled()
  await save()
  expect(create).toHaveBeenCalledTimes(2)
  expect(update).not.toHaveBeenCalled()
  expect(dialog.visible).toBe(false)
  expect(dialog.saving).toBe(false)
  expect(reload).toHaveBeenCalledTimes(1)
})
