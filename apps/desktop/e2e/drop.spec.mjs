/* global document, DataTransfer, DragEvent -- used inside page.evaluate callbacks, which run in the page */
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { _electron as electron, expect, test } from '@playwright/test';

// docs/03 section 5 and ADR 024 in a real window: a video dropped on the zone of the first-run
// screen or of the «Джерела» tab is registered like the one of «Додати відео». The path of the
// dropped file goes from the preload to main only (webUtils.getPathForFile), so the file must be a
// real one from the disk: a File that a temporary <input type="file"> got from setInputFiles.

const ENTRY = resolve(import.meta.dirname, '../out/main/index.js');
const FIXTURES = resolve(import.meta.dirname, '../../../test-results/fixtures');
const FIRST_VIDEO = join(FIXTURES, 'Київ запис.mp4');
const SECOND_VIDEO = join(FIXTURES, 'color_gop.mp4');
const PROJECT_NAME = 'Київ запис';

/** Starts the app on `dataDir`. The native picker is not replaced: nothing here may open it. */
async function launch(dataDir) {
  expect(existsSync(ENTRY), `build the app first: ${ENTRY}`).toBe(true);
  for (const fixture of [FIRST_VIDEO, SECOND_VIDEO]) {
    expect(existsSync(fixture), `generate the fixtures first (npm run fixtures): ${fixture}`).toBe(true);
  }
  const env = { ...process.env, CLIPFORGE_E2E: '1', CLIPFORGE_DATA_DIR: dataDir };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.CLIPFORGE_MODE;

  const app = await electron.launch({ args: [ENTRY], env });
  const page = await app.firstWindow();
  await expect(page.getByTestId('app-ready')).toBeVisible();
  return { app, page };
}

/** Runs `body(page, scratch)` with a fresh data folder and a scratch folder for extra files. */
function withApp(body) {
  return async () => {
    const root = mkdtempSync(join(tmpdir(), 'clipforge-drop-'));
    const dataDir = join(root, 'data');
    const scratch = join(root, 'scratch');
    mkdirSync(dataDir);
    mkdirSync(scratch);
    let app = null;
    try {
      const launched = await launch(dataDir);
      app = launched.app;
      await body(launched.page, scratch);
    } finally {
      await app?.close().catch(() => undefined);
      // Windows lets go of the database file a moment after the sidecar has exited.
      try {
        rmSync(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
      } catch {
        // A temp folder that stays behind is not a failure of the drop zone.
      }
    }
  };
}

/**
 * Puts the files from the disk into a DataTransfer, as a drag from the file manager does, and
 * sends the drag events `events` (in order) to the element with the test id `testId`. The Files
 * are those of a temporary file input, which is what gives them a path for `getPathForFile`.
 */
async function dragFiles(page, testId, paths, events) {
  await page.evaluate(() => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.id = 'e2e-drop-files';
    document.body.append(input);
  });
  await page.setInputFiles('#e2e-drop-files', paths);
  await page.evaluate(
    ({ testId: target, events: names }) => {
      const input = document.getElementById('e2e-drop-files');
      const transfer = new DataTransfer();
      for (const file of input.files) {
        transfer.items.add(file);
      }
      const element = document.querySelector(`[data-testid="${target}"]`);
      for (const name of names) {
        element.dispatchEvent(new DragEvent(name, { bubbles: true, cancelable: true, dataTransfer: transfer }));
      }
      input.remove();
    },
    { testId, events },
  );
}

const dropFiles = (page, testId, paths) => dragFiles(page, testId, paths, ['dragenter', 'dragover', 'drop']);

/** The first-run screen, once its checks are done and «Додати відео» is available. */
async function firstRunReady(page) {
  await expect(page.getByTestId('first-run')).toBeVisible();
  await expect(page.getByTestId('add-video')).toBeEnabled();
  await expect(page.getByTestId('drop-zone')).toBeVisible();
}

test(
  'first run: a dropped «Київ запис.mp4» opens the project «Київ запис» with the source ready',
  withApp(async (page) => {
    await firstRunReady(page);

    await dropFiles(page, 'drop-zone', [FIRST_VIDEO]);

    await expect(page.getByRole('heading', { level: 2, name: PROJECT_NAME })).toBeVisible();
    await expect(page.getByTestId('source-ready')).toContainText('Київ запис.mp4');
    await expect(page.getByRole('tab', { name: 'Джерела' })).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByTestId('first-run')).toHaveCount(0);
    await expect(page.getByTestId('drop-error')).toHaveCount(0);
    await expect(page.getByTestId('first-run-error')).toHaveCount(0);
    await expect(page.getByTestId('import-error')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);

    // The project is real: the list shows it, and the first-run screen is gone.
    await page
      .getByRole('navigation', { name: 'Навігаційний ланцюжок' })
      .getByRole('button', { name: 'Проєкти' })
      .click();
    await expect(page.getByTestId('project-item')).toHaveCount(1);
    await expect(page.getByTestId('project-item')).toContainText(PROJECT_NAME);
  }),
);

