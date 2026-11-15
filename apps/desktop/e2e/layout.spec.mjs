/* global window, document, getComputedStyle -- used inside page.evaluate callbacks, which run in the page */
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { _electron as electron, expect, test } from '@playwright/test';

// docs/03 sections 2, 8 and 12: the Моменти workspace in a real window. Three columns from 1440 px,
// tabs under the player below it, nothing cut off or scrolling sideways at the smallest window
// (1100x720) and at 125% and 150% display scaling, splitters that keep their width after a restart.
// jsdom does no layout, so the sizes can only be checked here.

const ENTRY = resolve(import.meta.dirname, '../out/main/index.js');
const FIXTURE = resolve(import.meta.dirname, '../../../test-results/fixtures/color_gop.mp4');
const SCREENS = resolve(import.meta.dirname, '../../../test-results/m2-screens');
const STATE_FILE = 'ui-state.json';
const PROJECT_NAME = 'Розкладка';
const SIZES = [
  { width: 1100, height: 720 },
  { width: 1440, height: 900 },
];
const SCALES = [1, 1.25, 1.5];

/**
 * Starts the app on `dataDir` at `scale` display scaling with a window whose page is exactly
 * `width` x `height` CSS px. The picker answers with `file` (the fixture unless told otherwise) or the
 * export folder.
 */
async function launch(dataDir, exportDir, { width = 1440, height = 900, scale = 1, file = FIXTURE } = {}) {
  expect(existsSync(ENTRY), `build the app first: ${ENTRY}`).toBe(true);
  expect(existsSync(FIXTURE), `generate the fixtures first (npm run fixtures): ${FIXTURE}`).toBe(true);
  const env = { ...process.env, CLIPFORGE_E2E: '1', CLIPFORGE_DATA_DIR: dataDir };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.CLIPFORGE_MODE;

  const app = await electron.launch({ args: [ENTRY, `--force-device-scale-factor=${String(scale)}`], env });
  await app.evaluate(
    ({ dialog }, { file, folder }) => {
      dialog.showOpenDialog = async (...args) => ({
        canceled: false,
        filePaths: [args.at(-1)?.properties?.includes('openDirectory') ? folder : file],
      });
    },
    { file, folder: exportDir },
  );
  const page = await app.firstWindow();
  await expect(page.getByTestId('app-ready')).toBeVisible();
  // The test means what it says: the page really is this size at this scaling. Chromium rounds a
  // window at a fractional scaling to whole device pixels, so a page may be a CSS pixel or two off.
  // The window exists now, but under load (a parallel pytest) a single setContentSize is
  // sometimes lost and the window stays 1280x798: ask again on every poll until the page has the size.
  await expect
    .poll(
      async () => {
        await app.evaluate(({ BrowserWindow }, size) => {
          for (const window of BrowserWindow.getAllWindows()) {
            window.setContentSize(size.width, size.height);
          }
        }, { width, height });
        const [innerWidth, innerHeight, ratio] = await page.evaluate(() => [
          window.innerWidth,
          window.innerHeight,
          window.devicePixelRatio,
        ]);
        return Math.abs(innerWidth - width) <= 2 && Math.abs(innerHeight - height) <= 3 && ratio === scale
          ? 'as asked'
          : `${String(innerWidth)}x${String(innerHeight)} at ${String(ratio)}x`;
      },
      { message: `the window did not take a ${String(width)}x${String(height)} page at ${String(scale)}x scaling` },
    )
    .toBe('as asked');
  return { app, page };
}

async function createProjectWithSource(page) {
  await page.getByTestId('new-project').click();
  await page.getByTestId('project-name').fill(PROJECT_NAME);
  await page.getByTestId('create-project').click();
  await page.getByTestId('import-video').click();
  await expect(page.getByTestId('source-ready')).toBeVisible();
}

async function saveManualClip(page) {
  await page.getByTestId('manual-clip').click();
  await page.getByTestId('clip-title').fill('Мій фрагмент');
  await page.getByTestId('clip-start').fill('00:00:04.120');
  await page.getByTestId('clip-end').fill('00:00:07.880');
  await page.getByTestId('save-clip').click();
  await expect(page.getByTestId('clip-card')).toContainText('Мій фрагмент');
}

