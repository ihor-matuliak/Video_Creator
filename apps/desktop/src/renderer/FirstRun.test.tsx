import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ApiData, ApiOp, SourceDto } from '../shared/api';
import { UNSUPPORTED_FILE_MESSAGE, apiFailure } from '../shared/bridge';
import type { ApiResult, ImportVideoParams } from '../shared/bridge';
import { App } from './App';
import {
  PROJECT_ID,
  VIDEO_TOKEN,
  failure,
  installBridge,
  listOf,
  makeChecks,
  makeProject,
  makeProjectItem,
  makeSource,
  ok,
} from './test-bridge';
import type { ApiHandlers, BridgeOptions, InstalledBridge } from './test-bridge';

// docs/03 section 3 and ADR 023/024: the screen of a user who has no project at all.

const NEVER: Promise<never> = new Promise<never>(() => undefined);
const FFMPEG_REQUIRED = 'Для обробки відео потрібен компонент FFmpeg';

type ChecksAnswer = ApiResult<ApiData['system.checks']>;

/** Opens the app with no project at all and waits for the first-run screen. */
async function openFirstRun(
  options: { readonly handlers?: ApiHandlers; readonly bridge?: Partial<BridgeOptions> } = {},
): Promise<InstalledBridge> {
  const installed = installBridge({
    ...options.bridge,
    handlers: {
      'projects.list': listOf(),
      'system.checks': () => ok(makeChecks()),
      ...options.handlers,
    },
  });
  render(<App />);
  await screen.findByTestId('first-run');
  return installed;
}

function callsOf(installed: InstalledBridge, op: ApiOp): unknown[] {
  return installed.call.mock.calls.filter(([called]) => called === op).map(([, params]) => params);
}

async function readyButton(): Promise<HTMLElement> {
  const button = screen.getByTestId('add-video');
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  return button;
}

