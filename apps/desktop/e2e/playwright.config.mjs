import { defineConfig } from '@playwright/test';

// Electron smoke E2E (run from the repository root: npm run test:e2e:smoke).
// The app must be built first; the root script does that.
export default defineConfig({
  testDir: '.',
  testMatch: 'smoke.spec.mjs',
  workers: 1,
  fullyParallel: false,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: [['list']],
  outputDir: '../../../test-results/e2e-smoke-artifacts',
});
