import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Built locally (deploy/deploy.sh sets VITE_DRYDOCK_LIVE=1), served as static files by the orchestrator: no npm on the VM.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { '/api': 'http://localhost:8000' } },
})
