import fs from 'node:fs/promises'
import path from 'node:path'
import { gzipSync } from 'node:zlib'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
const root = path.dirname(fileURLToPath(import.meta.url))
const result = {
  at: new Date().toISOString(),
  unit: 'bytes',
  method:
    'Independent Vite production entry assets; local gzip, not HTTP transfer or whole application size.',
  variants: []
}
for (const variant of ['element', 'naive', 'nuxt']) {
  const dir = path.join(root, 'dist', variant)
  const manifest = JSON.parse(await fs.readFile(path.join(dir, '.vite/manifest.json'), 'utf8'))
  const entry = Object.values(manifest).find((item) => item.isEntry)
  const names = [entry.file, ...(entry.css || [])]
  const assets = []
  for (const name of names) {
    const body = await fs.readFile(path.join(dir, name))
    assets.push({
      name,
      raw: body.length,
      gzip: gzipSync(body).length,
      sha256: createHash('sha256').update(body).digest('hex')
    })
  }
  result.variants.push({
    variant,
    assets,
    totalGzip: assets.reduce((sum, item) => sum + item.gzip, 0)
  })
}
await fs.mkdir(path.join(root, 'results'), { recursive: true })
await fs.writeFile(path.join(root, 'results/build-sizes.json'), JSON.stringify(result, null, 2))
console.log(
  result.variants
    .map((item) => `${item.variant}: ${(item.totalGzip / 1000).toFixed(1)} KB gzip (JS + CSS)`)
    .join('\n')
)
