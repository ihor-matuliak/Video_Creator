import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { _electron as electron, expect, test } from '@playwright/test';

// docs/03 section 5: the source inspector in a real window. After an import it shows the resolution,
// the frame rate, the VFR badge, how the video is viewed (and why a preview copy is needed) and the
// note that the video stays on this computer. The facts come from the real probe of a real file
// through main, so jsdom cannot stand in for this.

const ENTRY = resolve(import.meta.dirname, '../out/main/index.js');
const FIXTURES = resolve(import.meta.dirname, '../../../test-results/fixtures');
const PROJECT_NAME = 'Інспектор джерела';

/** Starts the app on `dataDir`; the native picker answers with `fixture` from test-results/fixtures. */
async function launch(dataDir, fixture) {
  const file = join(FIXTURES, fixture);
  expect(existsSync(ENTRY), `build the app first: ${ENTRY}`).toBe(true);
  expect(existsSync(file), `generate the fixtures first (npm run fixtures): ${file}`).toBe(true);
  const env = { ...process.env, CLIPFORGE_E2E: '1', CLIPFORGE_DATA_DIR: dataDir };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.CLIPFORGE_MODE;

  const app = await electron.launch({ args: [ENTRY], env });
  await app.evaluate(
    ({ dialog }, picked) => {
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [picked] });
    },
    file,
  );
  const page = await app.firstWindow();
  await expect(page.getByTestId('app-ready')).toBeVisible();
  return { app, page };
}

/** Creates a project, imports `fixture` into it and returns the page with the inspector open. */
async function importFixture(dataDir, fixture) {
  const { app, page } = await launch(dataDir, fixture);
  await page.getByTestId('new-project').click();
  await page.getByTestId('project-name').fill(PROJECT_NAME);
  await page.getByTestId('create-project').click();
  await page.getByTestId('import-video').click();
  await expect(page.getByTestId('source-ready')).toBeVisible();
  return { app, page };
}

/** Runs `body` with a fresh data folder; closes the app it launched and removes the folder. */
function withInspector(fixture, body) {
  return async () => {
    const dataDir = mkdtempSync(join(tmpdir(), 'clipforge-source-'));
    let app = null;
    try {
      const imported = await importFixture(dataDir, fixture);
      app = imported.app;
      await body(imported.page);
    } finally {
      await app?.close().catch(() => undefined);
      // Windows lets go of the database file a moment after the sidecar has exited.
      try {
        rmSync(dataDir, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
      } catch {
        // A temp folder that stays behind is not a failure of the inspector.
      }
    }
  };
}

test(
  'color_gop.mp4: constant frame rate, played directly',
  withInspector('color_gop.mp4', async (page) => {
    await expect(page.getByTestId('source-resolution')).toHaveText('320×180');
    await expect(page.getByTestId('source-fps')).toHaveText('30 к/с');
    await expect(page.getByTestId('source-vfr')).toHaveText('✓ Постійна частота кадрів');
    await expect(page.getByTestId('source-playback')).toHaveText('Напряму');
    await expect(page.getByTestId('source-local-note')).toHaveText("Відео залишається на цьому комп'ютері.");
  }),
);

test(
  'vfr.mkv: variable frame rate, a copy is needed for viewing',
  withInspector('vfr.mkv', async (page) => {
    await expect(page.getByTestId('source-resolution')).toHaveText('320×180');
    // The exact average depends on the timestamps of the file: a number with a decimal comma.
    await expect(page.getByTestId('source-fps')).toHaveText(/^\d+(,\d{1,3})? к\/с$/);
    await expect(page.getByTestId('source-vfr')).toHaveText('⚠ Змінна частота кадрів (VFR)');
    await expect(page.getByTestId('source-playback')).toHaveText(
      'Потрібна копія для перегляду. Причина: контейнер файлу не підтримується програвачем',
    );
    await expect(page.getByTestId('source-local-note')).toHaveText("Відео залишається на цьому комп'ютері.");
  }),
);