describe('first run: the screen', () => {
  it('shows the heading, «Додати відео» as the one primary action and keeps «Новий проєкт»', async () => {
    const installed = await openFirstRun();

    expect(
      screen.getByRole('heading', { level: 3, name: 'Знайдімо цікаві моменти у твоєму відео' }),
    ).toBeVisible();
    const add = await readyButton();
    expect(add).toHaveTextContent('Додати відео');
    expect(add).toHaveClass('primary');
    expect(screen.getAllByTestId('new-project')).toHaveLength(1);
    expect(screen.getByTestId('new-project')).not.toHaveClass('primary');
    expect(screen.getByText(/API-ключ можна налаштувати пізніше/)).toBeVisible();
    // «Налаштувати AI» and the FFmpeg installer come with M3 and M8.
    expect(screen.queryByRole('button', { name: /Налаштувати AI/ })).not.toBeInTheDocument();
    // The first-run screen is for a user with no project at all, archived ones included.
    expect(callsOf(installed, 'projects.list')).toEqual([{}, { include_archived: true, limit: 1 }]);
    expect(screen.queryByTestId('projects-all-archived')).not.toBeInTheDocument();
  });

  it('shows the readiness of FFmpeg, FFprobe and the data folder', async () => {
    const installed = await openFirstRun();

    expect(await screen.findByTestId('system-check-ffmpeg')).toHaveTextContent('FFmpeg готовий · версія 7.1');
    expect(screen.getByTestId('system-check-ffprobe')).toHaveTextContent('FFprobe готовий · версія 7.1');
    expect(screen.getByTestId('system-check-data-dir')).toHaveTextContent(
      'Папка даних готова · вільно 12,0 ГБ',
    );
    expect(screen.queryByTestId('system-checks-loading')).not.toBeInTheDocument();
    expect(screen.queryByTestId('add-video-reason')).not.toBeInTheDocument();
    expect(callsOf(installed, 'system.checks')).toEqual([{}]);
  });

  it('keeps «Додати відео» off, with a visible reason, while the checks are running', async () => {
    await openFirstRun({ handlers: { 'system.checks': () => NEVER } });

    const add = screen.getByTestId('add-video');
    expect(add).toBeDisabled();
    expect(screen.getByTestId('system-checks-loading')).toHaveTextContent('Перевіряємо готовність…');
    expect(add).toHaveAccessibleDescription(expect.stringContaining('Перевіряємо готовність…'));
  });

  it('says that FFmpeg is needed and keeps «Додати відео» off when FFmpeg is missing', async () => {
    const installed = await openFirstRun({
      handlers: {
        'system.checks': () => ok(makeChecks({ ffmpeg: { ok: false, version: null } })),
      },
    });

    expect(await screen.findByTestId('system-check-ffmpeg')).toHaveTextContent('FFmpeg не знайдено');
    expect(screen.getByTestId('system-check-ffprobe')).toHaveTextContent('FFprobe готовий');
    const add = screen.getByTestId('add-video');
    expect(add).toBeDisabled();
    expect(screen.getByTestId('add-video-reason')).toBeVisible();
    expect(screen.getByTestId('add-video-reason')).toHaveTextContent(FFMPEG_REQUIRED);
    expect(add).toHaveAccessibleDescription(expect.stringContaining(FFMPEG_REQUIRED));

    fireEvent.click(add);

    expect(installed.pickVideo).not.toHaveBeenCalled();
  });

  it('does the same when only FFprobe is missing, as the video cannot be read without it', async () => {
    await openFirstRun({
      handlers: {
        'system.checks': () => ok(makeChecks({ ffprobe: { ok: false, version: null } })),
      },
    });

    expect(await screen.findByTestId('system-check-ffprobe')).toHaveTextContent('FFprobe не знайдено');
    expect(screen.getByTestId('add-video')).toBeDisabled();
    expect(screen.getByTestId('add-video-reason')).toHaveTextContent(FFMPEG_REQUIRED);
  });

  it('reports a data folder that cannot be written without blocking the button', async () => {
    await openFirstRun({
      handlers: {
        'system.checks': () => ok(makeChecks({ data_dir: { ok: false, free_bytes: null } })),
      },
    });

    expect(await screen.findByTestId('system-check-data-dir')).toHaveTextContent(
      'Немає доступу до папки даних',
    );
    expect(screen.getByTestId('add-video')).toBeEnabled();
  });

  it('shows the free space in megabytes and leaves it out when unknown', async () => {
    await openFirstRun({
      handlers: {
        'system.checks': () => ok(makeChecks({ data_dir: { ok: true, free_bytes: 340 * 1024 ** 2 } })),
      },
    });
    expect(await screen.findByTestId('system-check-data-dir')).toHaveTextContent('вільно 340 МБ');
  });

  it('shows a failed check with a retry, and leaves «Додати відео» available', async () => {
    let answers = 0;
    const installed = await openFirstRun({
      handlers: {
        'system.checks': (): ChecksAnswer => {
          answers += 1;
          return answers === 1
            ? failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true)
            : ok(makeChecks());
        },
      },
    });

    const message = await screen.findByTestId('system-checks-error');
    expect(message).toHaveTextContent('Не вдалося перевірити готовність. Локальний рушій не відповідає');
    expect(screen.getByTestId('add-video')).toBeEnabled();
    expect(screen.queryByTestId('system-check-ffmpeg')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('system-checks-retry'));

    expect(await screen.findByTestId('system-check-ffmpeg')).toHaveTextContent('FFmpeg готовий');
    expect(screen.queryByTestId('system-checks-error')).not.toBeInTheDocument();
    expect(callsOf(installed, 'system.checks')).toHaveLength(2);
  });
});