/** Creates an empty project from the list; it opens on «Джерела». */
async function createEmptyProject(page, name) {
  await page.getByTestId('new-project').click();
  await page.getByTestId('project-name').fill(name);
  await page.getByTestId('create-project').click();
  await expect(page.getByRole('heading', { level: 2, name })).toBeVisible();
  await expect(page.getByTestId('import-video')).toBeVisible();
}

/** From an open project back to the project list (breadcrumb). */
async function backToList(page) {
  await page
    .getByRole('navigation', { name: 'Навігаційний ланцюжок' })
    .getByRole('button', { name: 'Проєкти' })
    .click();
  await expect(page.getByTestId('project-search')).toBeVisible();
}

/** The names the project list is audited with: the longest the backend takes, with and without spaces. */
const LONG_NAME = 'Довга назва проєкту для перевірки розкладки списку, яка переноситься на кілька рядків і все одно лишається в межах вікна';
const UNBROKEN_NAME = 'Назва_без_жодного_пробілу_'.repeat(5).slice(0, 120);

/** Runs `body` with a fresh data folder and export folder; closes every app it launched. */
function withWorkspace(body) {
  return async () => {
    const dataDir = mkdtempSync(join(tmpdir(), 'clipforge-layout-'));
    const exportDir = join(dataDir, 'exports');
    mkdirSync(exportDir);
    const apps = [];
    const open = async (options) => {
      const launched = await launch(dataDir, exportDir, options);
      apps.push(launched.app);
      return launched;
    };
    try {
      await body({ dataDir, open });
    } finally {
      for (const app of apps) {
        await app.close().catch(() => undefined);
      }
      // Windows lets go of the database file a moment after the sidecar has exited.
      try {
        rmSync(dataDir, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
      } catch {
        // A temp folder that stays behind is not a failure of the layout.
      }
    }
  };
}

/**
 * Runs in the page. Everything that is wrong with the layout, as sentences; [] when it is fine:
 * the page or the main area scrolling sideways, a control outside the window or cut off by a
 * panel, a button under 40 px, a button whose text does not fit, a field under 40 px.
 */
function auditLayout() {
  // A fractional display scaling puts an edge a device pixel off the whole CSS pixel of its box.
  const SLACK = 1.5;
  const problems = [];
  const root = document.documentElement;
  const width = root.clientWidth;
  const height = window.innerHeight;
  const name = (element) => {
    const id = element.getAttribute('data-testid');
    const text = (element.getAttribute('aria-label') ?? element.textContent ?? '').trim().slice(0, 40);
    return `<${element.tagName.toLowerCase()}${id === null ? '' : ` ${id}`}> "${text}"`;
  };

  if (root.scrollWidth > root.clientWidth) {
    problems.push(`the page scrolls sideways: ${String(root.scrollWidth)} > ${String(root.clientWidth)}`);
  }
  if (document.body.scrollWidth > document.body.clientWidth) {
    problems.push(`the body scrolls sideways: ${String(document.body.scrollWidth)} > ${String(document.body.clientWidth)}`);
  }
  for (const area of document.querySelectorAll('main, aside, section, [role="tabpanel"]')) {
    const style = getComputedStyle(area);
    if (style.overflowX !== 'visible' && area.scrollWidth > area.clientWidth) {
      problems.push(`${name(area)} scrolls sideways: ${String(area.scrollWidth)} > ${String(area.clientWidth)}`);
    }
  }

  const controls = document.querySelectorAll(
    'button, input, select, textarea, summary, [role="separator"], [tabindex="0"]',
  );
  for (const control of controls) {
    if (control.closest('[hidden]') !== null) {
      continue;
    }
    // A closed <details> renders its <summary> only; what is inside has no place on screen and cannot
    // be scrolled to. It is audited when the test opens the details.
    if (control.closest('details:not([open])') !== null && control.closest('summary') === null) {
      continue;
    }
    // A control that is hidden for sight (the collapsed sidebar labels are not controls) has no box.
    control.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    const rect = control.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0 || getComputedStyle(control).visibility === 'hidden') {
      continue;
    }
    if (rect.left < -SLACK || rect.right > width + SLACK) {
      problems.push(`${name(control)} is outside the window: ${rect.left.toFixed(1)}..${rect.right.toFixed(1)} of ${String(width)}`);
    }
    // A strip that is taller than the window (a splitter spans the whole workspace) is reached by scrolling.
    if (rect.height <= height && (rect.top < -SLACK || rect.bottom > height + SLACK)) {
      problems.push(`${name(control)} cannot be scrolled into the window: ${rect.top.toFixed(1)}..${rect.bottom.toFixed(1)} of ${String(height)}`);
    }
    for (let ancestor = control.parentElement; ancestor !== null; ancestor = ancestor.parentElement) {
      const style = getComputedStyle(ancestor);
      if (style.overflowX === 'visible' && style.overflowY === 'visible') {
        continue;
      }
      const box = ancestor.getBoundingClientRect();
      const left = box.left + ancestor.clientLeft;
      const top = box.top + ancestor.clientTop;
      const clipsX = style.overflowX !== 'visible';
      const clipsY = style.overflowY !== 'visible';
      if (
        (clipsX && (rect.left < left - SLACK || rect.right > left + ancestor.clientWidth + SLACK)) ||
        (clipsY &&
          rect.height <= ancestor.clientHeight &&
          (rect.top < top - SLACK || rect.bottom > top + ancestor.clientHeight + SLACK))
      ) {
        problems.push(
          `${name(control)} (x ${rect.left.toFixed(1)}..${rect.right.toFixed(1)}, y ${rect.top.toFixed(1)}..${rect.bottom.toFixed(1)}) is cut off by ${name(ancestor)} (x ${left.toFixed(1)}..${(left + ancestor.clientWidth).toFixed(1)}, y ${top.toFixed(1)}..${(top + ancestor.clientHeight).toFixed(1)})`,
        );
      }
    }
    if (control.tagName === 'BUTTON') {
      if (rect.height < 39.5) {
        problems.push(`${name(control)} is only ${rect.height.toFixed(1)} px high`);
      }
      if (control.scrollWidth > control.clientWidth + 1) {
        problems.push(`the text of ${name(control)} does not fit: ${String(control.scrollWidth)} > ${String(control.clientWidth)}`);
      }
    }
    if (control.classList.contains('field') && rect.height < 39.5) {
      problems.push(`${name(control)} is only ${rect.height.toFixed(1)} px high`);
    }
  }
  return problems;
}

