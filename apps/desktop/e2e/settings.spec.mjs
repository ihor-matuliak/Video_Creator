/* global document */
import { randomBytes } from 'node:crypto';
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { _electron as electron, expect, test } from '@playwright/test';

// «Налаштування» → AI (docs/03 section 10, ADR 033/034). The OpenRouter key lives in main: it is
// encrypted with safeStorage in the workspace folder (or kept in memory when the system cannot),
// given to the sidecar on every start, and never shown back. No paid provider is called here.

const ENTRY = resolve(import.meta.dirname, '../out/main/index.js');
const KEY_FILE = 'openrouter-key.bin';
// A made-up key of the allowed shape (20-200 printable ASCII); it opens nothing.
const KEY = `sk-or-v1-${randomBytes(24).toString('hex')}`;

/** Starts the app on a workspace folder and an Electron profile folder of its own. */
async function launch(dataDir, userDataDir) {
  expect(existsSync(ENTRY), `build the app first: ${ENTRY}`).toBe(true);
  const env = { ...process.env, CLIPFORGE_E2E: '1', CLIPFORGE_DATA_DIR: dataDir };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.CLIPFORGE_MODE;

  const app = await electron.launch({ args: [ENTRY, `--user-data-dir=${userDataDir}`], env });
  const page = await app.firstWindow();
  await expect(page.getByTestId('app-ready')).toBeVisible();
  return { app, page };
}

/** Runs `body` with fresh folders and closes every app it launched, also on failure. */
function withWorkspace(body) {
  return async () => {
    const dataDir = mkdtempSync(join(tmpdir(), 'clipforge-settings-'));
    const userDataDir = mkdtempSync(join(tmpdir(), 'clipforge-profile-'));
    const apps = [];
    const open = async () => {
      const launched = await launch(dataDir, userDataDir);
      apps.push(launched.app);
      return launched;
    };
    try {
      await body({ dataDir, userDataDir, open });
    } finally {
      for (const app of apps) {
        await app.close().catch(() => undefined);
      }
      rmSync(dataDir, { recursive: true, force: true });
      rmSync(userDataDir, { recursive: true, force: true });
    }
  };
}

async function openSettings(page) {
  await page
    .getByRole('navigation', { name: 'Основна навігація' })
    .getByRole('button', { name: 'Налаштування' })
    .click();
  await expect(page.getByTestId('settings-ai')).toBeVisible();
}

/** The settings as the backend answers them, through the same route the renderer uses. */
async function readSettings(page) {
  const result = await page.evaluate(() => globalThis.clipforge.call('settings.get', {}));
  expect(result.ok, JSON.stringify(result)).toBe(true);
  return result.data;
}

function filesUnder(directory) {
  const files = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...filesUnder(path));
    } else if (entry.isFile()) {
      files.push(path);
    }
  }
  return files;
}

/** Files of a folder that hold `text` as bytes (UTF-8 or UTF-16); locked files are counted apart. */
function scanForText(directory, text) {
  const needles = [Buffer.from(text, 'utf8'), Buffer.from(text, 'utf16le')];
  const hits = [];
  let scanned = 0;
  for (const file of filesUnder(directory)) {
    let bytes;
    try {
      bytes = readFileSync(file);
    } catch {
      continue; // a file the browser keeps locked
    }
    scanned += 1;
    if (needles.some((needle) => bytes.includes(needle))) {
      hits.push(file);
    }
  }
  return { hits, scanned };
}

/** The page as the user could copy it: markup, the value of every field, and the web storage. */
async function pageHolds(page, text) {
  const dump = await page.evaluate(() =>
    [
      document.documentElement.outerHTML,
      ...[...document.querySelectorAll('input, textarea, select')].map((field) => field.value),
      JSON.stringify(localStorage),
      JSON.stringify(sessionStorage),
    ].join('\n'),
  );
  return dump.includes(text) || (await page.content()).includes(text);
}

async function saveKey(page, key = KEY) {
  await page.getByTestId('api-key').fill(key);
  await page.getByTestId('save-api-key').click();
}

test(
  'save the key: the sidecar has it, a relaunch restores it, forget removes it everywhere',
  withWorkspace(async ({ dataDir, open }) => {
    const first = await open();
    await openSettings(first.page);
    await expect(first.page.getByTestId('api-key-state')).toContainText('не задано');
    expect(await readSettings(first.page)).toMatchObject({ has_api_key: false });

    await saveKey(first.page);

    await expect(first.page.getByTestId('api-key-state')).toContainText('захищено');
    expect(await readSettings(first.page)).toMatchObject({ has_api_key: true });
    expect(existsSync(join(dataDir, KEY_FILE))).toBe(true);
    expect(statSync(join(dataDir, KEY_FILE)).size).toBeGreaterThan(0);
    await first.app.close();

    // A new process starts without the key in memory; main gives it to the sidecar again before
    // the app reports ready, so the first read already sees it.
    const second = await open();
    expect(await readSettings(second.page)).toMatchObject({ has_api_key: true });
    expect(await second.page.evaluate(() => globalThis.clipforge.getApiKeyStatus())).toEqual({
      state: 'protected',
      message: null,
    });
    await openSettings(second.page);
    await expect(second.page.getByTestId('api-key-state')).toContainText('захищено');
    await expect(second.page.getByTestId('api-key')).toHaveValue('');
    await expect(second.page.getByTestId('api-key-sync-note')).toHaveCount(0);

    await second.page.getByTestId('forget-api-key').click();

    await expect(second.page.getByTestId('api-key-state')).toContainText('не задано');
    await expect(second.page.getByTestId('forget-api-key')).toBeDisabled();
    expect(await readSettings(second.page)).toMatchObject({ has_api_key: false });
    expect(existsSync(join(dataDir, KEY_FILE))).toBe(false);
    await second.app.close();

    const third = await open();
    expect(await readSettings(third.page)).toMatchObject({ has_api_key: false });
    expect(await third.page.evaluate(() => globalThis.clipforge.getApiKeyStatus())).toEqual({
      state: 'none',
      message: null,
    });
  }),
);

