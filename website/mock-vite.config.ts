// Dev config for the headless UI-test harness only: serves the app on
// :5199 and proxies /api + /ws to the protocol mock on :8100.  Never used
// in normal development (vite.config.ts proxies to the real `tod serve`).
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5199,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8100', changeOrigin: true },
      '/ws': { target: 'http://127.0.0.1:8100', ws: true },
    },
  },
})
