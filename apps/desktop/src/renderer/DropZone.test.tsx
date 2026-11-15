import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ApiData, SourceDto } from '../shared/api';
import { FILE_NOT_FOUND_MESSAGE, UNSUPPORTED_FILE_MESSAGE, apiFailure } from '../shared/bridge';
import type { ApiResult, ImportVideoParams, ImportVideoResult, PickVideoResult } from '../shared/bridge';
import { App } from './App';
import {
  PROJECT_ID,
  VIDEO_TOKEN,
  failure,
  installBridge,
  listOf,
  makeChecks,
  makeProject,
  makeSource,
  ok,
} from './test-bridge';
import type { ApiHandlers, BridgeOptions, InstalledBridge } from './test-bridge';

// docs/03 section 5 and ADR 024: a video dropped on the zone of the first-run screen or of the
// «Джерела» tab goes the way of «Додати відео»; the path never reaches the renderer.

const NEVER: Promise<never> = new Promise<never>(() => undefined);
const ONE_VIDEO = 'Перетягніть один відеофайл';

function makeFile(name: string): File {
  return new File(['video bytes'], name, { type: 'video/mp4' });
}

/** A drop of `files` on `target`; false when the page cancelled it, as a stray drop must be. */
function dropFiles(target: Element, files: readonly File[]): boolean {
  return fireEvent.drop(target, { dataTransfer: { files, types: files.length === 0 ? [] : ['Files'] } });
}

function dragFiles(target: Element, type: 'dragEnter' | 'dragLeave' | 'dragOver'): boolean {
  return fireEvent[type](target, { dataTransfer: { files: [], types: ['Files'] } });
}

function callsOf(installed: InstalledBridge, op: string): unknown[] {
  return installed.call.mock.calls.filter(([called]) => called === op).map(([, params]) => params);
}

/** The answer of `takeDroppedVideo` for a file that main accepted. */
function accepted(fileName: string): PickVideoResult {
  return { token: VIDEO_TOKEN, fileName };
}

