export interface SourceFile {
  path: string
  language: string
  sha256: string
}

export interface SourceSymbol {
  id: string
  path: string
  name: string
  kind: 'function' | 'class'
  line: number
  end_line: number
}

export interface Dependency {
  source: string
  target: string | null
  specifier: string
  line: number | null
  kind: 'runtime' | 'type' | 'dynamic'
  external: boolean
  statement?: string
}

export interface CycleHint {
  tool: 'grimp' | 'dependency-cruiser'
  kind: 'runtime' | 'type-only' | 'mixed'
  paths: string[]
  edges: {
    source: string
    target: string
    kind: Dependency['kind']
    line: number | null
  }[]
}

export interface ArchitectureIndex {
  files: SourceFile[]
  symbols: SourceSymbol[]
  dependencies: Dependency[]
  cycles?: CycleHint[]
  cycles_truncated?: boolean
  errors: { path: string | null; message: string }[]
  tools: Record<string, string>
  scanned_at: string
}
