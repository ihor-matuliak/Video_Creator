import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { _electron as electron, expect, test } from '@playwright/test';

// docs/03 section 6 and ADR 041 §23: the «Що шукати» panel in a real window. A fresh profile has no
// transcript and no analysis model. The panel says what is missing; a subtitle file read by main
// unlocks «Знайти моменти»; the estimate then stops at «Відкрити Налаштування», because no model is
// chosen. Nothing is sent to a provider here, and «Запустити пошук (платно)» is never clicked.

const ENTRY = resolve(import.meta.dirname, '../out/main/index.js');
const FIXTURES = resolve(import.meta.dirname, '../../../test-results/fixtures');
const PROJECT_NAME = 'Пошук моментів';
// color_gop.mp4 lasts 12 000 ms; both cues lie inside it, so the backend accepts the file.
const SRT = [
  '1',
  '00:00:00,000 --> 00:00:04,000',
  'The first subtitle line.',
  '',
  '2',
  '00:00:04,000 --> 00:00:08,000',
  'The second subtitle line.',
  '',
].join('\n');

/** The next native picker answers with `path`. */
async function pickNext(app, path) {
  await app.evaluate(
    ({ dialog }, picked) => {
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [picked] });
    },
    path,
  );
}

/** Starts the app on `dataDir`; the native picker answers with `file`. */
async function launch(dataDir, file) {
  expect(existsSync(ENTRY), `build the app first: ${ENTRY}`).toBe(true);
  expect(existsSync(file), `generate the fixtures first (npm run fixtures): ${file}`).toBe(true);
  const env = { ...process.env, CLIPFORGE_E2E: '1', CLIPFORGE_DATA_DIR: dataDir };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.CLIPFORGE_MODE;

  const app = await electron.launch({ args: [ENTRY], env });
  await pickNext(app, file);
  const page = await app.firstWindow();
  await expect(page.getByTestId('app-ready')).toBeVisible();
  return { app, page };
}

/** Removes a temp folder; Windows lets go of the database file a moment after the sidecar exits. */
function removeFolder(path) {
  try {
    rmSync(path, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
  } catch {
    // A temp folder that stays behind is not a failure of the search panel.
  }
}

test('color_gop.mp4: the search waits for a transcript, then stops at the settings', async () => {
  const dataDir = mkdtempSync(join(tmpdir(), 'clipforge-search-'));
  const subtitleDir = mkdtempSync(join(tmpdir(), 'clipforge-search-srt-'));
  let app = null;
  try {
    const launched = await launch(dataDir, join(FIXTURES, 'color_gop.mp4'));
    app = launched.app;
    const page = launched.page;

    await page.getByTestId('new-project').click();
    await page.getByTestId('project-name').fill(PROJECT_NAME);
    await page.getByTestId('create-project').click();
    await page.getByTestId('import-video').click();
    await expect(page.getByTestId('source-ready')).toBeVisible();

    // Collapsed by default: the panel body is not on screen yet.
    const settings = page.getByTestId('search-settings');
    await expect(settings).toHaveAttribute('aria-expanded', 'false');
    await expect(page.getByTestId('search-caption')).not.toBeVisible();
    await settings.click();
    await expect(page.getByTestId('search-caption')).toBeVisible();
    await expect(page.getByTestId('find-highlights')).toBeDisabled();
    await expect(page.getByTestId('find-reason')).toBeVisible();

    // A real subtitle file: main reads it and the backend stores a new transcript revision.
    const srtPath = join(subtitleDir, 'subtitles.srt');
    writeFileSync(srtPath, SRT, 'utf8');
    await pickNext(app, srtPath);
    await page.getByTestId('transcript-import-button').click();
    await expect(page.getByTestId('transcript-import-result')).toBeVisible();
    await expect(page.getByTestId('find-highlights')).toBeEnabled();

    // A fresh profile has no analysis model: the estimate is refused and the settings are offered.
    await page.getByTestId('find-highlights').click();
    await expect(page.getByTestId('search-error')).toContainText('Оберіть модель аналізу в Налаштуваннях');
    await expect(page.getByTestId('search-open-settings')).toBeVisible();
  } finally {
    await app?.close().catch(() => undefined);
    removeFolder(dataDir);
    removeFolder(subtitleDir);
  }
});
