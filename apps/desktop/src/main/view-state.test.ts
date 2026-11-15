// @vitest-environment node
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import type { IpcMainInvokeEvent } from 'electron';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { DEFAULT_LAYOUT, LAYOUT_LIMITS } from '../shared/bridge';
import type { ViewState } from '../shared/bridge';
import type { IpcRegistry } from './api-gateway';
import type { TrustedPage } from './ipc-guard';
import { VIEW_STATE_FILE, ViewStateStore, registerViewStateIpc } from './view-state';

const ID = '3f2b8c1e-5d4a-4b7e-9a10-2c6d8e0f1a2b';
const ID2 = '0a9d7e6c-1b2f-4c3d-8e4f-5a6b7c8d9e0f';
const TOKEN = 'ab'.repeat(32);
const PAGE = 'file:///C:/app/out/renderer/index.html';
const TRUSTED: TrustedPage = { webContentsId: 7, pageUrl: PAGE };
const FRAME = { url: PAGE };

const WIDE: ViewState['layout'] = { list_px: 280, details_px: 400 };
const SOURCES: ViewState = { project_id: ID, tab: 'sources', layout: DEFAULT_LAYOUT };
const CLIPS: ViewState = { project_id: ID2, tab: 'clips', layout: WIDE };

let dataDir = '';

beforeEach(() => {
  dataDir = mkdtempSync(join(tmpdir(), 'clipforge-view-state-'));
});

afterEach(() => {
  rmSync(dataDir, { recursive: true, force: true });
});

function stateFile(): string {
  return join(dataDir, VIEW_STATE_FILE);
}

function fileText(): string {
  return readFileSync(stateFile(), 'utf8');
}

