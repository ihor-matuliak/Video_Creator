import { randomUUID } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { _electron as electron, expect, test } from '@playwright/test';

// After a restart the user sees their work again (docs/03 section 12): the project that was
// open, its tab and the sources and clips registered in the earlier run. Every launch of a
// test uses the same CLIPFORGE_DATA_DIR, which is what makes it "the same installation".

const ENTRY = resolve(import.meta.dirname, '../out/main/index.js');
const FIXTURE = resolve(import.meta.dirname, '../../../test-results/fixtures/color_gop.mp4');
const STATE_FILE = 'ui-state.json';
const PROJECT_NAME = 'Перезапуск';

/** Starts the app on `dataDir`; the video picker answers with the fixture without a dialog. */
async function launch(dataDir) {
  expect(existsSync(ENTRY), `build the app first: ${ENTRY}`).toBe(true);
  expect(existsSync(FIXTURE), `generate the fixtures first (npm run fixtures): ${FIXTURE}`).toBe(true);
  const env = { ...process.env, CLIPFORGE_E2E: '1', CLIPFORGE_DATA_DIR: dataDir };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.CLIPFORGE_MODE;

  const app = await electron.launch({ args: [ENTRY], env });
  await app.evaluate(({ dialog }, file) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [file] });
  }, FIXTURE);
  const page = await app.firstWindow();
  await expect(page.getByTestId('app-ready')).toBeVisible();
  return { app, page };
}

/** Creates the project and imports the fixture into it, leaving the app on «Джерела». */
async function createProjectWithSource(page) {
  await page.getByTestId('new-project').click();
  await page.getByTestId('project-name').fill(PROJECT_NAME);
  await page.getByTestId('create-project').click();
  await page.getByTestId('import-video').click();
  await expect(page.getByTestId('source-ready')).toBeVisible();
}

function readState(dataDir) {
  const text = readFileSync(join(dataDir, STATE_FILE), 'utf8');
  return { text, document: JSON.parse(text) };
}

/** Runs `body` with a fresh data folder and closes every app it launched, also on failure. */
function withWorkspace(body) {
  return async () => {
    const dataDir = mkdtempSync(join(tmpdir(), 'clipforge-restart-'));
    const apps = [];
    const open = async () => {
      const launched = await launch(dataDir);
      apps.push(launched.app);
      return launched;
    };
    try {
      await body({ dataDir, open });
    } finally {
      for (const app of apps) {
        await app.close().catch(() => undefined);
      }
      rmSync(dataDir, { recursive: true, force: true });
    }
  };
}

test(
  'import a video, close the app, relaunch on the same data folder: the project and its source are back',
  withWorkspace(async ({ dataDir, open }) => {
    const first = await open();
    await createProjectWithSource(first.page);
    await expect(first.page.getByTestId('source-ready')).toContainText('color_gop.mp4');
    await first.app.close();

    // The file (schema v2) holds the open project, its tab and the panel widths and nothing else:
    // no path, no name, no token.
    const { text, document } = readState(dataDir);
    expect(Object.keys(document).sort()).toEqual(['layout', 'project_id', 'tab', 'version']);
    expect(document).toMatchObject({ version: 2, tab: 'sources', layout: { list_px: 320, details_px: 320 } });
    expect(document.project_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(text).not.toMatch(/color_gop|[A-Za-z]:\\|\\\\|[0-9a-f]{64}/);
    expect(text).not.toContain(dataDir);

    const second = await open();

    // Nothing is clicked: the project opens by itself, on the tab that was left.
    await expect(second.page.getByRole('heading', { level: 2, name: PROJECT_NAME })).toBeVisible();
    await expect(second.page.getByTestId('source-ready')).toBeVisible();
    await expect(second.page.getByTestId('source-ready')).toContainText('color_gop.mp4');
    await expect(second.page.getByRole('tab', { name: 'Джерела' })).toHaveAttribute('aria-selected', 'true');
    await expect(second.page.getByTestId('project-item')).toHaveCount(0);
    await expect(second.page.getByRole('alert')).toHaveCount(0);
    await expect(second.page.getByTestId('app-error')).toHaveCount(0);
  }),
);

test(
  'the clips of an earlier run and the last tab come back after a relaunch',
  withWorkspace(async ({ open }) => {
    const first = await open();
    await createProjectWithSource(first.page);
    await first.page.getByTestId('manual-clip').click();
    await first.page.getByTestId('clip-title').fill('Мій фрагмент');
    await first.page.getByTestId('clip-start').fill('00:00:04.120');
    await first.page.getByTestId('clip-end').fill('00:00:07.880');
    await first.page.getByTestId('save-clip').click();
    await expect(first.page.getByTestId('clip-card')).toContainText('Мій фрагмент');
    await first.app.close();

    const second = await open();

    // «Моменти» was the tab on screen: it is open again, with the source and its clip.
    await expect(second.page.getByRole('tab', { name: 'Моменти' })).toHaveAttribute('aria-selected', 'true');
    await expect(second.page.getByTestId('clip-card')).toHaveCount(1);
    await expect(second.page.getByTestId('clip-card')).toContainText('Мій фрагмент');
    await expect(second.page.getByTestId('player')).toBeVisible();
    // The video does not start by itself.
    await expect(second.page.getByTestId('player-video')).toHaveJSProperty('paused', true);

    await second.page.getByRole('tab', { name: 'Джерела' }).click();
    await expect(second.page.getByTestId('source-ready')).toContainText('color_gop.mp4');
    await expect(second.page.getByRole('alert')).toHaveCount(0);
  }),
);

test(
  'a remembered project that does not exist falls back to the project list without an error',
  withWorkspace(async ({ dataDir, open }) => {
    writeFileSync(
      join(dataDir, STATE_FILE),
      `${JSON.stringify({ version: 1, project_id: randomUUID(), tab: 'clips' })}\n`,
      'utf8',
    );

    const { page } = await open();

    await expect(page.getByTestId('projects-empty')).toBeVisible();
    await expect(page.getByTestId('new-project')).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expect(page.getByTestId('app-error')).toHaveCount(0);
    // The unknown project is forgotten, so the next start does not look for it again.
    await expect.poll(() => existsSync(join(dataDir, STATE_FILE))).toBe(false);
  }),
);

test(
  'leaving the project for the project list is remembered: the next start opens the list',
  withWorkspace(async ({ open }) => {
    const first = await open();
    await createProjectWithSource(first.page);
    await first.page
      .getByRole('navigation', { name: 'Навігаційний ланцюжок' })
      .getByRole('button', { name: 'Проєкти' })
      .click();
    await expect(first.page.getByTestId('project-item')).toHaveCount(1);
    await first.app.close();

    const second = await open();

    await expect(second.page.getByTestId('project-item')).toHaveCount(1);
    await expect(second.page.getByTestId('project-item')).toContainText(PROJECT_NAME);
    await expect(second.page.getByTestId('source-ready')).toHaveCount(0);
    await expect(second.page.getByRole('alert')).toHaveCount(0);
  }),
);
