import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend runs on 8010 by default (8000 is often taken by Docker).
// When changing the port, update both this file and the launch command in the README.
const BACKEND = process.env.VITE_BACKEND ?? 'http://127.0.0.1:8010'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/static': { target: BACKEND, changeOrigin: true },
      // Mirrors the nginx /tiles/ proxy in deploy/locswap-locations.inc.
      '/tiles': {
        target: 'https://tile.openstreetmap.org',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/tiles/, ''),
        headers: { 'User-Agent': 'LocSwap-dev/0.1' },
      },
    },
  },
})
