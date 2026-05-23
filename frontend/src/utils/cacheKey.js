export function paramsKey(params = {}) {
  return JSON.stringify(
    Object.keys(params)
      .sort()
      .reduce((result, key) => {
        if (params[key] !== undefined && params[key] !== null && params[key] !== '') {
          result[key] = params[key]
        }
        return result
      }, {})
  )
}
