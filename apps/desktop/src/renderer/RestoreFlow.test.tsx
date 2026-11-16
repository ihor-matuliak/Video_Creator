import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { SourceDto } from '../shared/api';
import { DEFAULT_LAYOUT } from '../shared/bridge';
import type { ViewState } from '../shared/bridge';
import { App } from './App';
import {
  CLIP_ID,
  PROJECT_ID,
  SOURCE_ID,
  failure,
  installBridge,
  listOf,
  makeClip,
  makeProject,
  makeProjectItem,
  makeSource,
  ok,
} from './test-bridge';
import type { ApiHandlers, BridgeOptions, InstalledBridge } from './test-bridge';

const OTHER_PROJECT_ID = '11111111-2222-4333-8444-555555555555';
const SECOND_SOURCE_ID = '9b2e5a7d-3c4f-4d6e-9f80-1b0c2d3e4f5a';
const THIRD_SOURCE_ID = '0a9d7e6c-1b2f-4c3d-8e4f-5a6b7c8d9e0f';

const FIRST = makeSource({ display_name: 'перша.mp4' });
const SECOND = makeSource({ id: SECOND_SOURCE_ID, display_name: 'друга.mp4' });
const THIRD = makeSource({ id: THIRD_SOURCE_ID, display_name: 'третя.mp4' });

function stubPlay() {
  return vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
}

let play: ReturnType<typeof stubPlay>;

beforeEach(() => {
  // jsdom does not play media: the player may ask for it, and a test can check that it did not.
  play = stubPlay();
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
});

/** The backend of a project whose sources and clips were registered in an earlier run. */
function earlierRun(sources: readonly SourceDto[] = [FIRST]): ApiHandlers {
  return {
    'projects.list': listOf(makeProject()),
    'sources.list': () => ok({ items: [...sources], next_cursor: null }),
    'clips.list': (params) =>
      ok({
        items: params.source_id === SOURCE_ID ? [makeClip()] : [],
        next_cursor: null,
      }),
  };
}

function start(
  handlers: ApiHandlers = earlierRun(),
  options: Omit<BridgeOptions, 'handlers'> = {},
): InstalledBridge {
  const installed = installBridge({ ...options, handlers });
  render(<App />);
  return installed;
}

async function openProject(): Promise<void> {
  fireEvent.click(await screen.findByTestId('project-item'));
}

function paramsOf(installed: InstalledBridge, op: string): unknown[] {
  return installed.call.mock.calls.filter(([called]) => called === op).map(([, params]) => params);
}

function remembered(installed: InstalledBridge): (ViewState | null)[] {
  return installed.setViewState.mock.calls.map(([state]) => state);
}

/** The «Проєкти» link of the breadcrumb in the project header (the sidebar has one too). */
function backToProjects(): HTMLElement {
  return within(screen.getByRole('navigation', { name: 'Навігаційний ланцюжок' })).getByRole('button', {
    name: 'Проєкти',
  });
}

const SOURCES_STATE: ViewState = { project_id: PROJECT_ID, tab: 'sources', layout: DEFAULT_LAYOUT };
const CLIPS_STATE: ViewState = { project_id: PROJECT_ID, tab: 'clips', layout: DEFAULT_LAYOUT };