/**
 * Runs in the page (docs/03 section 12). The sticky export bar must not hide any enabled button or
 * field of the Моменти panel: each one is scrolled into view the way a keyboard focus does it, and the
 * page is asked what is at its centre. It must be the control or something inside it, never the bar.
 * A strip taller than the room above the bar (a splitter spans the workspace) is judged by the centre
 * of the part that is on screen. Returns the problems as sentences; [] when nothing is covered.
 */
function auditExportBarCover() {
  const SLACK = 1.5;
  const panel = document.getElementById('panel-clips');
  const bar = document.querySelector('[data-testid="export-bar"]');
  const main = document.querySelector('main');
  if (panel === null || bar === null || main === null) {
    return ['the Моменти panel, its export bar or the main area is missing'];
  }
  const name = (element) => {
    const id = element.getAttribute('data-testid');
    const text = (element.getAttribute('aria-label') ?? element.textContent ?? '').trim().slice(0, 40);
    return `<${element.tagName.toLowerCase()}${id === null ? '' : ` ${id}`}> "${text}"`;
  };
  const problems = [];
  let checked = 0;
  const controls = panel.querySelectorAll(
    'button, input, select, textarea, summary, [role="separator"], [tabindex="0"]',
  );
  for (const control of controls) {
    if (bar.contains(control) || control.matches(':disabled') || control.closest('[hidden], [aria-disabled="true"]') !== null) {
      continue;
    }
    if (control.closest('details:not([open])') !== null && control.closest('summary') === null) {
      continue;
    }
    control.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    const rect = control.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0 || getComputedStyle(control).visibility === 'hidden') {
      continue;
    }
    // The part of the control that is inside the scrolling area.
    const area = main.getBoundingClientRect();
    const left = Math.max(rect.left, area.left);
    const right = Math.min(rect.right, area.right);
    const top = Math.max(rect.top, area.top);
    const bottom = Math.min(rect.bottom, area.bottom);
    if (right <= left || bottom <= top) {
      problems.push(`${name(control)} is not on screen after scrolling it into view`);
      continue;
    }
    const x = (left + right) / 2;
    const y = (top + bottom) / 2;
    const hit = document.elementFromPoint(x, y);
    checked += 1;
    const labelled = Array.from(control.labels ?? []).some((label) => hit !== null && label.contains(hit));
    if (hit !== null && (hit === control || control.contains(hit) || labelled)) {
      // The centre is the control's own. Its edge may still run under the bar (the bar is on top of it).
      const barBox = bar.getBoundingClientRect();
      const freeHeight = barBox.top - area.top;
      if (rect.height <= freeHeight && rect.bottom > barBox.top + SLACK && rect.top < barBox.bottom) {
        problems.push(
          `${name(control)} (y ${rect.top.toFixed(1)}..${rect.bottom.toFixed(1)}) runs under the export bar (from y ${barBox.top.toFixed(1)})`,
        );
      }
      continue;
    }
    problems.push(
      hit !== null && bar.contains(hit)
        ? `${name(control)} is covered by the export bar at its centre (${x.toFixed(1)}, ${y.toFixed(1)}): the page shows ${name(hit)}`
        : `${name(control)} has ${hit === null ? 'nothing' : name(hit)} at its centre (${x.toFixed(1)}, ${y.toFixed(1)})`,
    );
  }
  if (checked === 0) {
    problems.push('no control of the Моменти panel was checked: the selector found nothing');
  }
  return problems;
}

