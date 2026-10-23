import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/ui/',
  build: { outDir: '../src/folio/web/dist', emptyOutDir: true },
  server: {
    proxy: Object.fromEntries(
      ['/conversations', '/uploads', '/jobs', '/templates', '/settings', '/knowledge'].map(
        path => [path, 'http://127.0.0.1:8000'],
      ),
    ),
  },
})
