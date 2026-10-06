import http from 'node:http'
import { readFile, stat } from 'node:fs/promises'
import { resolve, extname, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
const root = fileURLToPath(new URL('./', import.meta.url))
const dist = resolve(root, 'dist')
const types = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png'
}
http
  .createServer(async (req, res) => {
    try {
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405)
        res.end()
        return
      }
      const url = new URL(req.url, 'http://localhost')
      const path = decodeURIComponent(url.pathname)
      let file = path === '/' ? resolve(root, 'index.html') : resolve(dist, '.' + path)
      if (path !== '/' && !file.startsWith(dist + sep)) {
        res.writeHead(403)
        res.end()
        return
      }
      if ((await stat(file)).isDirectory()) file = resolve(file, 'index.html')
      const body = await readFile(file)
      res.writeHead(200, {
        'Content-Type': types[extname(file)] || 'application/octet-stream',
        'Cache-Control': extname(file) === '.html' ? 'no-cache' : 'public, max-age=3600'
      })
      res.end(req.method === 'HEAD' ? undefined : body)
    } catch {
      res.writeHead(404)
      res.end('Not found')
    }
  })
  .listen(4318, '127.0.0.1', () => console.log('UI comparison: http://127.0.0.1:4318'))
