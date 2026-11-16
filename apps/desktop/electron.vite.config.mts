import react from '@vitejs/plugin-react';
import { defineConfig } from 'electron-vite';

import { devServerCsp } from './src/main/dev-server-csp';

export default defineConfig({
  main: {},
  preload: {},
  renderer: {
    plugins: [react(), devServerCsp()],
  },
});
