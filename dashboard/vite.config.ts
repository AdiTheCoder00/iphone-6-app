import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // Relative asset paths so a built dashboard works wherever it is served
  // from — the repo's `python -m http.server` exposes it at
  // /dashboard/dist/, not at the domain root.
  base: './',
  server: {
    port: 5174,
  },
});
