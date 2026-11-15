import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { _electron as electron, expect, test } from '@playwright/test';

// docs/03 section 3 and ADR 023/024 in a real window: a user with no project sees the first-run
// screen with the readiness of FFmpeg, FFprobe and the data folder (from the real backend), and
// «Додати відео» creates a project named after the file and registers the video in it. The native
// picker is replaced, so the path goes main -> backend as in production, through a token.

const ENTRY = resolve(import.meta.dirname, '../out/main/index.js');
const FIXTURE = resolve(import.meta.dirname, '../../../test-results/fixtures/Київ запис.mp4');
const PROJECT_NAME = 'Київ запис';

/** Starts the app on `dataDir`; the native picker answers with the fixture without a dialog. */
async function launch(dataDir) {
  expect(existsSync(ENTRY), `build the app first: ${ENTRY}`).toBe(true);
  expect(existsSync(FIXTURE), `generate the fixtures first (npm run fixtures): ${FIXTURE}`).toBe(true);
  const env = { ...process.env, CLIPFORGE_E2E: '1', CLIPFORGE_DATA_DIR: dataDir };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.CLIPFORGE_MODE;

  const app = await electron.launch({ args: [ENTRY], env });
  await app.evaluate(({ dialog }, picked) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [picked] });
  }, FIXTURE);
  const page = await app.firstWindow();
  await expect(page.getByTestId('app-ready')).toBeVisible();
  return { app, page };
}

/** Runs `body` with a fresh data folder and the app on it; closes the app and removes the folder. */
function withApp(body) {
  return async () => {
    const dataDir = mkdtempSync(join(tmpdir(), 'clipforge-first-run-'));
    let app = null;
    try {
      const launched = await launch(dataDir);
      app = launched.app;
      await body(launched.page);
    } finally {
      await app?.close().catch(() => undefined);
      // Windows lets go of the database file a moment after the sidecar has exited.
      try {
        rmSync(dataDir, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
      } catch {
        // A temp folder that stays behind is not a failure of the first-run screen.
      }
    }
  };
}

test(
  'clean data: the first-run screen shows the readiness of FFmpeg, FFprobe and the data folder',
  withApp(async (page) => {
    const firstRun = page.getByTestId('first-run');
    await expect(firstRun).toBeVisible();
    await expect(
      firstRun.getByRole('heading', { level: 3, name: 'Знайдімо цікаві моменти у твоєму відео' }),
    ).toBeVisible();

    // The answers come from the real backend: both tools are found and the folder is writable.
    await expect(page.getByTestId('system-check-ffmpeg')).toContainText('FFmpeg готовий');
    await expect(page.getByTestId('system-check-ffprobe')).toContainText('FFprobe готовий');
    await expect(page.getByTestId('system-check-data-dir')).toContainText('Папка даних готова');
    await expect(page.getByTestId('add-video')).toBeEnabled();
    await expect(page.getByTestId('add-video-reason')).toHaveCount(0);

    // «Новий проєкт» stays, and the list has nothing but this screen.
    await expect(page.getByTestId('new-project')).toBeVisible();
    await expect(page.getByTestId('project-item')).toHaveCount(0);
    await expect(page.getByTestId('projects-all-archived')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
  }),
);

test(
  '«Додати відео» with «Київ запис.mp4» opens the project «Київ запис» with the source ready',
  withApp(async (page) => {
    await expect(page.getByTestId('first-run')).toBeVisible();

    await page.getByTestId('add-video').click();

    await expect(page.getByRole('heading', { level: 2, name: PROJECT_NAME })).toBeVisible();
    await expect(page.getByTestId('source-ready')).toBeVisible();
    await expect(page.getByTestId('source-ready')).toContainText('Київ запис.mp4');
    await expect(page.getByRole('tab', { name: 'Джерела' })).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByTestId('first-run')).toHaveCount(0);
    await expect(page.getByTestId('first-run-error')).toHaveCount(0);
    await expect(page.getByTestId('import-error')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);

    // The project is real: the list shows it and no longer shows the first-run screen.
    await page
      .getByRole('navigation', { name: 'Навігаційний ланцюжок' })
      .getByRole('button', { name: 'Проєкти' })
      .click();
    await expect(page.getByTestId('project-item')).toHaveCount(1);
    await expect(page.getByTestId('project-item')).toContainText(PROJECT_NAME);
    await expect(page.getByTestId('first-run')).toHaveCount(0);
  }),
);
