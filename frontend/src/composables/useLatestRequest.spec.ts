import { effectScope } from 'vue'
import { describe, expect, it } from 'vitest'
import { useLatestRequest } from './useLatestRequest'

describe('latest request guard', () => {
  it('only the most recent live request can update result, error, and loading', () => {
    const scope = effectScope()
    const guard = scope.run(useLatestRequest)!
    const first = guard.begin()
    const second = guard.begin()
    expect(guard.isCurrent(first)).toBe(false)
    expect(guard.isCurrent(second)).toBe(true)
    guard.invalidate()
    expect(guard.isCurrent(second)).toBe(false)
    const third = guard.begin()
    scope.stop()
    expect(guard.isCurrent(third)).toBe(false)
  })
})
