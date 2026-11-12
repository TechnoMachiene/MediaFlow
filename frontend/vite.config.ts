import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// In development the API runs separately (uvicorn on :8000); proxy to it so the
// browser stays same-origin. In production FastAPI serves frontend/dist itself.
const api = process.env.MEDIAFLOW_API ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: api, changeOrigin: true },
      '/health': { target: api, changeOrigin: true },
    },
  },
});