describe('drop zone: first-run screen', () => {
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
    await waitFor(() => {
      expect(screen.getByTestId('add-video')).toBeEnabled();
    });
    return installed;
  }

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

  it('creates the project named after the dropped file, registers it by token and opens «Джерела»', async () => {
    const flow = registering();
    const file = makeFile('Київ запис.mp4');
    const installed = await openFirstRun({
      handlers: flow.handlers,
      bridge: { takeDroppedVideo: () => accepted('Київ запис.mp4'), importVideo: flow.importVideo },
    });

    dropFiles(screen.getByTestId('drop-zone'), [file]);

    expect(await screen.findByRole('heading', { level: 2, name: 'Київ запис' })).toBeVisible();
    expect(await screen.findByTestId('source-ready')).toHaveTextContent('Київ запис.mp4');
    expect(screen.getByRole('tab', { name: 'Джерела', selected: true })).toBeVisible();
    expect(installed.takeDroppedVideo).toHaveBeenCalledExactlyOnceWith(file);
    expect(callsOf(installed, 'projects.create')).toEqual([{ name: 'Київ запис' }]);
    // The same scenario as the button, but the picker is not opened.
    expect(flow.imported).toEqual([{ project_id: PROJECT_ID, file_token: VIDEO_TOKEN }]);
    expect(installed.pickVideo).not.toHaveBeenCalled();
    expect(screen.queryByTestId('first-run')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('asks for one video when two files are dropped, and does not touch the bridge', async () => {
    const installed = await openFirstRun();

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('a.mp4'), makeFile('b.mp4')]);

    const error = await screen.findByTestId('drop-error');
    expect(error).toHaveAttribute('role', 'alert');
    expect(error).toHaveTextContent(ONE_VIDEO);
    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
    expect(callsOf(installed, 'projects.create')).toEqual([]);
    expect(installed.importVideo).not.toHaveBeenCalled();
    expect(screen.getByTestId('add-video')).toBeEnabled();
  });

  it('asks for one video when nothing that is a file is dropped (a text, a link)', async () => {
    const installed = await openFirstRun();

    dropFiles(screen.getByTestId('drop-zone'), []);

    expect(await screen.findByTestId('drop-error')).toHaveTextContent(ONE_VIDEO);
    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
  });

  it('shows the refusal of main without a path and creates no project', async () => {
    const installed = await openFirstRun({
      bridge: { takeDroppedVideo: () => apiFailure('UNSUPPORTED_FILE', UNSUPPORTED_FILE_MESSAGE) },
    });

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('notes.txt')]);

    const error = await screen.findByTestId('drop-error');
    expect(error.textContent).toBe(UNSUPPORTED_FILE_MESSAGE);
    expect(callsOf(installed, 'projects.create')).toEqual([]);
    expect(installed.importVideo).not.toHaveBeenCalled();
    expect(screen.getByTestId('first-run')).toBeVisible();
    expect(screen.getByTestId('add-video')).toBeEnabled();
  });

  it('shows «файл не знайдено» as it is, and a later good drop clears it', async () => {
    const flow = registering();
    let answers = 0;
    await openFirstRun({
      handlers: flow.handlers,
      bridge: {
        takeDroppedVideo: () => {
          answers += 1;
          return answers === 1 ? apiFailure('FILE_NOT_FOUND', FILE_NOT_FOUND_MESSAGE) : accepted('a.mp4');
        },
        importVideo: flow.importVideo,
      },
    });

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('a.mp4')]);
    expect(await screen.findByTestId('drop-error')).toHaveTextContent(FILE_NOT_FOUND_MESSAGE);

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('a.mp4')]);

    expect(await screen.findByTestId('source-ready')).toBeVisible();
    expect(screen.queryByTestId('drop-error')).not.toBeInTheDocument();
  });

  it('turns a rejected bridge call into a Ukrainian message instead of crashing', async () => {
    await openFirstRun({
      bridge: {
        takeDroppedVideo: () => {
          throw new Error('IPC refused: C:\\secret\\clip.mp4');
        },
      },
    });

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('clip.mp4')]);

    const error = await screen.findByTestId('drop-error');
    expect(error).toHaveTextContent("Не вдалося зв'язатися з локальним рушієм");
    expect(error).not.toHaveTextContent('secret');
  });

  it('clears an error of the button flow when a drop is refused, so that one alert is left', async () => {
    await openFirstRun({
      bridge: { pickVideo: () => apiFailure('UNSUPPORTED_FILE', UNSUPPORTED_FILE_MESSAGE) },
    });
    fireEvent.click(screen.getByTestId('add-video'));
    expect(await screen.findByTestId('first-run-error')).toBeVisible();

    dropFiles(screen.getByTestId('drop-zone'), []);

    expect(await screen.findByTestId('drop-error')).toBeVisible();
    expect(screen.queryByTestId('first-run-error')).not.toBeInTheDocument();
    expect(screen.getAllByRole('alert')).toHaveLength(1);
  });

  it('ignores a drop outside the zone: no bridge call, and the page cancels the event', async () => {
    const installed = await openFirstRun();
    const heading = screen.getByRole('heading', { level: 3, name: 'Знайдімо цікаві моменти у твоєму відео' });

    expect(dropFiles(heading, [makeFile('a.mp4')])).toBe(false);
    expect(dropFiles(document.body, [makeFile('a.mp4')])).toBe(false);
    // dragover is cancelled too, or the window would open the file.
    expect(dragFiles(document.body, 'dragOver')).toBe(false);

    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
    expect(installed.pickVideo).not.toHaveBeenCalled();
    expect(callsOf(installed, 'projects.create')).toEqual([]);
    expect(screen.queryByTestId('drop-error')).not.toBeInTheDocument();
    expect(screen.getByTestId('first-run')).toBeVisible();
  });

  it('shows a hover state with text while a file is over the zone, and not for a text drag', async () => {
    await openFirstRun();
    const zone = screen.getByTestId('drop-zone');
    expect(screen.queryByTestId('drop-zone-active')).not.toBeInTheDocument();
    expect(zone).toHaveAttribute('data-active', 'false');

    fireEvent.dragEnter(zone, { dataTransfer: { files: [], types: ['text/plain'] } });
    expect(screen.queryByTestId('drop-zone-active')).not.toBeInTheDocument();
    fireEvent.dragLeave(zone, { dataTransfer: { files: [], types: ['text/plain'] } });

    dragFiles(zone, 'dragEnter');
    expect(screen.getByTestId('drop-zone-active')).toHaveTextContent('Відпустіть, щоб додати відео');
    expect(zone).toHaveAttribute('data-active', 'true');

    dragFiles(zone, 'dragLeave');
    expect(screen.queryByTestId('drop-zone-active')).not.toBeInTheDocument();
    expect(zone).toHaveAttribute('data-active', 'false');
  });

  it('keeps the hover while the pointer crosses the children of the zone, and ends it after the drop', async () => {
    await openFirstRun();
    const zone = screen.getByTestId('drop-zone');
    const button = within(zone).getByTestId('add-video');

    dragFiles(zone, 'dragEnter');
    // The browser sends «enter» for the child before «leave» for the zone.
    dragFiles(button, 'dragEnter');
    dragFiles(zone, 'dragLeave');
    expect(screen.getByTestId('drop-zone-active')).toBeVisible();

    dragFiles(button, 'dragLeave');
    expect(screen.queryByTestId('drop-zone-active')).not.toBeInTheDocument();

    dragFiles(zone, 'dragEnter');
    expect(screen.getByTestId('drop-zone-active')).toBeVisible();
    dropFiles(zone, []);
    expect(screen.queryByTestId('drop-zone-active')).not.toBeInTheDocument();
  });

  it('is off while the project is being created: a drop does nothing, and the zone says so', async () => {
    const installed = await openFirstRun({
      handlers: { 'projects.create': () => NEVER },
      bridge: { pickVideo: () => accepted('a.mp4'), takeDroppedVideo: () => accepted('b.mp4') },
    });
    fireEvent.click(screen.getByTestId('add-video'));
    expect(await screen.findByTestId('first-run-progress')).toHaveTextContent('Створюємо проєкт…');
    const zone = screen.getByTestId('drop-zone');

    expect(dropFiles(zone, [makeFile('b.mp4')])).toBe(false);
    dragFiles(zone, 'dragEnter');

    expect(zone).toHaveAttribute('data-disabled', 'true');
    expect(zone).toHaveTextContent('Перетягування зараз недоступне');
    expect(screen.queryByTestId('drop-zone-active')).not.toBeInTheDocument();
    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
    expect(callsOf(installed, 'projects.create')).toHaveLength(1);
  });

  it('is off while the readiness check runs, as the button is', async () => {
    const installed = installBridge({
      handlers: { 'projects.list': listOf(), 'system.checks': () => NEVER },
      takeDroppedVideo: () => accepted('a.mp4'),
    });
    render(<App />);
    await screen.findByTestId('first-run');

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('a.mp4')]);

    expect(screen.getByTestId('add-video')).toBeDisabled();
    expect(screen.getByTestId('drop-zone')).toHaveAttribute('data-disabled', 'true');
    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
  });

  it('is off while FFmpeg is missing, as a video could not be registered', async () => {
    const installed = installBridge({
      handlers: {
        'projects.list': listOf(),
        'system.checks': () => ok(makeChecks({ ffmpeg: { ok: false, version: null } })),
      },
      takeDroppedVideo: () => accepted('a.mp4'),
    });
    render(<App />);
    await screen.findByTestId('add-video-reason');

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('a.mp4')]);

    expect(screen.getByTestId('drop-zone')).toHaveAttribute('data-disabled', 'true');
    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
    expect(callsOf(installed, 'projects.create')).toEqual([]);
  });

  it('opens the project with the error when the dropped video cannot be registered', async () => {
    const installed = await openFirstRun({
      handlers: { 'projects.create': ({ name }) => ok(makeProject({ name })) },
      bridge: {
        takeDroppedVideo: () => accepted('Київ запис.mp4'),
        importVideo: () => failure('PROBE_FAILED', 'Не вдалося прочитати відео'),
      },
    });

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('Київ запис.mp4')]);

    const error = await screen.findByTestId('import-error');
    expect(error).toHaveTextContent('Не вдалося прочитати відео');
    expect(screen.getByRole('heading', { level: 2, name: 'Київ запис' })).toBeVisible();
    expect(installed.importVideo).toHaveBeenCalledExactlyOnceWith({
      project_id: PROJECT_ID,
      file_token: VIDEO_TOKEN,
    });
  });
});