describe('ViewStateStore', () => {
  it('reads nothing before anything was written', async () => {
    expect(await new ViewStateStore(dataDir).read()).toBeNull();
  });

  it('reads back what a previous run wrote, from a new store on the same folder', async () => {
    await new ViewStateStore(dataDir).write(CLIPS);

    expect(await new ViewStateStore(dataDir).read()).toEqual(CLIPS);
  });

  it('keeps the last write and replaces the file instead of appending', async () => {
    const store = new ViewStateStore(dataDir);

    await store.write(SOURCES);
    await store.write(CLIPS);

    expect(await store.read()).toEqual(CLIPS);
    expect(JSON.parse(fileText())).toEqual({ version: 2, ...CLIPS });
    expect(readdirSync(dataDir)).toEqual([VIEW_STATE_FILE]);
  });

  it('runs overlapping writes in order, so the last call wins', async () => {
    const store = new ViewStateStore(dataDir);

    const writes = [store.write(SOURCES), store.write(CLIPS), store.write(SOURCES), store.write(CLIPS)];
    await Promise.all(writes);

    expect(await store.read()).toEqual(CLIPS);
  });

  it('forgets the state when asked to write null, and forgetting twice is fine', async () => {
    const store = new ViewStateStore(dataDir);
    await store.write(SOURCES);

    await store.write(null);
    await store.write(null);

    expect(await store.read()).toBeNull();
    expect(readdirSync(dataDir)).toEqual([]);
  });

  it('creates the folder when the backend has not made it yet', async () => {
    const nested = join(dataDir, 'not', 'yet');

    await new ViewStateStore(nested).write(SOURCES);

    expect(await new ViewStateStore(nested).read()).toEqual(SOURCES);
  });

  it('writes only the version, the project id, the tab and the two widths (schema v2)', async () => {
    await new ViewStateStore(dataDir).write(SOURCES);

    const text = fileText();
    expect(Object.keys(JSON.parse(text) as object).sort()).toEqual(['layout', 'project_id', 'tab', 'version']);
    expect(text).toBe(
      `${JSON.stringify({
        version: 2,
        project_id: ID,
        tab: 'sources',
        layout: { list_px: 320, details_px: 320 },
      })}\n`,
    );
  });

  it('keeps the widths across a restart', async () => {
    await new ViewStateStore(dataDir).write(CLIPS);

    expect(await new ViewStateStore(dataDir).read()).toEqual({
      project_id: ID2,
      tab: 'clips',
      layout: { list_px: 280, details_px: 400 },
    });
  });

  it('never writes a width key that the layout object happens to carry besides its two', async () => {
    const polluted = {
      ...SOURCES,
      layout: { ...WIDE, path: 'C:\\Users\\me\\private.mp4' },
    } as ViewState;

    await new ViewStateStore(dataDir).write(polluted);

    expect(JSON.parse(fileText())).toEqual({ version: 2, ...SOURCES, layout: WIDE });
    expect(fileText()).not.toContain('private.mp4');
  });

  it('never writes a field that the state object happens to carry besides its two', async () => {
    const polluted = {
      ...SOURCES,
      path: 'C:\\Users\\me\\Videos\\private.mp4',
      token: TOKEN,
      output_dir: 'C:\\Exports',
    } as ViewState;

    await new ViewStateStore(dataDir).write(polluted);

    const text = fileText();
    expect(text).not.toContain('private.mp4');
    expect(text).not.toContain(TOKEN);
    expect(text).not.toContain('Exports');
    expect(text).not.toContain('C:');
    expect(text).not.toContain('\\\\');
    expect(JSON.parse(text)).toEqual({ version: 2, ...SOURCES });
  });

  describe('a v1 file (written before the panel widths existed)', () => {
    const V1 = { version: 1, project_id: ID2, tab: 'clips' };

    it('is still read, with the default widths', async () => {
      writeFileSync(stateFile(), `${JSON.stringify(V1)}\n`);

      expect(await new ViewStateStore(dataDir).read()).toEqual({
        project_id: ID2,
        tab: 'clips',
        layout: DEFAULT_LAYOUT,
      });
    });

    it('becomes a v2 file on the next write and keeps nothing of the old shape', async () => {
      writeFileSync(stateFile(), `${JSON.stringify(V1)}\n`);
      const store = new ViewStateStore(dataDir);

      const remembered = await store.read();
      if (remembered === null) {
        throw new Error('the v1 file was not read');
      }
      await store.write({ ...remembered, layout: WIDE });

      expect(JSON.parse(fileText())).toEqual({ version: 2, project_id: ID2, tab: 'clips', layout: WIDE });
    });

    it.each([
      ['widths (a v1 file never had them)', { ...V1, layout: DEFAULT_LAYOUT }],
      ['an extra key (a path)', { ...V1, path: 'C:\\x.mp4' }],
      ['a project id that is not a uuid', { ...V1, project_id: 'x' }],
      ['a tab that cannot be reopened', { ...V1, tab: 'export' }],
    ])('is ignored when it has %s', async (_name, document) => {
      writeFileSync(stateFile(), JSON.stringify(document));

      expect(await new ViewStateStore(dataDir).read()).toBeNull();
    });
  });

  it.each([
    ['not JSON', '{'],
    ['an empty file', ''],
    ['a JSON array', '[]'],
    ['null', 'null'],
    ['a number', '5'],
    ['a version from the future', JSON.stringify({ version: 3, ...SOURCES })],
    ['version 0', JSON.stringify({ version: 0, ...SOURCES })],
    ['a version as text', JSON.stringify({ version: '2', ...SOURCES })],
    ['v2 without widths', JSON.stringify({ version: 2, project_id: ID, tab: 'sources' })],
    ['no version', JSON.stringify(SOURCES)],
    ['a project id that is not a uuid', JSON.stringify({ version: 2, ...SOURCES, project_id: 'x' })],
    ['a tab that cannot be reopened', JSON.stringify({ version: 2, ...SOURCES, tab: 'export' })],
    ['an extra key (a path)', JSON.stringify({ version: 2, ...SOURCES, path: 'C:\\x.mp4' })],
    ['an extra key (a token)', JSON.stringify({ version: 2, ...SOURCES, token: TOKEN })],
    ['an extra key inside the widths', JSON.stringify({ version: 2, ...SOURCES, layout: { ...WIDE, path: 'C:\\x' } })],
    ['widths that are not an object', JSON.stringify({ version: 2, ...SOURCES, layout: 320 })],
    ['widths that are null', JSON.stringify({ version: 2, ...SOURCES, layout: null })],
    ['a missing details width', JSON.stringify({ version: 2, ...SOURCES, layout: { list_px: 320 } })],
    ['a missing list width', JSON.stringify({ version: 2, ...SOURCES, layout: { details_px: 320 } })],
    [
      'a list width under its limit',
      JSON.stringify({ version: 2, ...SOURCES, layout: { ...WIDE, list_px: LAYOUT_LIMITS.list_px.min - 1 } }),
    ],
    [
      'a list width over its limit',
      JSON.stringify({ version: 2, ...SOURCES, layout: { ...WIDE, list_px: LAYOUT_LIMITS.list_px.max + 1 } }),
    ],
    [
      'a details width under its limit',
      JSON.stringify({
        version: 2,
        ...SOURCES,
        layout: { ...WIDE, details_px: LAYOUT_LIMITS.details_px.min - 1 },
      }),
    ],
    [
      'a details width over its limit',
      JSON.stringify({
        version: 2,
        ...SOURCES,
        layout: { ...WIDE, details_px: LAYOUT_LIMITS.details_px.max + 1 },
      }),
    ],
    ['a negative width', JSON.stringify({ version: 2, ...SOURCES, layout: { ...WIDE, list_px: -320 } })],
    ['a zero width', JSON.stringify({ version: 2, ...SOURCES, layout: { ...WIDE, details_px: 0 } })],
    ['a fractional width', JSON.stringify({ version: 2, ...SOURCES, layout: { ...WIDE, list_px: 300.5 } })],
    ['a width as text', JSON.stringify({ version: 2, ...SOURCES, layout: { ...WIDE, list_px: '320' } })],
    ['a null width', JSON.stringify({ version: 2, ...SOURCES, layout: { ...WIDE, details_px: null } })],
    ['a huge width', JSON.stringify({ version: 2, ...SOURCES, layout: { ...WIDE, list_px: 1e21 } })],
  ])('treats a file with %s as nothing remembered', async (_name, content) => {
    writeFileSync(stateFile(), content);

    expect(await new ViewStateStore(dataDir).read()).toBeNull();
  });

  it('accepts widths exactly at both ends of their limits', async () => {
    const edges = [
      { list_px: LAYOUT_LIMITS.list_px.min, details_px: LAYOUT_LIMITS.details_px.min },
      { list_px: LAYOUT_LIMITS.list_px.max, details_px: LAYOUT_LIMITS.details_px.max },
    ];
    for (const layout of edges) {
      writeFileSync(stateFile(), JSON.stringify({ version: 2, ...SOURCES, layout }));

      expect(await new ViewStateStore(dataDir).read()).toEqual({ ...SOURCES, layout });
    }
  });

  it('treats a folder where the file should be as nothing remembered', async () => {
    mkdirSync(stateFile());

    expect(await new ViewStateStore(dataDir).read()).toBeNull();
  });

  it('is a no-op without a folder (test mode without CLIPFORGE_DATA_DIR)', async () => {
    const store = new ViewStateStore(null);

    await store.write(SOURCES);

    expect(await store.read()).toBeNull();
  });

  it('rejects a write that cannot reach the disk and still works afterwards', async () => {
    const blocked = join(dataDir, 'blocked');
    writeFileSync(blocked, 'a file, not a folder');
    const store = new ViewStateStore(blocked);

    await expect(store.write(SOURCES)).rejects.toThrow();
    await expect(store.write(CLIPS)).rejects.toThrow();
    expect(await store.read()).toBeNull();
  });
});

