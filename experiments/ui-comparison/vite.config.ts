import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig(async () => {
  const variant = process.env.UI_VARIANT || 'element'
  if (!['element', 'naive', 'nuxt', 'design'].includes(variant))
    throw new Error('Unknown UI variant')
  const plugins: any[] = [vue()]
  if (variant === 'nuxt') {
    const { default: ui } = await import('@nuxt/ui/vite')
    plugins.push(
      ui({
        ui: { colors: { primary: 'clay', neutral: 'stone' } },
        autoImport: false,
        components: false
      })
    )
  }
  return {
    root: fileURLToPath(new URL(`./${variant}/`, import.meta.url)),
    base: `/${variant}/`,
    plugins,
    resolve: {
      alias: { '@': fileURLToPath(new URL('../../frontend/src/', import.meta.url)) },
      dedupe: ['vue']
    },
    build: {
      outDir: fileURLToPath(new URL(`./dist/${variant}/`, import.meta.url)),
      emptyOutDir: true,
      manifest: true,
      target: 'es2020',
      reportCompressedSize: true
    }
  }
})