/** The sticky export bar covers no enabled control of the Моменти panel. */
async function checkNotCovered(page, label, tag) {
  const problems = await page.evaluate(auditExportBarCover);
  await page.evaluate(() => {
    for (const element of document.querySelectorAll('main')) {
      element.scrollTo(0, 0);
    }
  });
  expect(problems, `${tag} ${label}: the export bar covers a control`).toEqual([]);
}

/** Audits the page, then leaves a screenshot of it in test-results/m2-screens. */
async function checkScreen(page, label, tag) {
  mkdirSync(SCREENS, { recursive: true });
  const problems = await page.evaluate(auditLayout);
  await page.evaluate(() => {
    for (const element of document.querySelectorAll('main')) {
      element.scrollTo(0, 0);
    }
  });
  await page.screenshot({ path: join(SCREENS, `${tag}-${label}.png`) });
  expect(problems, `${tag} ${label}`).toEqual([]);
}

async function widthOf(locator) {
  return Math.round((await locator.boundingBox()).width);
}

for (const { width, height } of SIZES) {
  for (const scale of SCALES) {
    const tag = `${String(width)}x${String(height)}@${String(scale)}`;
    const wide = width >= 1440;

    test(
      `${tag}: nothing scrolls sideways or is cut off, buttons are 40 px high, panels follow docs/03 section 8`,
      withWorkspace(async ({ open }) => {
        const { page } = await open({ width, height, scale });

        await createProjectWithSource(page);
        await checkScreen(page, 'sources', tag);

        // Моменти with the empty new-clip form.
        await page.getByTestId('manual-clip').click();
        await expect(page.getByTestId('clip-title')).toBeVisible();
        const mode = await page.locator('.moments-grid').getAttribute('data-mode');
        expect(mode).toBe(wide ? 'wide' : 'narrow');
        await checkScreen(page, 'clips-new-clip', tag);
        await checkNotCovered(page, 'clips-new-clip', tag);

        if (wide) {
          // Three columns: list ~320, player at least 520, right panel ~320, and no tabs.
          await expect(page.getByRole('separator')).toHaveCount(2);
          await expect(page.getByRole('tablist', { name: 'Вкладки панелі фрагмента' })).toHaveCount(0);
          const list = await widthOf(page.getByRole('region', { name: 'Фрагменти' }));
          const player = await widthOf(page.getByRole('region', { name: 'Перегляд фрагмента' }));
          const details = await widthOf(page.getByTestId('details-panel'));
          expect(player, 'the player keeps its 520 px').toBeGreaterThanOrEqual(520);
          expect(list).toBeGreaterThanOrEqual(300);
          expect(list).toBeLessThanOrEqual(320);
          expect(details).toBeGreaterThanOrEqual(300);
          expect(details).toBeLessThanOrEqual(320);
          // The right panel is beside the player, not under it.
          const playerBox = await page.getByRole('region', { name: 'Перегляд фрагмента' }).boundingBox();
          const detailsBox = await page.getByTestId('details-panel').boundingBox();
          expect(detailsBox.x).toBeGreaterThanOrEqual(playerBox.x + playerBox.width - 1);
        } else {
          // Tabs under the player: one splitter, and the panel starts below the player.
          await expect(page.getByRole('separator')).toHaveCount(1);
          const tabs = page.getByRole('tablist', { name: 'Вкладки панелі фрагмента' });
          await expect(tabs.getByRole('tab')).toHaveText(['Межі', 'Клавіші']);
          const playerBox = await page.getByRole('region', { name: 'Перегляд фрагмента' }).boundingBox();
          const detailsBox = await page.getByTestId('details-panel').boundingBox();
          expect(detailsBox.y).toBeGreaterThanOrEqual(playerBox.y + playerBox.height - 1);
          expect(detailsBox.x).toBeGreaterThanOrEqual(playerBox.x - 1);
        }

        // The player keeps the proportions of the picture.
        const picture = await page.getByTestId('player-video').evaluate((video) => ({
          width: video.videoWidth,
          height: video.videoHeight,
        }));
        expect(picture.width).toBeGreaterThan(0);
        const stage = await page.getByTestId('player-stage').boundingBox();
        expect(stage.width / stage.height).toBeCloseTo(picture.width / picture.height, 1);

        // Save the clip, open it: the editor with its buttons.
        await page.getByTestId('clip-title').fill('Мій фрагмент');
        await page.getByTestId('clip-start').fill('00:00:04.120');
        await page.getByTestId('clip-end').fill('00:00:07.880');
        await page.getByTestId('save-clip').click();
        await expect(page.getByTestId('clip-card')).toContainText('Мій фрагмент');
        await page.getByTestId('open-clip').click();
        await expect(page.getByTestId('clip-editor')).toBeVisible();
        await checkScreen(page, 'clips-editor', tag);
        await checkNotCovered(page, 'clips-editor', tag);

        if (!wide) {
          await page.getByRole('tab', { name: 'Клавіші' }).click();
          await expect(page.getByTestId('hotkeys-hint')).toBeVisible();
          await checkScreen(page, 'clips-keys-tab', tag);
          await checkNotCovered(page, 'clips-keys-tab', tag);
          await page.getByRole('tab', { name: 'Межі' }).click();
        }

        // Approve, tick, and open the export summary: the bar with its parameters.
        await page.getByTestId('approve-clip').click();
        await expect(page.getByTestId('clip-review-status')).toContainText('Підтверджено');
        await page.getByTestId('select-clip').first().check();
        await expect(page.getByTestId('export-selected')).toBeEnabled();
        await checkNotCovered(page, 'export-bar-ready', tag);
        await page.getByTestId('export-selected').click();
        await expect(page.getByTestId('export-summary')).toBeVisible();
        await checkScreen(page, 'export-summary', tag);
        await checkNotCovered(page, 'export-summary', tag);
        // «Якість» opened: the select inside is a control too, and it must be reachable.
        await page.getByTestId('export-quality-details').locator('summary').click();
        await expect(page.getByTestId('export-quality')).toBeVisible();
        await checkScreen(page, 'export-summary-quality', tag);
        await checkNotCovered(page, 'export-summary-quality', tag);
      }),
    );

    test(
      `${tag}: the first-run screen, the project list (menu, archive confirmation, rename) and the import error fit the window`,
      withWorkspace(async ({ dataDir, open }) => {
        // A file with a video name and a text inside: the backend refuses it as unreadable.
        const inbox = join(dataDir, 'inbox');
        mkdirSync(inbox);
        const broken = join(inbox, 'Зламане відео.mp4');
        writeFileSync(broken, 'Це текст, а не відео.\n', 'utf8');
        const { page } = await open({ width, height, scale, file: broken });

        // First run: no project yet, the checks are done and «Додати відео» is on.
        await expect(page.getByTestId('first-run')).toBeVisible();
        await expect(page.getByTestId('system-check-data-dir')).toContainText('Папка даних готова');
        await expect(page.getByTestId('add-video')).toBeEnabled();
        await checkScreen(page, 'first-run', tag);

        // The first video cannot be read: the project opens on «Джерела» with the error block.
        await page.getByTestId('add-video').click();
        const importError = page.getByTestId('import-error');
        await expect(importError.getByRole('heading', { level: 3 })).toHaveText('Не вдалося прочитати відео');
        await expect(page.getByTestId('import-retry')).toHaveText('Вибрати інший файл');
        await checkScreen(page, 'import-error', tag);
        // «Деталі» opened: the code inside is content too, and a closed <details> is not audited.
        await importError.locator('summary').click();
        await expect(importError).toContainText('Код помилки: SOURCE_UNREADABLE');
        await checkScreen(page, 'import-error-details', tag);

        // Three projects, two of them with the longest names.
        await backToList(page);
        await createEmptyProject(page, LONG_NAME);
        await backToList(page);
        await createEmptyProject(page, UNBROKEN_NAME);
        await backToList(page);
        await expect(page.getByTestId('project-item')).toHaveCount(3);
        await checkScreen(page, 'projects-list', tag);

        // «Ще дії» open on the first row and on the last one.
        for (const [label, trigger] of [
          ['projects-menu-first', page.getByTestId('project-menu').first()],
          ['projects-menu-last', page.getByTestId('project-menu').last()],
        ]) {
          await trigger.click();
          await expect(page.getByRole('menu', { name: 'Ще дії' })).toBeVisible();
          await checkScreen(page, label, tag);
          await page.keyboard.press('Escape');
          await expect(page.getByRole('menu')).toHaveCount(0);
        }

        // The confirmation of the archive, for the project with the long name.
        const longRow = page.locator('li.project-row').filter({ hasText: LONG_NAME });
        await longRow.getByTestId('project-menu').click();
        await page.getByTestId('archive-project').click();
        await expect(page.getByTestId('archive-confirm-panel')).toBeVisible();
        await checkScreen(page, 'projects-archive-confirm', tag);
        await page.getByTestId('archive-cancel').click();
        await expect(page.getByTestId('archive-confirm-panel')).toHaveCount(0);

        // The name field of a row, with the long name in it, and then with the message of an empty name.
        await longRow.getByTestId('project-menu').click();
        await page.getByTestId('rename-project').click();
        await expect(page.getByTestId('rename-form')).toBeVisible();
        await checkScreen(page, 'projects-rename-form', tag);
        await page.getByTestId('rename-input').fill('   ');
        await page.keyboard.press('Enter');
        await expect(page.getByTestId('rename-error')).toBeVisible();
        await checkScreen(page, 'projects-rename-error', tag);
        await page.keyboard.press('Escape');
        await expect(page.getByTestId('rename-form')).toHaveCount(0);
      }),
    );
  }
}

