import { describe, expect, it, vi } from 'vitest'
vi.mock('element-plus', () => ({ ElMessage: { info: vi.fn(), error: vi.fn() } }))
import { ElMessage } from 'element-plus'
import { showApiError } from './showApiError'
import { PollingTimeoutError } from './polling'
import { validateForm } from './validateForm'
import { backfillHasIssues } from './reportBackfill'

describe('financial feedback', () => {
  it('polling timeout is informational and never says the background task failed', () => {
    showApiError(new PollingTimeoutError('任务仍在后台运行'), { prefix: '任务失败' })
    expect(ElMessage.info).toHaveBeenCalledWith('任务仍在后台运行')
    expect(ElMessage.error).not.toHaveBeenCalled()
  })
  it('invalid form rejection is expected, while a valid form can submit', async () => {
    expect(
      await validateForm({
        validate: async () => {
          throw { quantity: 'required' }
        }
      })
    ).toBe(false)
    expect(await validateForm(null)).toBe(false)
    expect(await validateForm({ validate: async () => true })).toBe(true)
  })
  it('single and batch backfills flag failed, blocked, incomplete and suspect output', () => {
    expect(backfillHasIssues({ failed: 1 })).toBe(true)
    expect(backfillHasIssues({ permanently_failed: 1 })).toBe(true)
    expect(backfillHasIssues({ plan_incomplete: true })).toBe(true)
    expect(backfillHasIssues({ statements: { suspect: 1 } })).toBe(true)
    expect(backfillHasIssues({ failed_count: 1 })).toBe(true)
    expect(backfillHasIssues({ statements_blocked: 1 })).toBe(true)
    expect(backfillHasIssues({ statements_suspect: 1 })).toBe(true)
    expect(backfillHasIssues({ failed: 0, statements: { failed: 0 } })).toBe(false)
  })
})
