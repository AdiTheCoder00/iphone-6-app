import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // Relative asset paths so a built dashboard works wherever it is served
  // from — serve_frontend.py exposes the build at /dashboard/ (falling back
  // to index.html for the SPA), not at the domain root.
  base: './',
  server: {
    port: 5174,
  },
});