test(
  'the smallest window shows a 1100x720 page, the size the layout is checked at',
  withWorkspace(async ({ open }) => {
    const { app, page } = await open({ width: 1440, height: 900, scale: 1 });

    // setSize, not setContentSize: on Windows setContentSize can undershoot the minimum height,
    // while a resize of the window frame (what a user's drag is) is held at the minimum.
    await app.evaluate(({ BrowserWindow }) => {
      for (const window of BrowserWindow.getAllWindows()) {
        window.setSize(300, 300);
      }
    });

    await expect
      .poll(() => page.evaluate(() => [window.innerWidth, window.innerHeight]))
      .toEqual([1100, 720]);
  }),
);

test(
  'a disabled primary button always says why (the new-clip form and the export bar)',
  withWorkspace(async ({ open }) => {
    const { page } = await open({ width: 1100, height: 720, scale: 1 });
    await createProjectWithSource(page);
    await page.getByTestId('manual-clip').click();

    const reasons = await page.evaluate(() =>
      [...document.querySelectorAll('button.primary:disabled')].map((button) => {
        const ids = (button.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean);
        const notes = ids
          .map((id) => document.getElementById(id))
          .filter((note) => note !== null && note.getBoundingClientRect().height > 0);
        return { id: button.getAttribute('data-testid'), reason: notes.map((note) => note.textContent.trim()).join(' ') };
      }),
    );

    expect(reasons.map((entry) => entry.id).sort()).toEqual(['export-selected', 'save-clip']);
    for (const entry of reasons) {
      expect(entry.reason, `${entry.id} has no visible reason`).not.toBe('');
    }
  }),
);

