export type QualityGroup = 'backend' | 'frontend'
export type QualityStatus = 'current' | 'stale' | 'partial' | 'unknown'

export interface ReportError {
  path: string | null
  message: string
}

export interface QualityFunction {
  group: QualityGroup
  path: string
  name: string
  line: number
  end_line: number
  cc: number | null
  coverage: number | null
  crap: number | null
  status: 'measured' | 'not_applicable' | QualityStatus
  reason: string | null
}

export interface QualityReport {
  schema_version: number
  generated_at: string
  status: QualityStatus
  source: { root: string; files: Record<string, string>; config: Record<string, string> }
  scope: { method: string; formula: string }
  groups: Record<QualityGroup, { status: QualityStatus; reasons: string[] }>
  functions: QualityFunction[]
  errors: ReportError[]
}

export interface ReportSource {
  status: 'current' | 'stale' | 'unknown'
  can_navigate: boolean
  changed_files: string[]
}

export interface ReportListItem {
  id: string
  quality_status: QualityStatus | null
  ci_status: Record<QualityGroup, string | null>
  finished_at: string | null
  mutation_status?: string | null
}

export interface ReportList {
  reports: ReportListItem[]
  errors: ReportError[]
  truncated: boolean
}

export interface CheckGroup {
  status: string
  run_id: string
  run_attempt: string
  source: { commit: string | null; head: string | null; base: string | null; dirty: boolean }
  started_at: string | null
  finished_at: string | null
  steps: { id: string; status: string; exit_code: number | null; duration_seconds: number | null }[]
}

export interface GuardResult {
  rule: string
  test: string | null
  status: 'passed' | 'failed' | 'error' | 'skipped' | 'unknown'
  path: string | null
  line: number | null
  message: string | null
}

export interface ReportDetail {
  id: string
  quality: QualityReport | null
  ci: Record<QualityGroup, CheckGroup | null>
  guards: GuardResult[]
  source: ReportSource
  errors: ReportError[]
  mutation?: MutationReport | null
}

export interface MutationReport {
  status: string
  reason?: string | null
  tool: Record<string, string | null>
  scope: { path: string; function: string; tests: string[] }
  line: number | null
  steps: {
    baseline: { status: string; exit_code: number | null; duration_seconds: number | null }
    mutation: { status: string; exit_code: number | null; duration_seconds: number | null }
  }
  counts: Record<string, number>
  mutants: {
    name: string
    status: string
    native_status: string | null
    exit_code: number | null
    equivalence: string | null
  }[]
  mutation_score: number | null
  source_files: Record<string, string>
  source: ReportSource
  started_at: string | null
  finished_at: string | null
}

export interface MutationComparison {
  status: 'comparable' | 'incomparable'
  reasons: string[]
  before_score: number | null
  after_score: number | null
  delta: number | null
  change: 'improved' | 'worsened' | 'unchanged' | 'unknown'
  changed_test_files?: string[]
}

export type ChangeBucket = 'added' | 'removed' | 'worsened' | 'improved' | 'unchanged' | 'unknown'

export interface FunctionChange {
  group?: QualityGroup
  path?: string
  name?: string
  before: QualityFunction | null
  after: QualityFunction | null
  delta: number | null
  reason: string
  candidates?: { before: QualityFunction[]; after: QualityFunction[] }
}

export type Comparison = Record<ChangeBucket, FunctionChange[]> & {
  metric: 'crap'
  status: 'comparable' | 'partial' | 'incomparable'
  groups: Record<QualityGroup, { status: 'comparable' | 'incomparable'; reasons: string[] }>
}

export interface ComparisonResponse {
  before: { id: string; source: ReportSource }
  after: { id: string; source: ReportSource }
  comparison: Comparison
  guard_comparison?: GuardComparison
  errors: ReportError[]
}

export interface GuardChange {
  rule: string
  test: string | null
  before: GuardResult | null
  after: GuardResult | null
  reason: string
}

export type GuardComparison = Record<ChangeBucket, GuardChange[]> & {
  status: 'comparable' | 'incomparable'
  reasons: string[]
}

export interface QualitySourceLocation {
  path: string
  line: number
  sha256: string
}

export interface QualitySelection {
  report: ReportDetail | null
  threshold: number
}