test(
  'first run: a file over the zone shows the hover state with text, and leaving it hides the text',
  withApp(async (page) => {
    await firstRunReady(page);
    await expect(page.getByTestId('drop-zone-active')).toHaveCount(0);

    await dragFiles(page, 'drop-zone', [FIRST_VIDEO], ['dragenter', 'dragover']);

    await expect(page.getByTestId('drop-zone-active')).toBeVisible();
    await expect(page.getByTestId('drop-zone-active')).toContainText('Відпустіть, щоб додати відео');

    await dragFiles(page, 'drop-zone', [FIRST_VIDEO], ['dragleave']);

    await expect(page.getByTestId('drop-zone-active')).toHaveCount(0);
    await expect(page.getByTestId('first-run')).toBeVisible();
  }),
);

test(
  '«Джерела»: a dropped file is registered in the open project as a second source',
  withApp(async (page) => {
    await firstRunReady(page);
    await dropFiles(page, 'drop-zone', [FIRST_VIDEO]);
    await expect(page.getByTestId('source-ready')).toContainText('Київ запис.mp4');
    await expect(page.getByRole('list', { name: 'Джерела проєкту' })).toHaveCount(0);

    await dropFiles(page, 'drop-zone', [SECOND_VIDEO]);

    const list = page.getByRole('list', { name: 'Джерела проєкту' });
    await expect(list.getByRole('button')).toHaveCount(2);
    await expect(list.getByRole('button', { name: 'Київ запис.mp4' })).toBeVisible();
    await expect(list.getByRole('button', { name: 'color_gop.mp4' })).toBeVisible();
    await expect(page.getByTestId('source-ready')).toContainText('color_gop.mp4');
    await expect(page.getByRole('heading', { level: 2, name: PROJECT_NAME })).toBeVisible();
    await expect(page.getByTestId('drop-error')).toHaveCount(0);
    await expect(page.getByTestId('import-error')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
  }),
);

test(
  'first run: a text file is refused with «Це не відеофайл…», and no project is created',
  withApp(async (page, scratch) => {
    const notes = join(scratch, 'notes.txt');
    writeFileSync(notes, 'це не відео\n');
    await firstRunReady(page);

    await dropFiles(page, 'drop-zone', [notes]);

    const error = page.getByTestId('drop-error');
    await expect(error).toContainText('Це не відеофайл');
    // The message says what to do and never where the file was.
    await expect(error).not.toContainText('notes.txt');
    await expect(error).not.toContainText(scratch);
    await expect(page.getByTestId('first-run')).toBeVisible();
    await expect(page.getByTestId('add-video')).toBeEnabled();
    await expect(page.getByRole('heading', { level: 2, name: PROJECT_NAME })).toHaveCount(0);
    await expect(page.getByTestId('source-ready')).toHaveCount(0);
  }),
);

test(
  'first run: two files are refused with «Перетягніть один відеофайл»',
  withApp(async (page) => {
    await firstRunReady(page);

    await dropFiles(page, 'drop-zone', [FIRST_VIDEO, SECOND_VIDEO]);

    await expect(page.getByTestId('drop-error')).toContainText('Перетягніть один відеофайл');
    await expect(page.getByTestId('first-run')).toBeVisible();
    await expect(page.getByRole('heading', { level: 2, name: PROJECT_NAME })).toHaveCount(0);
    await expect(page.getByTestId('source-ready')).toHaveCount(0);
  }),
);

test(
  'a file dropped outside the zone is ignored: nothing opens, nothing is registered',
  withApp(async (page) => {
    await firstRunReady(page);
    const url = page.url();

    await dropFiles(page, 'first-run', [FIRST_VIDEO]);
    // A drop that was taken would create the project within a second or two; give it that time.
    await page.waitForTimeout(2000);

    await expect(page.getByTestId('first-run')).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expect(page.getByRole('heading', { level: 2, name: PROJECT_NAME })).toHaveCount(0);
    await expect(page.getByTestId('source-ready')).toHaveCount(0);
    expect(page.url()).toBe(url);
  }),
);
