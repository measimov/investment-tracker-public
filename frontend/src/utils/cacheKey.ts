export function paramsKey(params: Record<string, unknown> = {}): string {
  return JSON.stringify(
    Object.keys(params)
      .sort()
      .reduce<Record<string, unknown>>((result, key) => {
        if (params[key] !== undefined && params[key] !== null && params[key] !== '') {
          result[key] = params[key]
        }
        return result
      }, {})
  )
}