describe('registerViewStateIpc', () => {
  type Handler = (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown;

  function event(webContentsId: number): IpcMainInvokeEvent {
    return {
      sender: { id: webContentsId, mainFrame: FRAME },
      senderFrame: FRAME,
    } as unknown as IpcMainInvokeEvent;
  }

  function register(trusted: TrustedPage | null = TRUSTED): {
    readonly handlers: Map<string, Handler>;
    readonly store: ViewStateStore;
  } {
    const handlers = new Map<string, Handler>();
    const registry: IpcRegistry = {
      handle: (channel, listener) => {
        handlers.set(channel, listener);
      },
    };
    const store = new ViewStateStore(dataDir);
    registerViewStateIpc(registry, store, () => trusted);
    return { handlers, store };
  }

  function handler(handlers: Map<string, Handler>, channel: string): Handler {
    const found = handlers.get(channel);
    if (found === undefined) {
      throw new Error(`no handler for ${channel}`);
    }
    return found;
  }

  it('registers exactly the get and set channels', () => {
    expect([...register().handlers.keys()].sort()).toEqual([
      'app:get-view-state',
      'app:set-view-state',
    ]);
  });

  it('saves what the renderer sends and gives it back', async () => {
    const { handlers } = register();
    const get = handler(handlers, 'app:get-view-state');
    const set = handler(handlers, 'app:set-view-state');

    expect(await get(event(7))).toBeNull();
    expect(await set(event(7), CLIPS)).toEqual({ ok: true, data: { saved: true } });
    expect(await get(event(7))).toEqual(CLIPS);
    expect(JSON.parse(fileText())).toEqual({ version: 2, ...CLIPS });
  });

  it('forgets the state on null', async () => {
    const { handlers } = register();
    const get = handler(handlers, 'app:get-view-state');
    const set = handler(handlers, 'app:set-view-state');
    await set(event(7), SOURCES);

    expect(await set(event(7), null)).toEqual({ ok: true, data: { saved: true } });
    expect(await get(event(7))).toBeNull();
  });

  it.each([
    undefined,
    'sources',
    {},
    { project_id: ID },
    { project_id: ID, tab: 'sources' },
    { ...SOURCES, project_id: 'x' },
    { ...SOURCES, tab: 'export' },
    { ...SOURCES, path: 'C:\\Videos\\a.mp4' },
    { ...SOURCES, token: TOKEN },
    { ...SOURCES, output_dir: 'C:\\Exports' },
    { ...SOURCES, version: 2 },
    { ...SOURCES, layout: undefined },
    { ...SOURCES, layout: null },
    { ...SOURCES, layout: { list_px: 320 } },
    { ...SOURCES, layout: { ...DEFAULT_LAYOUT, path: 'C:\\x' } },
    { ...SOURCES, layout: { ...DEFAULT_LAYOUT, list_px: 239 } },
    { ...SOURCES, layout: { ...DEFAULT_LAYOUT, list_px: 481 } },
    { ...SOURCES, layout: { ...DEFAULT_LAYOUT, details_px: 279 } },
    { ...SOURCES, layout: { ...DEFAULT_LAYOUT, details_px: 481 } },
    { ...SOURCES, layout: { ...DEFAULT_LAYOUT, list_px: 320.5 } },
    { ...SOURCES, layout: { ...DEFAULT_LAYOUT, details_px: '320' } },
    { ...SOURCES, layout: { ...DEFAULT_LAYOUT, details_px: Number.NaN } },
    { ...SOURCES, layout: { ...DEFAULT_LAYOUT, list_px: Number.POSITIVE_INFINITY } },
  ])('refuses %j without touching the file', async (payload) => {
    const { handlers } = register();
    const set = handler(handlers, 'app:set-view-state');
    await set(event(7), SOURCES);
    const before = fileText();

    expect(await set(event(7), payload)).toMatchObject({
      ok: false,
      error: { code: 'INVALID_PARAMS' },
    });
    expect(fileText()).toBe(before);
  });

  it('refuses an array without touching the file', async () => {
    const { handlers } = register();
    const set = handler(handlers, 'app:set-view-state');

    expect(await set(event(7), [SOURCES])).toMatchObject({
      ok: false,
      error: { code: 'INVALID_PARAMS' },
    });
    expect(readdirSync(dataDir)).toEqual([]);
  });

  it('keeps paths and tokens out of the state file whatever the renderer sends', async () => {
    const { handlers } = register();
    const set = handler(handlers, 'app:set-view-state');

    await set(event(7), { ...SOURCES, path: 'C:\\Users\\me\\private.mp4', token: TOKEN });
    expect(readdirSync(dataDir)).toEqual([]);

    await set(event(7), SOURCES);
    const text = fileText();
    expect(text).not.toMatch(/[A-Za-z]:\\|\\\\|\.mp4|[0-9a-f]{64}/);
  });

  it('reports a write failure without any file system text', async () => {
    const blocked = join(dataDir, 'blocked');
    writeFileSync(blocked, 'a file, not a folder');
    const handlers = new Map<string, Handler>();
    registerViewStateIpc(
      {
        handle: (channel, listener) => {
          handlers.set(channel, listener);
        },
      },
      new ViewStateStore(blocked),
      () => TRUSTED,
    );

    const result = await handler(handlers, 'app:set-view-state')(event(7), SOURCES);

    expect(result).toEqual({
      ok: false,
      error: {
        code: 'VIEW_STATE_NOT_SAVED',
        message: "Не вдалося запам'ятати відкритий проєкт",
        retryable: false,
      },
    });
    expect(JSON.stringify(result)).not.toContain(dataDir);
  });

  it.each([
    ['another web contents', 99, TRUSTED],
    ['a moment without a trusted window', 7, null],
  ] as const)('refuses %s on both channels', async (_name, webContentsId, trusted) => {
    const { handlers } = register(trusted);
    const set = handler(handlers, 'app:set-view-state');
    const get = handler(handlers, 'app:get-view-state');

    await expect(Promise.resolve().then(() => set(event(webContentsId), SOURCES))).rejects.toThrow(
      'Недозволений відправник IPC',
    );
    await expect(Promise.resolve().then(() => get(event(webContentsId)))).rejects.toThrow(
      'Недозволений відправник IPC',
    );
    expect(readdirSync(dataDir)).toEqual([]);
  });

  it('refuses a sub frame', async () => {
    const { handlers } = register();
    const subFrame = {
      sender: { id: 7, mainFrame: FRAME },
      senderFrame: { url: PAGE },
    } as unknown as IpcMainInvokeEvent;

    await expect(
      Promise.resolve().then(() => handler(handlers, 'app:get-view-state')(subFrame)),
    ).rejects.toThrow('Недозволений відправник IPC');
  });
});