describe('Project: the sources of earlier runs', () => {
  it('asks the backend for the sources of the opened project and shows the one that exists', async () => {
    const installed = start();
    await openProject();

    const ready = await screen.findByTestId('source-ready');

    expect(within(ready).getByRole('heading', { name: 'перша.mp4' })).toBeInTheDocument();
    expect(ready).toHaveTextContent('00:00:12.000');
    expect(paramsOf(installed, 'sources.list')).toEqual([{ project_id: PROJECT_ID }]);
    expect(installed.importVideo).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows a loading status, and no import button, until the list arrives', async () => {
    start({ ...earlierRun(), 'sources.list': () => new Promise(() => undefined) });
    await openProject();

    expect(await screen.findByTestId('sources-loading')).toHaveTextContent(
      'Завантажуємо джерела проєкту…',
    );
    expect(screen.getByRole('heading', { level: 2, name: 'Перевірка інтерфейсу' })).toBeVisible();
    expect(screen.queryByTestId('import-video')).not.toBeInTheDocument();
    expect(screen.queryByTestId('source-ready')).not.toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
  });

  it('offers the import of the first video for a project that has no sources yet', async () => {
    start(earlierRun([]));
    await openProject();

    expect(await screen.findByTestId('import-video')).toBeEnabled();
    expect(screen.queryByTestId('source-ready')).not.toBeInTheDocument();
    expect(screen.queryByTestId('sources-loading')).not.toBeInTheDocument();
  });

  it('follows the cursor and merges every page, in order', async () => {
    const installed = start({
      ...earlierRun(),
      'sources.list': (params) =>
        params.cursor === undefined
          ? ok({ items: [FIRST, SECOND], next_cursor: 'page-2' })
          : ok({ items: [THIRD], next_cursor: null }),
    });
    await openProject();

    const list = await screen.findByRole('list', { name: 'Джерела проєкту' });

    expect(within(list).getAllByRole('button').map((button) => button.textContent)).toEqual([
      'перша.mp4',
      'друга.mp4',
      'третя.mp4',
    ]);
    expect(paramsOf(installed, 'sources.list')).toEqual([
      { project_id: PROJECT_ID },
      { project_id: PROJECT_ID, cursor: 'page-2' },
    ]);
    // The last source is the one shown, like after an import. Level 3: the inspector has a heading of
    // its own and, below it, the one of the speech-recognition block.
    expect(within(screen.getByTestId('source-ready')).getByRole('heading', { level: 3 })).toHaveTextContent(
      'третя.mp4',
    );
  });

  it('keeps a source once when two pages both list it', async () => {
    start({
      ...earlierRun(),
      'sources.list': (params) =>
        params.cursor === undefined
          ? ok({ items: [FIRST, SECOND], next_cursor: 'page-2' })
          : ok({ items: [SECOND, THIRD], next_cursor: null }),
    });
    await openProject();

    const list = await screen.findByRole('list', { name: 'Джерела проєкту' });

    expect(within(list).getAllByRole('button').map((button) => button.textContent)).toEqual([
      'перша.mp4',
      'друга.mp4',
      'третя.mp4',
    ]);
  });

  it('shows the sources of every page on the Моменти tab too', async () => {
    start({
      ...earlierRun(),
      'sources.list': (params) =>
        params.cursor === undefined
          ? ok({ items: [FIRST, SECOND], next_cursor: 'page-2' })
          : ok({ items: [THIRD], next_cursor: null }),
    });
    await openProject();
    await screen.findByTestId('source-ready');

    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));

    const select = await screen.findByTestId('clips-source');
    expect(within(select).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'перша.mp4',
      'друга.mp4',
      'третя.mp4',
    ]);
  });

  it('lists the clips of a source that was registered in an earlier run', async () => {
    const installed = start();
    await openProject();
    await screen.findByTestId('source-ready');

    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));

    const card = await screen.findByTestId('clip-card');
    expect(card).toHaveAttribute('data-clip-id', CLIP_ID);
    expect(card).toHaveTextContent('Мій фрагмент');
    expect(paramsOf(installed, 'clips.list')).toEqual([{ source_id: SOURCE_ID }]);
    expect(screen.queryByTestId('clips-no-sources')).not.toBeInTheDocument();
  });

  it('adds a video imported now to the sources that were already there', async () => {
    const installed = start(earlierRun(), { importVideo: () => ok(SECOND) });
    await openProject();
    await screen.findByTestId('source-ready');

    fireEvent.click(screen.getByTestId('import-video'));

    const list = await screen.findByRole('list', { name: 'Джерела проєкту' });
    expect(within(list).getAllByRole('button').map((button) => button.textContent)).toEqual([
      'перша.mp4',
      'друга.mp4',
    ]);
    expect(installed.importVideo).toHaveBeenCalledExactlyOnceWith({ project_id: PROJECT_ID });
  });

  it('reads the list again for a project that is opened a second time', async () => {
    const installed = start();
    await openProject();
    await screen.findByTestId('source-ready');

    fireEvent.click(backToProjects());
    await openProject();
    await screen.findByTestId('source-ready');

    expect(paramsOf(installed, 'sources.list')).toHaveLength(2);
  });

  it('shows the Ukrainian error with a retry that starts the list again from the first page', async () => {
    let failing = true;
    const installed = start({
      ...earlierRun(),
      'sources.list': (params) => {
        if (params.cursor === undefined) {
          return ok({ items: [FIRST], next_cursor: 'page-2' });
        }
        return failing
          ? failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true)
          : ok({ items: [SECOND], next_cursor: null });
      },
    });
    await openProject();

    const alert = await screen.findByTestId('sources-error');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert).toHaveTextContent('Не вдалося завантажити джерела проєкту');
    expect(alert).toHaveTextContent('Локальний рушій не відповідає');
    // A half-read list is not shown as if it were the project's sources.
    expect(screen.queryByTestId('source-ready')).not.toBeInTheDocument();
    expect(screen.queryByTestId('import-video')).not.toBeInTheDocument();

    failing = false;
    fireEvent.click(screen.getByTestId('sources-retry'));

    const list = await screen.findByRole('list', { name: 'Джерела проєкту' });
    expect(within(list).getAllByRole('button')).toHaveLength(2);
    expect(screen.queryByTestId('sources-error')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'sources.list')).toEqual([
      { project_id: PROJECT_ID },
      { project_id: PROJECT_ID, cursor: 'page-2' },
      { project_id: PROJECT_ID },
      { project_id: PROJECT_ID, cursor: 'page-2' },
    ]);
  });

  it('shows a bridge failure of the list as the same error, not as an empty project', async () => {
    start({
      ...earlierRun(),
      'sources.list': () => {
        throw new Error('IPC refused');
      },
    });
    await openProject();

    expect(await screen.findByTestId('sources-error')).toBeVisible();
    expect(screen.queryByTestId('import-video')).not.toBeInTheDocument();
  });
});