describe('first run: when the screen appears', () => {
  it('is not shown for a user with projects', async () => {
    installBridge({
      handlers: { 'projects.list': listOf(makeProject()), 'system.checks': () => ok(makeChecks()) },
    });
    render(<App />);

    expect(await screen.findByTestId('project-item')).toBeVisible();
    expect(screen.queryByTestId('first-run')).not.toBeInTheDocument();
    expect(screen.queryByTestId('add-video')).not.toBeInTheDocument();
  });

  it('is replaced by «Усі проєкти в архіві» when every project is archived', async () => {
    const archived = makeProjectItem({ archived_at: '2026-11-15T08:00:00Z' });
    const installed = installBridge({
      handlers: {
        'projects.list': (params): ApiResult<ApiData['projects.list']> =>
          ok({ items: params.include_archived === true ? [archived] : [], next_cursor: null }),
        'system.checks': () => ok(makeChecks()),
      },
    });
    render(<App />);

    const state = await screen.findByTestId('projects-all-archived');
    expect(within(state).getByRole('heading', { level: 3, name: 'Усі проєкти в архіві' })).toBeVisible();
    expect(screen.queryByTestId('first-run')).not.toBeInTheDocument();
    expect(screen.queryByTestId('add-video')).not.toBeInTheDocument();
    expect(callsOf(installed, 'system.checks')).toEqual([]);
    // «Новий проєкт» is still the primary action here.
    expect(screen.getByTestId('new-project')).toHaveClass('primary');

    fireEvent.click(within(state).getByTestId('projects-show-archived'));

    expect(await screen.findByTestId('project-item')).toHaveTextContent('Перевірка інтерфейсу');
    expect(screen.getByTestId('project-archived')).toHaveTextContent('В архіві');
    expect(screen.getByTestId('show-archived')).toBeChecked();
    expect(screen.queryByTestId('projects-all-archived')).not.toBeInTheDocument();
    expect(callsOf(installed, 'projects.list')).toContainEqual({ include_archived: true });
  });

  it('shows the load error when the archive cannot be asked about', async () => {
    installBridge({
      handlers: {
        'projects.list': (params): ApiResult<ApiData['projects.list']> =>
          params.include_archived === true
            ? failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true)
            : ok({ items: [], next_cursor: null }),
        'system.checks': () => ok(makeChecks()),
      },
    });
    render(<App />);

    expect(await screen.findByTestId('projects-error')).toHaveTextContent('Локальний рушій не відповідає');
    expect(screen.queryByTestId('first-run')).not.toBeInTheDocument();
    expect(screen.queryByTestId('projects-all-archived')).not.toBeInTheDocument();
  });

  it('does not ask about the archive when the user is searching', async () => {
    const installed = installBridge({
      handlers: {
        'projects.list': (params): ApiResult<ApiData['projects.list']> =>
          ok({
            items: params.q === undefined ? [makeProjectItem()] : [],
            next_cursor: null,
          }),
      },
    });
    render(<App />);
    await screen.findByTestId('project-item');
    installed.call.mockClear();

    fireEvent.change(screen.getByTestId('project-search'), { target: { value: 'немає такого' } });

    expect(await screen.findByTestId('projects-no-results')).toBeVisible();
    expect(screen.queryByTestId('first-run')).not.toBeInTheDocument();
    expect(callsOf(installed, 'projects.list')).toEqual([{ q: 'немає такого' }]);
  });
});

