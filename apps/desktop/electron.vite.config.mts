import react from '@vitejs/plugin-react';
import { defineConfig } from 'electron-vite';
import type { Plugin } from 'vite';

const STRICT_CSP = "default-src 'self'";

// Vite dev server injects an inline React Refresh preamble and a HMR websocket,
// which the strict policy forbids. Relax the policy for `serve` only; the
// built renderer keeps the strict policy from index.html unchanged.
function devServerCsp(): Plugin {
  return {
    name: 'clipforge-dev-server-csp',
    apply: 'serve',
    transformIndexHtml(html) {
      const relaxed =
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws://localhost:* ws://127.0.0.1:*";
      return html.replace(`content="${STRICT_CSP}"`, `content="${relaxed}"`);
    },
  };
}

export default defineConfig({
  main: {},
  preload: {},
  renderer: {
    plugins: [react(), devServerCsp()],
  },
});