describe('Restart: the last project and tab', () => {
  it('opens the remembered project on «Джерела» and shows its source again', async () => {
    const installed = start(earlierRun(), { viewState: SOURCES_STATE });

    const ready = await screen.findByTestId('source-ready');

    expect(within(ready).getByRole('heading', { name: 'перша.mp4' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'Перевірка інтерфейсу' })).toBeVisible();
    expect(screen.getByRole('tab', { name: 'Джерела' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByTestId('project-item')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'sources.list')).toEqual([{ project_id: PROJECT_ID }]);
  });

  it('opens the remembered tab «Моменти» with the clips of the source', async () => {
    const installed = start(earlierRun(), { viewState: CLIPS_STATE });

    expect(await screen.findByTestId('player')).toBeVisible();

    expect(screen.getByRole('tab', { name: 'Моменти' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Джерела' })).toHaveAttribute('aria-selected', 'false');
    expect(await screen.findByTestId('clip-card')).toHaveAttribute('data-clip-id', CLIP_ID);
    expect(paramsOf(installed, 'clips.list')).toEqual([{ source_id: SOURCE_ID }]);
  });

  it('does not start the video by itself (docs/03 section 12)', async () => {
    start(earlierRun(), { viewState: CLIPS_STATE });
    await screen.findByTestId('clip-card');

    expect(play).not.toHaveBeenCalled();
  });

  it('finds the project on a later page of the project list', async () => {
    const other = makeProjectItem({ id: OTHER_PROJECT_ID, name: 'Інший проєкт' });
    const installed = start(
      {
        ...earlierRun(),
        'projects.list': (params) =>
          params.cursor === undefined
            ? ok({ items: [other], next_cursor: 'next-1' })
            : ok({ items: [makeProjectItem()], next_cursor: null }),
      },
      { viewState: SOURCES_STATE },
    );

    await screen.findByTestId('source-ready');

    expect(paramsOf(installed, 'projects.list')).toEqual([{}, { cursor: 'next-1' }]);
    expect(screen.getByRole('heading', { level: 2, name: 'Перевірка інтерфейсу' })).toBeVisible();
  });

  it('shows the project list at once when nothing is remembered', async () => {
    const installed = start();

    expect(await screen.findByTestId('project-item')).toBeVisible();

    expect(installed.getViewState).toHaveBeenCalledTimes(1);
    expect(paramsOf(installed, 'sources.list')).toEqual([]);
    expect(paramsOf(installed, 'projects.list')).toEqual([{}]);
  });

  it('falls back to the project list, without an error, for a project that is not there any more', async () => {
    const other = makeProjectItem({ id: OTHER_PROJECT_ID, name: 'Інший проєкт' });
    const installed = start(
      { ...earlierRun(), 'projects.list': listOf(other) },
      { viewState: SOURCES_STATE },
    );

    const item = await screen.findByTestId('project-item');

    expect(item).toHaveTextContent('Інший проєкт');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByTestId('sources-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('projects-error')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'sources.list')).toEqual([]);
    // Forgotten, so the next start does not look for it again.
    expect(remembered(installed)).toEqual([null]);
    expect(await installed.bridge.getViewState()).toBeNull();
  });

  it('falls back to an empty project list the same way', async () => {
    start({ ...earlierRun(), 'projects.list': listOf() }, { viewState: CLIPS_STATE });

    expect(await screen.findByTestId('projects-empty')).toBeVisible();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps the memory and shows the list when the projects cannot be read for the lookup', async () => {
    let calls = 0;
    const installed = start(
      {
        ...earlierRun(),
        'projects.list': () => {
          calls += 1;
          return calls === 1
            ? failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true)
            : ok({ items: [makeProjectItem()], next_cursor: null });
        },
      },
      { viewState: SOURCES_STATE },
    );

    expect(await screen.findByTestId('project-item')).toBeVisible();

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(installed.setViewState).not.toHaveBeenCalled();
  });

  it('treats an unreadable memory as none', async () => {
    start(earlierRun(), { getViewState: () => Promise.reject(new Error('IPC refused')) });

    expect(await screen.findByTestId('project-item')).toBeVisible();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows a neutral status inside the app frame while the memory is read', async () => {
    start(earlierRun(), { getViewState: () => new Promise(() => undefined) });

    expect(await screen.findByTestId('app-ready')).toBeVisible();
    expect(screen.getByTestId('restoring')).toHaveTextContent('Завантажуємо…');
    expect(screen.queryByTestId('new-project')).not.toBeInTheDocument();
    expect(screen.queryByTestId('project-item')).not.toBeInTheDocument();
  });
});

describe('The project and tab on screen are remembered', () => {
  it('remembers a project when it is opened, on «Джерела»', async () => {
    const installed = start();

    await openProject();

    expect(remembered(installed)).toEqual([SOURCES_STATE]);
  });

  it('remembers a project that was just created', async () => {
    const installed = start({
      ...earlierRun(),
      'projects.list': listOf(),
      'projects.create': () => ok(makeProject()),
    });
    fireEvent.click(await screen.findByTestId('new-project'));
    fireEvent.change(screen.getByTestId('project-name'), { target: { value: 'Перевірка інтерфейсу' } });
    fireEvent.click(screen.getByTestId('create-project'));

    await screen.findByTestId('import-video');

    expect(remembered(installed)).toEqual([SOURCES_STATE]);
  });

  it('remembers each tab the user opens', async () => {
    const installed = start();
    await openProject();
    await screen.findByTestId('source-ready');

    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
    await screen.findByTestId('player');
    fireEvent.click(screen.getByRole('tab', { name: 'Джерела' }));
    await screen.findByTestId('source-ready');

    expect(remembered(installed)).toEqual([SOURCES_STATE, CLIPS_STATE, SOURCES_STATE]);
  });

  it('remembers «Моменти» when the manual clip form opens it', async () => {
    const installed = start();
    await openProject();
    await screen.findByTestId('source-ready');

    fireEvent.click(screen.getByTestId('manual-clip'));
    await screen.findByTestId('clip-title');

    expect(remembered(installed)).toEqual([SOURCES_STATE, CLIPS_STATE]);
  });

  it('forgets the project when the user returns to the project list', async () => {
    const installed = start();
    await openProject();
    await screen.findByTestId('source-ready');

    fireEvent.click(backToProjects());

    expect(await screen.findByTestId('project-item')).toBeVisible();
    expect(remembered(installed)).toEqual([SOURCES_STATE, null]);
  });

  it('forgets the project when the user leaves it through the sidebar', async () => {
    const installed = start();
    await openProject();
    await screen.findByTestId('source-ready');

    const nav = screen.getByRole('navigation', { name: 'Основна навігація' });
    fireEvent.click(within(nav).getByRole('button', { name: 'Проєкти' }));

    expect(await screen.findByTestId('project-item')).toBeVisible();
    expect(remembered(installed)).toEqual([SOURCES_STATE, null]);
  });

  it('saves nothing but a project id and a tab, whatever the user does', async () => {
    const installed = start(earlierRun(), { importVideo: () => ok(SECOND) });
    await openProject();
    await screen.findByTestId('source-ready');
    fireEvent.click(screen.getByTestId('import-video'));
    await screen.findByRole('list', { name: 'Джерела проєкту' });
    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
    await screen.findByTestId('player');

    const saved = remembered(installed);

    expect(saved.length).toBeGreaterThan(1);
    for (const state of saved) {
      expect(state === null || Object.keys(state).sort().join() === 'layout,project_id,tab').toBe(true);
    }
    const text = JSON.stringify(saved);
    expect(text).not.toMatch(/[A-Za-z]:\\|\.mp4|перша|друга|token/);
  });

  it('goes on working when the memory cannot be saved', async () => {
    const installed = start();
    installed.setViewState.mockRejectedValue(new Error('IPC refused'));

    await openProject();

    expect(await screen.findByTestId('source-ready')).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
    expect(await screen.findByTestId('player')).toBeVisible();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('comes back to what was left: a new start reads what the last one saved', async () => {
    const firstRun = installBridge({ handlers: earlierRun() });
    const firstView = render(<App />);
    await openProject();
    await screen.findByTestId('source-ready');
    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
    await screen.findByTestId('player');
    const left = await firstRun.bridge.getViewState();
    firstView.unmount();

    installBridge({ handlers: earlierRun(), viewState: left });
    render(<App />);

    expect(await screen.findByTestId('player')).toBeVisible();
    expect(screen.getByRole('tab', { name: 'Моменти' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('heading', { level: 2, name: 'Перевірка інтерфейсу' })).toBeVisible();
  });
});
