import { defineConfig, mergeConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import baseConfig from './vitest.config'

export default mergeConfig(
  baseConfig,
  defineConfig({
    plugins: [vue()],
    test: {
      coverage: {
        provider: 'istanbul',
        all: true,
        include: ['src/**/*.ts', 'src/**/*.tsx', 'src/**/*.vue'],
        exclude: ['src/**/*.spec.ts', 'src/**/*.d.ts', 'src/types/api.generated.ts'],
        reporter: ['json', 'json-summary', 'lcovonly', 'html'],
        reportOnFailure: true
      }
    }
  })
)