describe('drop zone: «Джерела» tab', () => {
  async function openSources(options: {
    readonly takeDroppedVideo?: BridgeOptions['takeDroppedVideo'];
    readonly importVideo?: BridgeOptions['importVideo'];
    readonly pickVideo?: BridgeOptions['pickVideo'];
  }): Promise<InstalledBridge> {
    const installed = installBridge({
      ...options,
      handlers: { 'projects.list': listOf(makeProject()) },
    });
    render(<App />);
    fireEvent.click(await screen.findByTestId('project-item'));
    await screen.findByTestId('drop-zone');
    return installed;
  }

  it('registers the dropped file in the current project by token and shows the source', async () => {
    const file = makeFile('color_gop.mp4');
    const installed = await openSources({
      takeDroppedVideo: () => accepted('color_gop.mp4'),
      importVideo: () => ok(makeSource()),
    });
    expect(screen.queryByTestId('source-ready')).not.toBeInTheDocument();

    dropFiles(screen.getByTestId('drop-zone'), [file]);

    expect(await screen.findByTestId('source-ready')).toHaveTextContent('color_gop.mp4');
    expect(installed.takeDroppedVideo).toHaveBeenCalledExactlyOnceWith(file);
    expect(installed.importVideo).toHaveBeenCalledExactlyOnceWith({
      project_id: PROJECT_ID,
      file_token: VIDEO_TOKEN,
    });
    expect(installed.pickVideo).not.toHaveBeenCalled();
    expect(callsOf(installed, 'projects.create')).toEqual([]);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('adds a second source by a second drop', async () => {
    const first = makeSource();
    const second = makeSource({ id: '9b2e5a7d-3c4f-4d6e-9f80-1b0c2d3e4f5a', display_name: 'two_tracks.mkv' });
    const sources = [first, second];
    await openSources({
      takeDroppedVideo: () => accepted('video'),
      importVideo: () => ok(sources.shift() ?? first),
    });

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('color_gop.mp4')]);
    await screen.findByTestId('source-ready');
    expect(screen.queryByRole('list', { name: 'Джерела проєкту' })).not.toBeInTheDocument();

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('two_tracks.mkv')]);

    const list = await screen.findByRole('list', { name: 'Джерела проєкту' });
    expect(within(list).getAllByRole('button')).toHaveLength(2);
    expect(screen.getByTestId('source-ready')).toHaveTextContent('two_tracks.mkv');
  });

  it('asks for one video when two files are dropped, and registers nothing', async () => {
    const installed = await openSources({ importVideo: () => ok(makeSource()) });

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('a.mp4'), makeFile('b.mp4')]);

    expect(await screen.findByTestId('drop-error')).toHaveTextContent(ONE_VIDEO);
    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
    expect(installed.importVideo).not.toHaveBeenCalled();
    expect(screen.queryByTestId('source-ready')).not.toBeInTheDocument();
  });

  it('asks for one video when no file is dropped', async () => {
    const installed = await openSources({});

    dropFiles(screen.getByTestId('drop-zone'), []);

    expect(await screen.findByTestId('drop-error')).toHaveTextContent(ONE_VIDEO);
    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
  });

  it('shows UNSUPPORTED_FILE of main as text, and the next good drop removes it', async () => {
    let answers = 0;
    const installed = await openSources({
      takeDroppedVideo: () => {
        answers += 1;
        return answers === 1 ? apiFailure('UNSUPPORTED_FILE', UNSUPPORTED_FILE_MESSAGE) : accepted('a.mp4');
      },
      importVideo: () => ok(makeSource()),
    });

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('notes.txt')]);

    const error = await screen.findByTestId('drop-error');
    expect(error.textContent).toBe(UNSUPPORTED_FILE_MESSAGE);
    expect(installed.importVideo).not.toHaveBeenCalled();
    expect(screen.getByTestId('import-video')).toBeEnabled();

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('a.mp4')]);

    expect(await screen.findByTestId('source-ready')).toBeVisible();
    expect(screen.queryByTestId('drop-error')).not.toBeInTheDocument();
  });

  it('turns a rejected bridge call into a Ukrainian message', async () => {
    await openSources({});

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('a.mp4')]);

    expect(await screen.findByTestId('drop-error')).toHaveTextContent("Не вдалося зв'язатися з локальним рушієм");
  });

  it('ignores a drop outside the zone, even over the other parts of the tab', async () => {
    const installed = await openSources({ takeDroppedVideo: () => accepted('a.mp4') });

    expect(dropFiles(screen.getByRole('heading', { level: 2 }), [makeFile('a.mp4')])).toBe(false);
    expect(dropFiles(screen.getByRole('tablist'), [makeFile('a.mp4')])).toBe(false);
    expect(dragFiles(screen.getByTestId('import-video').closest('.sources') ?? document.body, 'dragOver')).toBe(
      false,
    );

    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
    expect(installed.importVideo).not.toHaveBeenCalled();
    expect(screen.queryByTestId('drop-error')).not.toBeInTheDocument();
  });

  it('is off while an import is running: a drop does nothing, and the zone says so', async () => {
    const installed = await openSources({
      takeDroppedVideo: () => accepted('a.mp4'),
      importVideo: () => new Promise<ImportVideoResult>(() => undefined),
    });
    fireEvent.click(screen.getByTestId('import-video'));
    expect(await screen.findByTestId('import-loading')).toBeVisible();
    const zone = screen.getByTestId('drop-zone');

    dropFiles(zone, [makeFile('a.mp4')]);
    dragFiles(zone, 'dragEnter');

    expect(zone).toHaveAttribute('data-disabled', 'true');
    expect(zone).toHaveTextContent('Перетягування зараз недоступне');
    expect(screen.queryByTestId('drop-zone-active')).not.toBeInTheDocument();
    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
    expect(installed.importVideo).toHaveBeenCalledTimes(1);
  });

  it('shows the error of registering a dropped video with a retry that opens the picker', async () => {
    let attempts = 0;
    const installed = await openSources({
      takeDroppedVideo: () => accepted('a.mp4'),
      importVideo: (): ApiResult<SourceDto> => {
        attempts += 1;
        return attempts === 1 ? failure('PROBE_FAILED', 'Не вдалося прочитати відео') : ok(makeSource());
      },
    });

    dropFiles(screen.getByTestId('drop-zone'), [makeFile('a.mp4')]);

    const error = await screen.findByTestId('import-error');
    expect(error).toHaveTextContent('Не вдалося прочитати відео');

    fireEvent.click(screen.getByTestId('import-retry'));

    expect(await screen.findByTestId('source-ready')).toBeVisible();
    expect(installed.importVideo.mock.calls.map(([params]) => params)).toEqual([
      { project_id: PROJECT_ID, file_token: VIDEO_TOKEN },
      { project_id: PROJECT_ID },
    ]);
  });

  it('drops the error of an earlier import when a drop is refused, so that one alert is left', async () => {
    await openSources({ importVideo: () => failure('PROBE_FAILED', 'Не вдалося прочитати відео') });
    fireEvent.click(screen.getByTestId('import-video'));
    expect(await screen.findByTestId('import-error')).toBeVisible();

    dropFiles(screen.getByTestId('drop-zone'), []);

    expect(await screen.findByTestId('drop-error')).toBeVisible();
    expect(screen.queryByTestId('import-error')).not.toBeInTheDocument();
    expect(screen.getAllByRole('alert')).toHaveLength(1);
  });

  it('shows the hover state with text, and hides it when the drag leaves', async () => {
    await openSources({});
    const zone = screen.getByTestId('drop-zone');

    dragFiles(zone, 'dragEnter');
    expect(screen.getByTestId('drop-zone-active')).toHaveTextContent('Відпустіть, щоб додати відео');

    dragFiles(zone, 'dragLeave');
    expect(screen.queryByTestId('drop-zone-active')).not.toBeInTheDocument();
  });
});

