import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { cloudflare } from '@cloudflare/vite-plugin';
export default defineConfig({
  plugins: [react(), cloudflare()],
  // Local Worker proxy disconnects are handled by the app; keep diagnostics in the terminal.
  // Type errors and build failures are still reported by the dedicated checks.
  server: { hmr: { overlay: false } },
});
