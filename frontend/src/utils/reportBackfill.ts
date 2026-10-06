interface BackfillOutcome {
  failed?: unknown
  failed_count?: unknown
  permanently_failed?: unknown
  digests_blocked?: unknown
  statements_blocked?: unknown
  statements_suspect?: unknown
  plan_incomplete?: unknown
  fatal?: unknown
  statements?: unknown
}

/** Single-security and batch backfills must use the same success/warning rules. */
export function backfillHasIssues(result: BackfillOutcome | null | undefined): boolean {
  if (!result) return false
  const statements =
    result.statements && typeof result.statements === 'object'
      ? (result.statements as Record<string, unknown>)
      : {}
  return (
    Boolean(result.fatal) ||
    result.plan_incomplete === true ||
    statements.plan_incomplete === true ||
    [
      result.failed,
      result.failed_count,
      result.permanently_failed,
      result.digests_blocked,
      result.statements_blocked,
      result.statements_suspect,
      statements.failed,
      statements.permanently_failed,
      statements.suspect
    ].some((count) => Number(count ?? 0) > 0)
  )
}
