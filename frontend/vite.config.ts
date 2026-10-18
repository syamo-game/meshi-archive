import { defineConfig } from 'vite';

export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/static/app/' : '/',
  build: { outDir: '../web/static/app', emptyOutDir: true, manifest: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8021', '/auth': 'http://127.0.0.1:8021', '/logout': 'http://127.0.0.1:8021', '/media': 'http://127.0.0.1:8021', '/static': 'http://127.0.0.1:8021' } },
}));
