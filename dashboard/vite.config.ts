import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // Relative asset paths so a built dashboard works wherever it is served
  // from — serve_frontend.py exposes the build at /dashboard/ (falling back
  // to index.html for the SPA), not at the domain root.
  base: './',
  // es2018: iOS 12 Safari (iPhone 6) predates `??`, optional catch binding
  // and private fields. Vite's default target (es2020 / safari14) keeps `??`
  // in the bundle, which fails to parse on the phone.
  build: {
    target: 'es2018',
  },
  server: {
    port: 5174,
  },
});
