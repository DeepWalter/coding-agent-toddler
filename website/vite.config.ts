import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev server proxies /api and /ws to a running `tod serve` on :8000 —
// the production path is the static mount in toddler/web/app.py, which
// serves website/dist from the same origin.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'http://localhost:8000', ws: true },
    },
  },
})