test(
  'a dragged splitter keeps its width after a restart, and the file is schema v2',
  withWorkspace(async ({ dataDir, open }) => {
    const first = await open({ width: 1440, height: 900, scale: 1 });
    await createProjectWithSource(first.page);
    await saveManualClip(first.page);
    await expect(first.page.locator('.moments-grid')).toHaveAttribute('data-mode', 'wide');

    // The list: two arrow presses narrower. The right panel: dragged wider with the mouse.
    const list = first.page.getByRole('separator', { name: 'Ширина списку фрагментів' });
    const details = first.page.getByRole('separator', { name: 'Ширина панелі фрагмента' });
    await list.focus();
    await list.press('ArrowLeft');
    await list.press('ArrowLeft');
    const box = await details.boundingBox();
    await first.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await first.page.mouse.down();
    await first.page.mouse.move(box.x + box.width / 2 - 40, box.y + box.height / 2, { steps: 8 });
    await first.page.mouse.up();

    const listWidth = Number(await list.getAttribute('aria-valuenow'));
    const detailsWidth = Number(await details.getAttribute('aria-valuenow'));
    expect(detailsWidth, 'the drag made the right panel wider').toBeGreaterThan(320);
    expect(listWidth, 'the arrows made the list narrower').toBeLessThan(320);
    // What is on screen is what the splitters say.
    expect(await widthOf(first.page.getByRole('region', { name: 'Фрагменти' }))).toBe(listWidth);
    expect(await widthOf(first.page.getByTestId('details-panel'))).toBe(detailsWidth);
    await first.app.close();

    const document = JSON.parse(readFileSync(join(dataDir, STATE_FILE), 'utf8'));
    expect(Object.keys(document).sort()).toEqual(['layout', 'project_id', 'tab', 'version']);
    expect(document.version).toBe(2);
    expect(document.tab).toBe('clips');
    expect(document.layout).toEqual({ list_px: listWidth, details_px: detailsWidth });

    const second = await open({ width: 1440, height: 900, scale: 1 });

    await expect(second.page.getByRole('tab', { name: 'Моменти' })).toHaveAttribute('aria-selected', 'true');
    const listAgain = second.page.getByRole('separator', { name: 'Ширина списку фрагментів' });
    const detailsAgain = second.page.getByRole('separator', { name: 'Ширина панелі фрагмента' });
    await expect(listAgain).toHaveAttribute('aria-valuenow', String(listWidth));
    await expect(detailsAgain).toHaveAttribute('aria-valuenow', String(detailsWidth));
    expect(await widthOf(second.page.getByRole('region', { name: 'Фрагменти' }))).toBe(listWidth);
    expect(await widthOf(second.page.getByTestId('details-panel'))).toBe(detailsWidth);
    // Nothing was written just because the app opened: the file is what the first run left.
    expect(JSON.parse(readFileSync(join(dataDir, STATE_FILE), 'utf8'))).toEqual(document);
  }),
);