test(
  'the key is in no file in plain form and not in the page after it was saved',
  withWorkspace(async ({ dataDir, userDataDir, open }) => {
    const { app, page } = await open();
    await openSettings(page);

    await saveKey(page);
    await expect(page.getByTestId('api-key-state')).toContainText('захищено');

    // The page: the field is empty again and the key is in no markup, field or web storage.
    await expect(page.getByTestId('api-key')).toHaveValue('');
    expect(await pageHolds(page, KEY)).toBe(false);
    // The bridge never gives it back: not in the status, not in the settings.
    const answers = await page.evaluate(async () => [
      await globalThis.clipforge.getApiKeyStatus(),
      await globalThis.clipforge.call('settings.get', {}),
    ]);
    expect(JSON.stringify(answers)).not.toContain(KEY);
    // After a change of screen too.
    await page
      .getByRole('navigation', { name: 'Основна навігація' })
      .getByRole('button', { name: 'Проєкти' })
      .click();
    expect(await pageHolds(page, KEY)).toBe(false);

    await app.close();

    // Every file of the workspace and of the Electron profile, including the database, the
    // state file and the key file itself.
    const workspace = scanForText(dataDir, KEY);
    expect(workspace.hits).toEqual([]);
    expect(workspace.scanned).toBeGreaterThan(1);
    expect(existsSync(join(dataDir, KEY_FILE))).toBe(true);
    expect(scanForText(userDataDir, KEY).hits).toEqual([]);
  }),
);

test(
  'api:call never carries the key: the setting is refused before the sidecar is asked',
  withWorkspace(async ({ open }) => {
    const { page } = await open();

    const results = await page.evaluate(async (key) => {
      const call = globalThis.clipforge.call;
      return [
        await call('settings.update', { openrouter_api_key: key }),
        await call('settings.update', { has_api_key: true }),
        await call('settings.update', { stt_model: null }),
      ];
    }, KEY);

    for (const result of results) {
      expect(result).toMatchObject({ ok: false, error: { code: 'INVALID_PARAMS' } });
    }
    expect(JSON.stringify(results)).not.toContain(KEY);
    expect(await readSettings(page)).toMatchObject({ has_api_key: false });
  }),
);

test(
  'a key of the wrong shape is refused with a fixed text and nothing is stored',
  withWorkspace(async ({ dataDir, open }) => {
    const { page } = await open();
    await openSettings(page);

    await saveKey(page, 'short key');

    await expect(page.getByTestId('api-key-error')).toContainText('від 20 до 200');
    await expect(page.getByTestId('api-key-state')).toContainText('не задано');
    expect(existsSync(join(dataDir, KEY_FILE))).toBe(false);
    expect(await readSettings(page)).toMatchObject({ has_api_key: false });
  }),
);

test(
  'without system protection the key lives only in this session: no file, gone after a relaunch',
  withWorkspace(async ({ dataDir, userDataDir, open }) => {
    const first = await open();
    // The system offers no encryption on this machine (replaced at call time, like the dialog).
    await first.app.evaluate(({ safeStorage }) => {
      safeStorage.isEncryptionAvailable = () => false;
    });
    await openSettings(first.page);

    await saveKey(first.page);

    await expect(first.page.getByTestId('api-key-state')).toContainText('лише на цей сеанс');
    expect(await readSettings(first.page)).toMatchObject({ has_api_key: true });
    expect(existsSync(join(dataDir, KEY_FILE))).toBe(false);
    expect(await pageHolds(first.page, KEY)).toBe(false);
    await first.app.close();
    expect(scanForText(dataDir, KEY).hits).toEqual([]);
    expect(scanForText(userDataDir, KEY).hits).toEqual([]);

    const second = await open();
    expect(await readSettings(second.page)).toMatchObject({ has_api_key: false });
    expect(await second.page.evaluate(() => globalThis.clipforge.getApiKeyStatus())).toEqual({
      state: 'none',
      message: null,
    });
  }),
);

