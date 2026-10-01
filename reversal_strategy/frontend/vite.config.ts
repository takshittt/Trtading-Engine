import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Backend port: 8000 in DEMO, 8020 in LIVE (Shoonya gateway owns 8000).
const BACKEND = process.env.SWING_BACKEND_PORT || '8020'
const target = `http://localhost:${BACKEND}`

const proxy = {
  '/api/ws': { target, ws: true, changeOrigin: true },
  '/api': target,
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5176, proxy },
  // `vite preview` serves the built dist (used by the systemd UI service).
  preview: { host: '0.0.0.0', port: 5176, proxy },
})