describe('drop zone: other screens', () => {
  it('does not offer a zone in the project list and ignores a stray drop there', async () => {
    const installed = installBridge({
      handlers: { 'projects.list': listOf(makeProject()) },
      takeDroppedVideo: () => accepted('a.mp4'),
    });
    render(<App />);
    const item = await screen.findByTestId('project-item');

    expect(screen.queryByTestId('drop-zone')).not.toBeInTheDocument();
    expect(dropFiles(item, [makeFile('a.mp4')])).toBe(false);
    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
  });

  it('has no zone in the Моменти tab and ignores a stray drop there', async () => {
    const installed = installBridge({
      handlers: {
        'projects.list': listOf(makeProject()),
        'sources.list': (): ApiResult<ApiData['sources.list']> =>
          ok({ items: [makeSource()], next_cursor: null }),
        'clips.list': () => ok({ items: [], next_cursor: null }),
      },
      takeDroppedVideo: () => accepted('a.mp4'),
    });
    render(<App />);
    fireEvent.click(await screen.findByTestId('project-item'));
    await screen.findByTestId('source-ready');
    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
    const panel = await screen.findByRole('tabpanel', { name: 'Моменти' });

    expect(screen.queryByTestId('drop-zone')).not.toBeInTheDocument();
    expect(dropFiles(panel, [makeFile('a.mp4')])).toBe(false);
    expect(installed.takeDroppedVideo).not.toHaveBeenCalled();
  });
});
