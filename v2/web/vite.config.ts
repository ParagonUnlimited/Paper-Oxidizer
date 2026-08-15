import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    // Local dev: Vite serves the TS app, the Rust server runs on 8779.
    proxy: {
      '/login': 'http://127.0.0.1:8779',
      '/logout': 'http://127.0.0.1:8779',
      '/whoami': 'http://127.0.0.1:8779',
      '/page.img': 'http://127.0.0.1:8779',
      '/api': 'http://127.0.0.1:8779',
      '/healthz': 'http://127.0.0.1:8779',
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
});