test(
  'a stored key that cannot be decrypted is no key, with a fixed message, and can be replaced',
  withWorkspace(async ({ dataDir, open }) => {
    writeFileSync(join(dataDir, KEY_FILE), Buffer.from('this is not what safeStorage wrote'));

    const { page } = await open();

    expect(await readSettings(page)).toMatchObject({ has_api_key: false });
    await openSettings(page);
    await expect(page.getByTestId('api-key-state')).toContainText('не задано');
    await expect(page.getByTestId('api-key-message')).toContainText('не вдалося прочитати');
    await expect(page.getByTestId('app-error')).toHaveCount(0);

    await saveKey(page);

    await expect(page.getByTestId('api-key-state')).toContainText('захищено');
    await expect(page.getByTestId('api-key-message')).toHaveCount(0);
    expect(await readSettings(page)).toMatchObject({ has_api_key: true });
  }),
);

test(
  'the budget, the models and the language are saved and survive a relaunch',
  withWorkspace(async ({ open }) => {
    const first = await open();
    await openSettings(first.page);
    await expect(first.page.getByTestId('budget-limit')).toHaveValue('');
    await expect(first.page.getByTestId('stt-language')).toHaveValue('');
    await expect(first.page.getByTestId('price-note')).toContainText('Ціна: Невідомо');

    await first.page.getByTestId('budget-limit').fill('2.50');
    await first.page.getByTestId('stt-model').selectOption('google/gemini-3.5-transcribe');
    await first.page.getByTestId('stt-language').selectOption('es');
    await first.page.getByTestId('analysis-model').fill('google/gemini-3.5-flash');
    await first.page.getByTestId('save-settings').click();

    await expect(first.page.getByTestId('settings-saved')).toBeVisible();
    await expect(first.page.getByTestId('settings-error')).toHaveCount(0);
    const expected = {
      stt_model: 'google/gemini-3.5-transcribe',
      stt_language: 'es',
      analysis_model: 'google/gemini-3.5-flash',
      run_budget_usd: '2.50',
      has_api_key: false,
    };
    expect(await readSettings(first.page)).toEqual(expected);
    await first.app.close();

    const second = await open();
    expect(await readSettings(second.page)).toEqual(expected);
    await openSettings(second.page);
    await expect(second.page.getByTestId('budget-limit')).toHaveValue('2.50');
    await expect(second.page.getByTestId('stt-model')).toHaveValue('google/gemini-3.5-transcribe');
    await expect(second.page.getByTestId('stt-language')).toHaveValue('es');
    await expect(second.page.getByTestId('analysis-model')).toHaveValue('google/gemini-3.5-flash');

    // Emptying a field and choosing Автовизначення return the settings to their defaults.
    await second.page.getByTestId('budget-limit').fill('');
    await second.page.getByTestId('stt-language').selectOption('');
    await second.page.getByTestId('analysis-model').fill('');
    await second.page.getByTestId('save-settings').click();
    await expect(second.page.getByTestId('settings-saved')).toBeVisible();
    expect(await readSettings(second.page)).toEqual({
      stt_model: 'google/gemini-3.5-transcribe',
      stt_language: null,
      analysis_model: null,
      run_budget_usd: null,
      has_api_key: false,
    });
  }),
);

test(
  'an invalid budget shows a Ukrainian error, saves nothing and can be corrected',
  withWorkspace(async ({ open }) => {
    const { page } = await open();
    await openSettings(page);

    for (const bad of ['abc', '5000', '0', '1e2']) {
      await page.getByTestId('budget-limit').fill(bad);
      await page.getByTestId('save-settings').click();

      await expect(page.getByTestId('settings-error')).toContainText('Ліміт витрат має бути числом');
      await expect(page.getByTestId('budget-limit')).toHaveAttribute('aria-invalid', 'true');
      await expect(page.getByTestId('settings-saved')).toHaveCount(0);
      expect(await readSettings(page)).toMatchObject({ run_budget_usd: null });
    }

    // The same save with the other fields changed is all or nothing: the model did not change either.
    await page.getByTestId('stt-model').selectOption('google/gemini-3.5-transcribe');
    await page.getByTestId('budget-limit').fill('abc');
    await page.getByTestId('save-settings').click();
    await expect(page.getByTestId('settings-error')).toBeVisible();
    expect(await readSettings(page)).toMatchObject({ stt_model: 'microsoft/mai-transcribe-2' });

    await page.getByTestId('budget-limit').fill('1');
    await page.getByTestId('save-settings').click();

    await expect(page.getByTestId('settings-saved')).toBeVisible();
    await expect(page.getByTestId('settings-error')).toHaveCount(0);
    expect(await readSettings(page)).toMatchObject({
      stt_model: 'google/gemini-3.5-transcribe',
      run_budget_usd: '1',
    });
  }),
);

test(
  'an invalid analysis model is refused by its code in Ukrainian',
  withWorkspace(async ({ open }) => {
    const { page } = await open();
    await openSettings(page);

    await page.getByTestId('analysis-model').fill('no-slash-here');
    await page.getByTestId('save-settings').click();

    await expect(page.getByTestId('settings-error')).toContainText('«постачальник/модель»');
    await expect(page.getByTestId('analysis-model')).toHaveAttribute('aria-invalid', 'true');
    expect(await readSettings(page)).toMatchObject({ analysis_model: null });
  }),
);