test(
  'a v1 state file (no widths) still reopens the project, with the default widths',
  withWorkspace(async ({ dataDir, open }) => {
    const first = await open({ width: 1440, height: 900, scale: 1 });
    await createProjectWithSource(first.page);
    await first.page.getByRole('tab', { name: 'Моменти' }).click();
    await expect(first.page.getByTestId('player')).toBeVisible();
    await first.app.close();
    const { project_id: projectId } = JSON.parse(readFileSync(join(dataDir, STATE_FILE), 'utf8'));
    writeFileSync(
      join(dataDir, STATE_FILE),
      `${JSON.stringify({ version: 1, project_id: projectId, tab: 'clips' })}\n`,
      'utf8',
    );

    const second = await open({ width: 1440, height: 900, scale: 1 });

    await expect(second.page.getByRole('tab', { name: 'Моменти' })).toHaveAttribute('aria-selected', 'true');
    await expect(second.page.getByRole('heading', { level: 2, name: PROJECT_NAME })).toBeVisible();
    const list = second.page.getByRole('separator', { name: 'Ширина списку фрагментів' });
    // ~320 px: the grid may give a few px less when the window is only just 1440 px wide.
    expect(Number(await list.getAttribute('aria-valuenow'))).toBeGreaterThanOrEqual(300);
    expect(Number(await list.getAttribute('aria-valuenow'))).toBeLessThanOrEqual(320);
    await expect(second.page.getByRole('alert')).toHaveCount(0);

    // The first change writes v2.
    await list.focus();
    await list.press('ArrowLeft');
    await expect
      .poll(() => JSON.parse(readFileSync(join(dataDir, STATE_FILE), 'utf8')).version)
      .toBe(2);
    expect(JSON.parse(readFileSync(join(dataDir, STATE_FILE), 'utf8'))).toMatchObject({
      project_id: projectId,
      tab: 'clips',
    });
  }),
);