describe('first run: «Додати відео»', () => {
  function registering(): {
    readonly importVideo: NonNullable<BridgeOptions['importVideo']>;
    readonly handlers: ApiHandlers;
    readonly imported: ImportVideoParams[];
  } {
    const imported: ImportVideoParams[] = [];
    const sources: SourceDto[] = [];
    return {
      imported,
      importVideo: (params) => {
        imported.push(params);
        const source = makeSource({ display_name: 'Київ запис.mp4' });
        sources.push(source);
        return ok(source);
      },
      handlers: {
        'projects.create': ({ name }) => ok(makeProject({ name })),
        'sources.list': () => ok({ items: [...sources], next_cursor: null }),
      },
    };
  }

  it('picks the video, creates the project named after the file, registers it and opens «Джерела»', async () => {
    const flow = registering();
    const installed = await openFirstRun({
      handlers: flow.handlers,
      bridge: {
        pickVideo: () => ({ token: VIDEO_TOKEN, fileName: 'Київ запис.mp4' }),
        importVideo: flow.importVideo,
      },
    });

    fireEvent.click(await readyButton());

    expect(await screen.findByRole('heading', { level: 2, name: 'Київ запис' })).toBeVisible();
    expect(await screen.findByTestId('source-ready')).toHaveTextContent('Київ запис.mp4');
    expect(screen.getByRole('tab', { name: 'Джерела', selected: true })).toBeVisible();
    expect(callsOf(installed, 'projects.create')).toEqual([{ name: 'Київ запис' }]);
    // The path never reaches the renderer: the video is registered by its token.
    expect(flow.imported).toEqual([{ project_id: PROJECT_ID, file_token: VIDEO_TOKEN }]);
    expect(installed.pickVideo).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('first-run')).not.toBeInTheDocument();
    expect(screen.queryByTestId('import-error')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows what is under way while the steps run, and blocks a second click', async () => {
    let finish: (project: ApiResult<ApiData['projects.create']>) => void = () => undefined;
    const installed = await openFirstRun({
      handlers: {
        'projects.create': () =>
          new Promise<ApiResult<ApiData['projects.create']>>((resolve) => {
            finish = resolve;
          }),
      },
      bridge: { pickVideo: () => ({ token: VIDEO_TOKEN, fileName: 'a.mp4' }) },
    });

    fireEvent.click(await readyButton());

    expect(await screen.findByTestId('first-run-progress')).toHaveTextContent('Створюємо проєкт…');
    const add = screen.getByTestId('add-video');
    expect(add).toBeDisabled();
    expect(add).toHaveAccessibleDescription(expect.stringContaining('Створюємо проєкт…'));
    fireEvent.click(add);
    expect(installed.pickVideo).toHaveBeenCalledTimes(1);

    finish(failure('SIDECAR_UNAVAILABLE', 'Рушій не відповідає'));
    expect(await screen.findByTestId('first-run-error')).toBeVisible();
  });

  it('does nothing when the picker is cancelled, and the button can be used again', async () => {
    const installed = await openFirstRun({ bridge: { pickVideo: () => ({ canceled: true }) } });

    fireEvent.click(await readyButton());

    await waitFor(() => {
      expect(installed.pickVideo).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(screen.getByTestId('add-video')).toBeEnabled();
    });
    expect(screen.queryByTestId('first-run-progress')).not.toBeInTheDocument();
    expect(screen.queryByTestId('first-run-error')).not.toBeInTheDocument();
    expect(callsOf(installed, 'projects.create')).toEqual([]);
    expect(installed.importVideo).not.toHaveBeenCalled();
  });

  it('shows an error of the picker with its code under «Деталі»', async () => {
    const installed = await openFirstRun({
      bridge: {
        pickVideo: () => apiFailure('UNSUPPORTED_FILE', UNSUPPORTED_FILE_MESSAGE),
      },
    });

    fireEvent.click(await readyButton());

    const error = await screen.findByTestId('first-run-error');
    expect(error).toHaveAttribute('role', 'alert');
    expect(error).toHaveTextContent('Не вдалося вибрати відео');
    expect(error).toHaveTextContent('Це не відеофайл');
    expect(error).toHaveTextContent('Код помилки: UNSUPPORTED_FILE');
    expect(screen.getByTestId('add-video')).toBeEnabled();
    expect(callsOf(installed, 'projects.create')).toEqual([]);
  });

  it('turns a rejected picker call into a Ukrainian message instead of crashing', async () => {
    await openFirstRun({
      bridge: {
        pickVideo: () => {
          throw new Error('IPC refused');
        },
      },
    });

    fireEvent.click(await readyButton());

    expect(await screen.findByTestId('first-run-error')).toHaveTextContent(
      "Не вдалося зв'язатися з локальним рушієм",
    );
  });

  it('stays on the screen with the error when the project cannot be created, and can try again', async () => {
    const flow = registering();
    let attempts = 0;
    const installed = await openFirstRun({
      handlers: {
        ...flow.handlers,
        'projects.create': ({ name }) => {
          attempts += 1;
          return attempts === 1
            ? failure('INVALID_PROJECT_NAME', 'Назва проєкту недопустима')
            : ok(makeProject({ name }));
        },
      },
      bridge: {
        pickVideo: () => ({ token: VIDEO_TOKEN, fileName: 'Київ запис.mp4' }),
        importVideo: flow.importVideo,
      },
    });

    fireEvent.click(await readyButton());

    const error = await screen.findByTestId('first-run-error');
    expect(error).toHaveTextContent('Не вдалося створити проєкт');
    expect(error).toHaveTextContent('Назва проєкту недопустима');
    expect(screen.getByTestId('first-run')).toBeVisible();
    expect(installed.importVideo).not.toHaveBeenCalled();

    fireEvent.click(await readyButton());

    expect(await screen.findByRole('heading', { level: 2, name: 'Київ запис' })).toBeVisible();
    expect(await screen.findByTestId('source-ready')).toBeVisible();
    expect(callsOf(installed, 'projects.create')).toHaveLength(2);
  });

  it('opens the project with the error when the video cannot be registered, and «Додати відео» tries again', async () => {
    const imports: ImportVideoParams[] = [];
    const registered = makeSource({ display_name: 'Київ запис.mp4' });
    const sources: SourceDto[] = [];
    await openFirstRun({
      handlers: {
        'projects.create': ({ name }) => ok(makeProject({ name })),
        'sources.list': () => ok({ items: [...sources], next_cursor: null }),
      },
      bridge: {
        pickVideo: () => ({ token: VIDEO_TOKEN, fileName: 'Київ запис.mp4' }),
        importVideo: (params) => {
          imports.push(params);
          if (imports.length === 1) {
            return failure('PROBE_FAILED', 'Не вдалося прочитати відео');
          }
          sources.push(registered);
          return ok(registered);
        },
      },
    });

    fireEvent.click(await readyButton());

    // The project is there, on «Джерела», with the error and without a source.
    expect(await screen.findByRole('heading', { level: 2, name: 'Київ запис' })).toBeVisible();
    const error = await screen.findByTestId('import-error');
    expect(error).toHaveTextContent('Не вдалося додати відео');
    expect(error).toHaveTextContent('Не вдалося прочитати відео');
    expect(error).toHaveTextContent('Код помилки: PROBE_FAILED');
    expect(screen.getByRole('tab', { name: 'Джерела', selected: true })).toBeVisible();
    expect(screen.queryByTestId('source-ready')).not.toBeInTheDocument();
    expect(screen.queryByTestId('first-run')).not.toBeInTheDocument();
    expect(imports).toEqual([{ project_id: PROJECT_ID, file_token: VIDEO_TOKEN }]);

    // The usual button opens the picker again (the token is spent) and registers into this project.
    fireEvent.click(screen.getByTestId('import-video'));

    expect(await screen.findByTestId('source-ready')).toHaveTextContent('Київ запис.mp4');
    expect(imports[1]).toEqual({ project_id: PROJECT_ID });
    expect(screen.queryByTestId('import-error')).not.toBeInTheDocument();

    // The error of the first attempt does not come back when the tab is opened again.
    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
    await screen.findByRole('tabpanel', { name: 'Моменти' });
    fireEvent.click(screen.getByRole('tab', { name: 'Джерела' }));
    expect(await screen.findByTestId('source-ready')).toBeVisible();
    expect(screen.queryByTestId('import-error')).not.toBeInTheDocument();
  });

  it('opens the project with a note when the registration was cancelled', async () => {
    await openFirstRun({
      handlers: { 'projects.create': ({ name }) => ok(makeProject({ name })) },
      bridge: {
        pickVideo: () => ({ token: VIDEO_TOKEN, fileName: 'a.mp4' }),
        importVideo: () => ({ canceled: true }),
      },
    });

    fireEvent.click(await readyButton());

    expect(await screen.findByTestId('import-canceled')).toHaveTextContent('Імпорт скасовано');
    expect(screen.getByRole('heading', { level: 2, name: 'a' })).toBeVisible();
  });

  it('names a project «Новий проєкт» when the file name gives no stem', async () => {
    const flow = registering();
    const installed = await openFirstRun({
      handlers: flow.handlers,
      bridge: {
        pickVideo: () => ({ token: VIDEO_TOKEN, fileName: '   .mp4' }),
        importVideo: flow.importVideo,
      },
    });

    fireEvent.click(await readyButton());

    await screen.findByTestId('source-ready');
    expect(callsOf(installed, 'projects.create')).toEqual([{ name: 'Новий проєкт' }]);
  });
});
