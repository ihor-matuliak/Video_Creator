import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { _electron as electron, expect, test } from '@playwright/test';

// docs/03 section 4 in a real window: the project list searches by name (Cyrillic, any case), shows
// the sources and clips of each project with the last activity in local time, and archives a project
// and brings it back through the «Ще дії» menu. The names, counts and the archive come from the real
// backend and database, so jsdom cannot stand in for this.

const ENTRY = resolve(import.meta.dirname, '../out/main/index.js');
const FIXTURE = resolve(import.meta.dirname, '../../../test-results/fixtures/color_gop.mp4');
const ACTIVITY = /^Остання дія: \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$/;

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
  await expect(page.getByTestId('project-search')).toBeVisible();
  return { app, page };
}

/**
 * Runs `body` with a fresh data folder; `open()` starts the app on it (again, for a restart on the
 * same data). Closes every app it started and removes the folder.
 */
function withData(body) {
  return async () => {
    const dataDir = mkdtempSync(join(tmpdir(), 'clipforge-projects-'));
    const apps = [];
    const open = async () => {
      const launched = await launch(dataDir);
      apps.push(launched.app);
      return launched;
    };
    try {
      await body({ open });
    } finally {
      for (const app of apps) {
        await app.close().catch(() => undefined);
      }
      // Windows lets go of the database file a moment after the sidecar has exited.
      try {
        rmSync(dataDir, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
      } catch {
        // A temp folder that stays behind is not a failure of the project list.
      }
    }
  };
}

/** Runs `body` with the page of an app on a fresh data folder. */
function withApp(body) {
  return withData(async ({ open }) => {
    const { page } = await open();
    await body(page);
  });
}

/** Creates a project from the list; it opens on «Джерела». */
async function createProject(page, name) {
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

function row(page, name) {
  return page.locator('li.project-row').filter({ hasText: name });
}

async function openProject(page, name) {
  await row(page, name).getByTestId('project-item').click();
  await expect(page.getByRole('heading', { level: 2, name })).toBeVisible();
}

async function importFixture(page) {
  await page.getByTestId('import-video').click();
  await expect(page.getByTestId('source-ready')).toBeVisible();
}

async function archive(page, name) {
  await row(page, name).getByTestId('project-menu').click();
  await page.getByTestId('archive-project').click();
  await page.getByTestId('archive-confirm').click();
}

/** The local time a «дд.мм.рррр гг:хх» label shows. */
function parseActivity(label) {
  const match = /(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})$/.exec(label);
  return new Date(Number(match[3]), Number(match[2]) - 1, Number(match[1]), Number(match[4]), Number(match[5]));
}

test(
  'search finds Cyrillic names whatever the case, says «Нічого не знайдено» and clears',
  withApp(async (page) => {
    for (const name of ['Кирилиця Перший', 'КИРИЛИЦЯ другий', 'Зовсім інший']) {
      await createProject(page, name);
      await backToList(page);
    }
    await expect(page.getByTestId('project-item')).toHaveCount(3);

    // Lower case finds a name that starts with a capital and a name written in capitals.
    await page.getByTestId('project-search').fill('кирилиця');
    await expect(page.getByTestId('project-item')).toHaveCount(2);
    await expect(row(page, 'Кирилиця Перший')).toHaveCount(1);
    await expect(row(page, 'КИРИЛИЦЯ другий')).toHaveCount(1);
    await expect(row(page, 'Зовсім інший')).toHaveCount(0);

    // Capitals find a name in lower case, and a part from the middle of a word finds it too.
    await page.getByTestId('project-search').fill('ПЕРШИ');
    await expect(page.getByTestId('project-item')).toHaveCount(1);
    await expect(page.getByTestId('project-item')).toContainText('Кирилиця Перший');

    await page.getByTestId('project-search').fill('немає такої назви');
    const nothing = page.getByTestId('projects-no-results');
    await expect(nothing).toBeVisible();
    await expect(nothing.getByRole('heading', { name: 'Нічого не знайдено' })).toBeVisible();
    await expect(page.getByTestId('project-item')).toHaveCount(0);
    await expect(page.getByTestId('projects-empty')).toHaveCount(0);

    await nothing.getByRole('button', { name: 'Очистити пошук' }).click();

    await expect(page.getByTestId('project-item')).toHaveCount(3);
    await expect(page.getByTestId('project-search')).toHaveValue('');
    await expect(page.getByTestId('project-search')).toBeFocused();
    await expect(page.getByTestId('projects-no-results')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
  }),
);

test(
  'counts and the last activity follow an import and a clip',
  withApp(async (page) => {
    const name = 'Лічильники';
    await createProject(page, name);
    await backToList(page);

    await expect(row(page, name).getByTestId('project-counts')).toHaveText('0 джерел · 0 кліпів');
    await expect(row(page, name).getByTestId('project-last-activity')).toHaveText(ACTIVITY);
    await expect(row(page, name).getByTestId('project-item')).toContainText(
      /0 джерел · 0 кліпів · Остання дія: \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}/,
    );

    await openProject(page, name);
    await importFixture(page);
    await backToList(page);

    await expect(row(page, name).getByTestId('project-counts')).toHaveText('1 джерело · 0 кліпів');

    await openProject(page, name);
    await page.getByTestId('manual-clip').click();
    await page.getByTestId('clip-title').fill('Мій фрагмент');
    await page.getByTestId('clip-start').fill('00:00:04.120');
    await page.getByTestId('clip-end').fill('00:00:07.880');
    await page.getByTestId('save-clip').click();
    await expect(page.getByTestId('clip-card')).toContainText('Мій фрагмент');
    await backToList(page);

    await expect(row(page, name).getByTestId('project-counts')).toHaveText('1 джерело · 1 кліп');
    const activity = row(page, name).getByTestId('project-last-activity');
    await expect(activity).toHaveText(ACTIVITY);
    // The label is the local time of the last change, which was made a moment ago.
    const age = Date.now() - parseActivity(await activity.innerText()).getTime();
    expect(age).toBeGreaterThanOrEqual(0);
    expect(age).toBeLessThan(10 * 60 * 1000);
  }),
);

test(
  'archive after a confirmation, show archived projects and bring one back',
  withApp(async (page) => {
    await createProject(page, 'Активний проєкт');
    await backToList(page);
    await createProject(page, 'Проєкт для архіву');
    await importFixture(page);
    await backToList(page);
    await expect(page.getByTestId('project-item')).toHaveCount(2);

    // «Скасувати» changes nothing.
    await row(page, 'Проєкт для архіву').getByTestId('project-menu').click();
    await expect(page.getByRole('menu', { name: 'Ще дії' })).toBeVisible();
    await page.getByTestId('archive-project').click();
    const confirm = page.getByRole('group', { name: 'Підтвердження архівування' });
    await expect(confirm).toContainText('Відео на диску не видаляються');
    await page.getByTestId('archive-cancel').click();
    await expect(confirm).toHaveCount(0);
    await expect(page.getByTestId('project-item')).toHaveCount(2);

    await archive(page, 'Проєкт для архіву');

    await expect(page.getByTestId('project-item')).toHaveCount(1);
    await expect(row(page, 'Проєкт для архіву')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
    // The video stays where it was.
    expect(existsSync(FIXTURE)).toBe(true);

    // The archive is in the database, not only on screen: «Показати архівні» asks the backend again.
    await page.getByTestId('show-archived').click();
    await expect(page.getByTestId('project-item')).toHaveCount(2);
    const archived = row(page, 'Проєкт для архіву');
    await expect(archived.getByTestId('project-archived')).toHaveText('В архіві');
    await expect(archived.getByTestId('project-counts')).toHaveText('1 джерело · 0 кліпів');
    await expect(row(page, 'Активний проєкт').getByTestId('project-archived')).toHaveCount(0);

    await archived.getByTestId('project-menu').click();
    await expect(page.getByTestId('archive-project')).toHaveCount(0);
    await expect(page.getByRole('menuitem')).toHaveText(['Перейменувати', 'Повернути з архіву']);
    await page.getByTestId('unarchive-project').click();

    await expect(page.getByTestId('project-archived')).toHaveCount(0);
    await expect(page.getByTestId('project-item')).toHaveCount(2);

    // Back in the ordinary list: both projects are active now.
    await page.getByTestId('show-archived').click();
    await expect(page.getByTestId('project-item')).toHaveCount(2);
    await expect(page.getByTestId('project-archived')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
  }),
);

test(
  'the «Ще дії» menu and the confirmation work from the keyboard',
  withApp(async (page) => {
    await createProject(page, 'Клавіатурний проєкт');
    await backToList(page);
    const trigger = row(page, 'Клавіатурний проєкт').getByTestId('project-menu');

    await trigger.focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('menu', { name: 'Ще дії' })).toBeVisible();
    await expect(page.getByTestId('rename-project')).toBeFocused();

    await page.keyboard.press('Escape');
    await expect(page.getByRole('menu')).toHaveCount(0);
    await expect(trigger).toBeFocused();

    await page.keyboard.press('ArrowDown');
    await expect(page.getByTestId('rename-project')).toBeFocused();
    await page.keyboard.press('ArrowDown');
    await expect(page.getByTestId('archive-project')).toBeFocused();
    await page.keyboard.press('Enter');

    await expect(page.getByTestId('archive-confirm-panel')).toBeVisible();
    await expect(page.getByTestId('archive-cancel')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('archive-confirm-panel')).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await expect(page.getByTestId('project-item')).toHaveCount(1);
  }),
);

test(
  'rename from the «Ще дії» menu with the keyboard: the new name is saved and is still there after a restart',
  withData(async ({ open }) => {
    const oldName = 'Стара назва';
    const newName = 'Нова назва проєкту';
    const first = await open();
    await createProject(first.page, oldName);
    await backToList(first.page);
    // The only project of the list: its row is found by the menu button, which outlives the new name.
    const trigger = first.page.getByTestId('project-menu');

    // Escape closes the field and changes nothing.
    await trigger.click();
    await first.page.getByTestId('rename-project').click();
    const input = first.page.getByTestId('rename-input');
    await expect(input).toBeFocused();
    await expect(input).toHaveValue(oldName);
    await input.fill('Не збережу');
    await first.page.keyboard.press('Escape');
    await expect(first.page.getByTestId('rename-form')).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await expect(first.page.getByTestId('project-item')).toContainText(oldName);

    // An empty name is refused in the row; the field stays open.
    await trigger.click();
    await first.page.getByTestId('rename-project').click();
    await input.fill('   ');
    await first.page.keyboard.press('Enter');
    await expect(first.page.getByTestId('rename-error')).toHaveText(/Введіть назву проєкту/);
    await expect(input).toBeVisible();

    // Enter saves; the list shows the new name and focus goes back to the menu button.
    await input.fill(newName);
    await first.page.keyboard.press('Enter');
    await expect(first.page.getByTestId('rename-form')).toHaveCount(0);
    await expect(first.page.getByTestId('project-item')).toContainText(newName);
    await expect(first.page.getByTestId('project-item')).not.toContainText(oldName);
    await expect(trigger).toBeFocused();
    await expect(first.page.getByRole('alert')).toHaveCount(0);
    await first.app.close();

    // The name is in the database, not only on screen.
    const second = await open();
    await expect(second.page.getByTestId('project-item')).toHaveCount(1);
    await expect(second.page.getByTestId('project-item')).toContainText(newName);
    await expect(second.page.getByTestId('project-item')).not.toContainText(oldName);
    await openProject(second.page, newName);
    await expect(second.page.getByRole('alert')).toHaveCount(0);
  }),
);
