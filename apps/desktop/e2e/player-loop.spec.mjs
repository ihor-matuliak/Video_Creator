import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { _electron as electron, expect, test } from '@playwright/test';

// docs/03 section 8, «Повтор фрагмента»: a fragment of the real fixture, played with the loop on,
// keeps playing inside [start, end) instead of pausing at its end. jsdom plays no media, so the
// loop can only be seen in a real window.

const ENTRY = resolve(import.meta.dirname, '../out/main/index.js');
const FIXTURE = resolve(import.meta.dirname, '../../../test-results/fixtures/color_gop.mp4');
const PROJECT_NAME = 'Повтор фрагмента';
// A 1 s fragment, played for three times that long.
const START_S = 4;
const END_S = 5;
const WATCH_MS = 3000;
// The end of a fragment is checked every few tens of ms, so the playhead may pass it by a little.
const OVERSHOOT_S = 0.1;

/** Starts the app on `dataDir`; the native picker answers with the fixture. */
async function launch(dataDir) {
  expect(existsSync(ENTRY), `build the app first: ${ENTRY}`).toBe(true);
  expect(existsSync(FIXTURE), `generate the fixtures first (npm run fixtures): ${FIXTURE}`).toBe(true);
  const env = { ...process.env, CLIPFORGE_E2E: '1', CLIPFORGE_DATA_DIR: dataDir };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.CLIPFORGE_MODE;

  const app = await electron.launch({ args: [ENTRY], env });
  await app.evaluate(
    ({ dialog }, picked) => {
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [picked] });
    },
    FIXTURE,
  );
  const page = await app.firstWindow();
  await expect(page.getByTestId('app-ready')).toBeVisible();
  return { app, page };
}

async function createProjectWithSource(page) {
  await page.getByTestId('new-project').click();
  await page.getByTestId('project-name').fill(PROJECT_NAME);
  await page.getByTestId('create-project').click();
  await page.getByTestId('import-video').click();
  await expect(page.getByTestId('source-ready')).toBeVisible();
}

test('a fragment played with «Повтор фрагмента» on is still playing after 3 s, inside its bounds', async () => {
  const dataDir = mkdtempSync(join(tmpdir(), 'clipforge-loop-'));
  let app = null;
  try {
    const launched = await launch(dataDir);
    app = launched.app;
    const { page } = launched;
    await createProjectWithSource(page);

    await page.getByRole('tab', { name: 'Моменти' }).click();
    await page.getByTestId('manual-clip').click();
    await page.getByTestId('clip-start').fill('00:00:04.000');
    await page.getByTestId('clip-end').fill('00:00:05.000');

    const loop = page.getByTestId('player-loop');
    await expect(loop).toHaveAttribute('aria-pressed', 'false');
    await loop.click();
    await expect(loop).toHaveAttribute('aria-pressed', 'true');

    const video = page.getByTestId('player-video');
    // «Відтворити фрагмент» is enabled once the video has loaded.
    await page.getByTestId('preview-clip').click();
    await expect
      .poll(() => video.evaluate((element) => !element.paused && element.currentTime >= 4))
      .toBe(true);

    // Watch the playhead for 3 s: the fragment is 1 s long, so it has to start over at least twice.
    const seen = await video.evaluate(
      (element, watchMs) =>
        new Promise((done) => {
          const samples = [];
          const began = performance.now();
          const timer = setInterval(() => {
            samples.push({ at: element.currentTime, paused: element.paused, ended: element.ended });
            if (performance.now() - began >= watchMs) {
              clearInterval(timer);
              done(samples);
            }
          }, 10);
        }),
      WATCH_MS,
    );

    expect(seen.length, 'the playhead was sampled').toBeGreaterThan(50);
    expect(seen.filter((sample) => sample.paused || sample.ended), 'it never paused or ended').toEqual([]);
    const outside = seen.filter((sample) => sample.at < START_S || sample.at > END_S + OVERSHOOT_S);
    expect(outside, `currentTime stays within [${String(START_S)}, ${String(END_S + OVERSHOOT_S)}]`).toEqual([]);
    const restarts = seen.filter((sample, index) => index > 0 && sample.at < seen[index - 1].at).length;
    expect(restarts, 'the fragment started over').toBeGreaterThanOrEqual(2);
    // Still playing 3 s after it began.
    expect(await video.evaluate((element) => !element.paused)).toBe(true);

    // Switched off, the pass that is under way stops at the end.
    await loop.click();
    await expect(loop).toHaveAttribute('aria-pressed', 'false');
    await expect.poll(() => video.evaluate((element) => element.paused)).toBe(true);
    const stoppedAt = await video.evaluate((element) => element.currentTime);
    expect(stoppedAt).toBeGreaterThanOrEqual(END_S - 0.001);
    expect(stoppedAt).toBeLessThanOrEqual(END_S + OVERSHOOT_S);
  } finally {
    await app?.close().catch(() => undefined);
    // Windows lets go of the database file a moment after the sidecar has exited.
    try {
      rmSync(dataDir, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
    } catch {
      // A temp folder that stays behind is not a failure of the loop.
    }
  }
});
