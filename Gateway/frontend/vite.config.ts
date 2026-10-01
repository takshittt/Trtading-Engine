import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss()
  ],
  server: {
    proxy: {
      '/api/ws': {
        target: 'http://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
      '/api': 'http://localhost:8000',
    },
  },
  preview: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api/ws': {
        target: 'http://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
      '/api': 'http://localhost:8000',
    },
  },
})
