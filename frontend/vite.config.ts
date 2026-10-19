/// <reference types="vitest/config" />
import { fileURLToPath } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const backend = 'http://127.0.0.1:8765'
const proxied = ['/api', '/admin/api', '/frame', '/assets', '/ae', '/demo', '/static/logo.png', '/static/icon.png']

export default defineConfig({
  base: '/static/app/',
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: { outDir: '../keepframe/web/static/app', emptyOutDir: true, chunkSizeWarningLimit: 900 },
  server: { proxy: Object.fromEntries(proxied.map((p) => [p, backend])) },
  test: { environment: 'jsdom', include: ['src/**/*.test.ts', 'src/**/*.test.tsx'] },
})
