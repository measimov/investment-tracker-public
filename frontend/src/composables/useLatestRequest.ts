import { getCurrentScope, onScopeDispose } from 'vue'

/** Only the latest request may update the view, its errors, or its loading state. */
export function useLatestRequest() {
  let sequence = 0
  let disposed = false
  const invalidate = () => ++sequence
  if (getCurrentScope()) {
    onScopeDispose(() => {
      disposed = true
      invalidate()
    })
  }
  return {
    begin: invalidate,
    invalidate,
    isCurrent: (request: number) => !disposed && request === sequence
  }
}
