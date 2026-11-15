import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiParams, ClipDto, SourceDto } from '../shared/api';
import type { ApiResult } from '../shared/bridge';
import { App } from './App';
import {
  CLIP_ID,
  FOLDER_TOKEN,
  failure,
  installBridge,
  listOf,
  makeClip,
  makeProject,
  makeSource,
  ok,
} from './test-bridge';
import type { ApiHandlers, BridgeOptions, InstalledBridge } from './test-bridge';

const SECOND_ID = '6d8f0a2c-4e5a-4b7c-9d9e-0f1a2b3c4d5e';
const THIRD_ID = '7e9a1b3d-5f6a-4c8d-8eaf-1a2b3c4d5e6f';
const FOURTH_ID = '8fab2c4e-6a7b-4d9e-9fb0-2b3c4d5e6f70';
const JOB_ID = '2e4a6c8b-1d3f-4a5b-8c7d-9e0f1a2b3c4e';
const PREVIEW_ID = '7d9f2b4a-6c1e-4f3a-9b5d-1a2b3c4d5e6f';

const STALE_MESSAGE = 'Кліп змінено в іншому місці. Оновіть дані та повторіть';

/** The clip of makeClip(): «Мій фрагмент», 4120–7880. */
const FIRST = makeClip();
const SECOND = makeClip({ id: SECOND_ID, title: 'Другий', start_ms: 1000, end_ms: 3500 });

let media: {
  play: ReturnType<typeof stubPlay>;
  pause: ReturnType<typeof stubPause>;
};

function stubPlay() {
  return vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
}

function stubPause() {
  return vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
}

beforeEach(() => {
  // jsdom does not play media: these are the calls the player is expected to make.
  media = { play: stubPlay(), pause: stubPause() };
});

interface Backend {
  readonly clips: ClipDto[];
  readonly handlers: ApiHandlers;
  /** What the real backend does with a patch: the revision is checked, then the fields are applied. */
  readonly patch: (params: ApiParams['clips.patch']) => ApiResult<ClipDto>;
}

/** A fake backend for clips: list, patch and review act on one array. */
function backendOf(initial: readonly ClipDto[]): Backend {
  const clips = [...initial];
  const patch = (params: ApiParams['clips.patch']): ApiResult<ClipDto> => {
    const index = clips.findIndex((clip) => clip.id === params.id);
    const current = clips[index];
    if (current === undefined) {
      return failure('CLIP_NOT_FOUND', 'Кліп не знайдено');
    }
    if (current.revision !== params.expected_revision) {
      return failure('STALE_REVISION', STALE_MESSAGE);
    }
    const next: ClipDto = {
      ...current,
      revision: current.revision + 1,
      title: params.title ?? current.title,
      start_ms: params.start_ms ?? current.start_ms,
      end_ms: params.end_ms ?? current.end_ms,
    };
    clips[index] = next;
    return ok(next);
  };
  const handlers: ApiHandlers = {
    'clips.list': () => ok({ items: [...clips], next_cursor: null }),
    'clips.create': (params) => {
      const created = makeClip({
        id: `00000000-0000-4000-8000-${String(clips.length + 1).padStart(12, '0')}`,
        source_id: params.source_id,
        title: params.title,
        start_ms: params.start_ms,
        end_ms: params.end_ms,
      });
      clips.push(created);
      return ok(created);
    },
    'clips.patch': patch,
    'clips.review': (params) => {
      const index = clips.findIndex((clip) => clip.id === params.id);
      const current = clips[index];
      if (current === undefined) {
        return failure('CLIP_NOT_FOUND', 'Кліп не знайдено');
      }
      if (current.revision !== params.expected_revision) {
        return failure('STALE_REVISION', STALE_MESSAGE);
      }
      const next = { ...current, revision: current.revision + 1, review_status: params.status };
      clips[index] = next;
      return ok(next);
    },
  };
  return { clips, handlers, patch };
}

interface Opened {
  readonly installed: InstalledBridge;
  readonly backend: Backend;
}

/** The project with the source imported, then the Моменти tab, with the clips listed. */
async function openMoments(
  clips: readonly ClipDto[] = [FIRST],
  options: {
    readonly backend?: Backend;
    readonly handlers?: ApiHandlers;
    readonly source?: SourceDto;
    readonly bridge?: Pick<BridgeOptions, 'chooseExportFolder'>;
  } = {},
): Promise<Opened> {
  const backend = options.backend ?? backendOf(clips);
  const source = options.source ?? makeSource();
  const installed = installBridge({
    importVideo: () => ok(source),
    ...options.bridge,
    handlers: { 'projects.list': listOf(makeProject()), ...backend.handlers, ...options.handlers },
  });
  render(<App />);
  fireEvent.click(await screen.findByTestId('project-item'));
  fireEvent.click(await screen.findByTestId('import-video'));
  await screen.findByTestId('source-ready');
  fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
  await screen.findByTestId('player');
  if (clips.length > 0) {
    await screen.findAllByTestId('clip-card');
  }
  return { installed, backend };
}

function video(): HTMLVideoElement {
  const element = screen.getByTestId('player-video');
  if (!(element instanceof HTMLVideoElement)) {
    throw new TypeError('player-video is not a <video>');
  }
  return element;
}

function setPlayhead(element: HTMLVideoElement, seconds: number): void {
  Object.defineProperty(element, 'currentTime', { configurable: true, writable: true, value: seconds });
}

/** The video has its metadata: the player knows the position. */
function loadVideo(seconds = 0): HTMLVideoElement {
  const element = video();
  setPlayhead(element, seconds);
  fireEvent.loadedMetadata(element);
  return element;
}

function cardOf(clipId: string): HTMLElement {
  const found = screen
    .getAllByTestId('clip-card')
    .find((element) => element.getAttribute('data-clip-id') === clipId);
  if (found === undefined) {
    throw new Error(`no card for ${clipId}`);
  }
  return found;
}

/** Presses the title of the card: the keyboard way to make a clip the active one. */
function activate(clipId: string): void {
  fireEvent.click(within(cardOf(clipId)).getByTestId('open-clip'));
}

function activeCardIds(): (string | null)[] {
  return screen
    .getAllByTestId('clip-card')
    .filter((element) => element.getAttribute('aria-current') === 'true')
    .map((element) => element.getAttribute('data-clip-id'));
}

function paramsOf(installed: InstalledBridge, op: string): unknown[] {
  return installed.call.mock.calls.filter(([called]) => called === op).map(([, params]) => params);
}

const patchesOf = (installed: InstalledBridge): unknown[] => paramsOf(installed, 'clips.patch');
const reviewsOf = (installed: InstalledBridge): unknown[] => paramsOf(installed, 'clips.review');

function fill(testId: string, value: string): void {
  fireEvent.change(screen.getByTestId(testId), { target: { value } });
}

function leave(testId: string): void {
  fireEvent.blur(screen.getByTestId(testId));
}

function keyInit(key: string): KeyboardEventInit {
  if (key === ' ') {
    return { key, code: 'Space' };
  }
  return { key, code: /^[a-z]$/i.test(key) ? `Key${key.toUpperCase()}` : '' };
}

/** A key press; false when the page cancelled the browser's own action for it. */
function press(key: string, init: KeyboardEventInit = {}, target: Element = document.body): boolean {
  return fireEvent.keyDown(target, { ...keyInit(key), ...init });
}

const UNDO = { ctrlKey: true };

/** Types into a field the way a user does: every key goes down, changes the text and comes up. */
function typeInto(field: HTMLElement, text: string): void {
  if (!(field instanceof HTMLInputElement)) {
    throw new TypeError('not a text field');
  }
  let typed = field.value;
  for (const char of text) {
    const init = keyInit(char);
    // Nobody may take the key away from the field.
    expect(fireEvent.keyDown(field, init)).toBe(true);
    typed += char;
    fireEvent.change(field, { target: { value: typed } });
    fireEvent.keyUp(field, init);
  }
}

function deferred<T>() {
  const state: { release: (value: T) => void } = { release: () => undefined };
  const promise = new Promise<T>((resolve) => {
    state.release = resolve;
  });
  return { promise, release: (value: T) => {
    state.release(value);
  } };
}

async function saved(): Promise<void> {
  await waitFor(() => {
    expect(screen.getByTestId('clip-save-status')).toHaveTextContent('Збережено');
  });
}

describe('active clip', () => {
  it('makes a pressed card the active clip: the player goes to its start and the fields show it', async () => {
    const { installed } = await openMoments([FIRST, SECOND]);
    const element = loadVideo(0);

    activate(SECOND_ID);

    expect(activeCardIds()).toEqual([SECOND_ID]);
    expect(cardOf(SECOND_ID)).toHaveAttribute('data-active', 'true');
    expect(cardOf(CLIP_ID)).toHaveAttribute('data-active', 'false');
    expect(element.currentTime).toBe(1);
    expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:01.000 / 00:00:12.000');
    expect(screen.getByTestId('clip-editor')).toHaveAttribute('data-clip-id', SECOND_ID);
    expect(screen.getByTestId('clip-title')).toHaveValue('Другий');
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:01.000');
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:03.500');
    // Looking at a clip changes nothing and does not start the video.
    expect(patchesOf(installed)).toEqual([]);
    expect(reviewsOf(installed)).toEqual([]);
    expect(media.play).not.toHaveBeenCalled();

    activate(CLIP_ID);

    expect(activeCardIds()).toEqual([CLIP_ID]);
    expect(element.currentTime).toBe(4.12);
    expect(screen.getByTestId('clip-title')).toHaveValue('Мій фрагмент');
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:04.120');
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:07.880');
  });

  it('is also made by a click anywhere on the card, but not by a click on its own controls', async () => {
    await openMoments([FIRST, SECOND]);
    loadVideo(0);

    fireEvent.click(within(cardOf(SECOND_ID)).getByText(/00:00:01\.000–00:00:03\.500/));
    expect(activeCardIds()).toEqual([SECOND_ID]);

    fireEvent.click(within(cardOf(CLIP_ID)).getByTestId('select-clip'));
    expect(within(cardOf(CLIP_ID)).getByTestId('select-clip')).toBeChecked();
    expect(activeCardIds()).toEqual([SECOND_ID]);

    fireEvent.click(within(cardOf(CLIP_ID)).getByTestId('approve-clip'));
    expect(activeCardIds()).toEqual([SECOND_ID]);
  });

  it('seeks as soon as the video has loaded when the card was pressed before that', async () => {
    await openMoments([FIRST]);

    activate(CLIP_ID);
    const element = video();
    setPlayhead(element, 0);
    fireEvent.loadedMetadata(element);

    expect(element.currentTime).toBe(4.12);
    expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:04.120');
  });

  it('gives way to the form of a new clip on «Створити фрагмент вручну»', async () => {
    await openMoments([FIRST]);
    activate(CLIP_ID);
    expect(screen.getByTestId('clip-editor')).toBeVisible();
    expect(screen.queryByTestId('save-clip')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('manual-clip'));

    expect(screen.queryByTestId('clip-editor')).not.toBeInTheDocument();
    expect(screen.getByTestId('save-clip')).toBeDisabled();
    expect(screen.getByTestId('clip-title')).toHaveValue('');
    expect(screen.getByTestId('clip-title')).toHaveFocus();
    expect(activeCardIds()).toEqual([]);
  });

  it('keeps creating clips as before: the new clip is not made active', async () => {
    const { installed } = await openMoments([]);
    fireEvent.click(screen.getByTestId('manual-clip'));
    fill('clip-start', '00:00:02.000');
    fill('clip-end', '00:00:05.000');

    fireEvent.click(screen.getByTestId('save-clip'));

    await screen.findByTestId('clip-card');
    expect(screen.getByTestId('save-clip')).toBeDisabled();
    expect(screen.queryByTestId('clip-editor')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'clips.create')).toHaveLength(1);
    expect(patchesOf(installed)).toEqual([]);
  });
});

describe('autosave of an edited clip', () => {
  it('stores a new title when the field is left, with the revision, and says «Збережено»', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);

    fill('clip-title', 'Нова назва');
    // Nothing goes out while the user is still typing.
    expect(patchesOf(installed)).toEqual([]);
    leave('clip-title');

    await saved();
    expect(patchesOf(installed)).toEqual([{ id: CLIP_ID, expected_revision: 1, title: 'Нова назва' }]);
    expect(within(cardOf(CLIP_ID)).getByRole('heading', { name: 'Нова назва' })).toBeVisible();
    expect(screen.getByTestId('clip-title')).toHaveValue('Нова назва');
    expect(screen.queryByTestId('clip-save-error')).not.toBeInTheDocument();
  });

  it('stores the title on Enter too', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);

    fill('clip-title', 'З Enter');
    fireEvent.keyDown(screen.getByTestId('clip-title'), { key: 'Enter', code: 'Enter' });

    await saved();
    expect(patchesOf(installed)).toEqual([{ id: CLIP_ID, expected_revision: 1, title: 'З Enter' }]);
  });

  it('stores typed boundaries as milliseconds when a field is left', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);

    fill('clip-start', '00:00:05.000');
    leave('clip-start');
    await saved();

    expect(patchesOf(installed)).toEqual([
      { id: CLIP_ID, expected_revision: 1, start_ms: 5000, end_ms: 7880 },
    ]);
    expect(within(cardOf(CLIP_ID)).getByText(/00:00:05\.000–00:00:07\.880/)).toBeVisible();
  });

  it('shows interval-error for a reversed interval and sends nothing until it is fixed', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);

    fill('clip-start', '00:00:09.000');
    expect(screen.queryByTestId('interval-error')).not.toBeInTheDocument();
    leave('clip-start');

    expect(screen.getByTestId('interval-error')).toHaveTextContent('Кінець має бути пізніше за початок');
    expect(screen.getByTestId('clip-start')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:09.000');
    expect(patchesOf(installed)).toEqual([]);

    fill('clip-end', '00:00:10.000');
    leave('clip-end');
    await saved();

    expect(screen.queryByTestId('interval-error')).not.toBeInTheDocument();
    expect(patchesOf(installed)).toEqual([
      { id: CLIP_ID, expected_revision: 1, start_ms: 9000, end_ms: 10_000 },
    ]);
  });

  it('refuses an empty title, keeps the field and still stores a good boundary', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);

    fill('clip-title', '   ');
    fill('clip-end', '00:00:09.000');
    leave('clip-title');
    await saved();

    expect(screen.getByTestId('clip-title-error')).toHaveTextContent('Назва не може бути порожньою');
    expect(screen.getByTestId('clip-title')).toHaveValue('   ');
    expect(patchesOf(installed)).toEqual([
      { id: CLIP_ID, expected_revision: 1, start_ms: 4120, end_ms: 9000 },
    ]);
  });

  it('sends nothing when a field is left unchanged or typed back to what it was', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);

    leave('clip-title');
    leave('clip-start');
    fill('clip-end', '00:00:09.000');
    fill('clip-end', '00:00:07.880');
    leave('clip-end');
    fill('clip-title', ' Мій фрагмент ');
    leave('clip-title');

    // Give a request, if one were made, the time to go out.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(patchesOf(installed)).toEqual([]);
    expect(screen.getByTestId('clip-save-status')).not.toHaveTextContent('Збережено');
  });

  it('stores at once a boundary moved by a second', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);

    fireEvent.click(screen.getByTestId('clip-end-plus'));
    await saved();

    expect(patchesOf(installed)).toEqual([
      { id: CLIP_ID, expected_revision: 1, start_ms: 4120, end_ms: 8880 },
    ]);
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:08.880');

    fireEvent.click(screen.getByTestId('clip-start-minus'));
    await waitFor(() => {
      expect(patchesOf(installed)).toHaveLength(2);
    });
    expect(patchesOf(installed)[1]).toEqual({
      id: CLIP_ID,
      expected_revision: 2,
      start_ms: 3120,
      end_ms: 8880,
    });
  });

  it('stores at once a boundary taken from the player', async () => {
    const { installed } = await openMoments();
    const element = loadVideo(0);
    activate(CLIP_ID);

    setPlayhead(element, 5.5);
    fireEvent.click(screen.getByTestId('clip-start-here'));
    await saved();

    expect(patchesOf(installed)).toEqual([
      { id: CLIP_ID, expected_revision: 1, start_ms: 5500, end_ms: 7880 },
    ]);
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:05.500');
  });

  it('shows the error, keeps what was typed and stores it on «Спробувати ще раз»', async () => {
    let refused = true;
    const backend = backendOf([FIRST]);
    const { installed } = await openMoments([FIRST], {
      backend,
      // The same backend, except that its first patch is refused.
      handlers: {
        'clips.patch': (params) => {
          if (refused) {
            refused = false;
            return failure('INVALID_INTERVAL', 'Кінець виходить за межі відео');
          }
          return backend.patch(params);
        },
      },
    });
    activate(CLIP_ID);

    fill('clip-title', 'Нова назва');
    leave('clip-title');

    const alert = await screen.findByTestId('clip-save-error');
    expect(alert).toHaveTextContent('Не вдалося зберегти зміни');
    expect(alert).toHaveTextContent('Кінець виходить за межі відео');
    expect(screen.getByTestId('clip-title')).toHaveValue('Нова назва');
    expect(screen.getByTestId('clip-save-status')).not.toHaveTextContent('Збережено');

    fireEvent.click(screen.getByTestId('clip-resave'));

    await saved();
    expect(screen.queryByTestId('clip-save-error')).not.toBeInTheDocument();
    expect(within(cardOf(CLIP_ID)).getByRole('heading', { name: 'Нова назва' })).toBeVisible();
    expect(paramsOf(installed, 'clips.patch')).toHaveLength(2);
  });

  it('sends two quick changes one after another, the second on the revision of the first', async () => {
    const gate = deferred<null>();
    let calls = 0;
    const backend = backendOf([FIRST]);
    const { installed } = await openMoments([FIRST], {
      backend,
      handlers: {
        'clips.patch': async (params) => {
          calls += 1;
          const result = backend.patch(params);
          if (calls === 1) {
            await gate.promise;
          }
          return result;
        },
      },
    });
    activate(CLIP_ID);

    fireEvent.click(screen.getByTestId('clip-end-plus'));
    fireEvent.click(screen.getByTestId('clip-end-plus'));

    await waitFor(() => {
      expect(screen.getByTestId('clip-save-status')).toHaveTextContent('Зберігаємо');
    });
    expect(patchesOf(installed)).toHaveLength(1);
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:09.880');

    gate.release(null);
    await waitFor(() => {
      expect(patchesOf(installed)).toHaveLength(2);
    });
    expect(patchesOf(installed)).toEqual([
      { id: CLIP_ID, expected_revision: 1, start_ms: 4120, end_ms: 8880 },
      { id: CLIP_ID, expected_revision: 2, start_ms: 4120, end_ms: 9880 },
    ]);
    await saved();
    expect(screen.queryByTestId('clip-save-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('clip-conflict')).not.toBeInTheDocument();
  });

  it('lets a review wait for the edit that is still being stored, then uses the new revision', async () => {
    const gate = deferred<null>();
    const backend = backendOf([FIRST]);
    const { installed } = await openMoments([FIRST], {
      backend,
      handlers: {
        'clips.patch': async (params) => {
          const result = backend.patch(params);
          await gate.promise;
          return result;
        },
      },
    });
    activate(CLIP_ID);

    fill('clip-title', 'Нова назва');
    leave('clip-title');
    await waitFor(() => {
      expect(patchesOf(installed)).toHaveLength(1);
    });
    fireEvent.click(within(cardOf(CLIP_ID)).getByTestId('approve-clip'));

    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(reviewsOf(installed)).toEqual([]);

    gate.release(null);
    await waitFor(() => {
      expect(within(cardOf(CLIP_ID)).getByTestId('clip-review-status')).toHaveTextContent('Підтверджено');
    });
    expect(reviewsOf(installed)).toEqual([{ id: CLIP_ID, expected_revision: 2, status: 'approved' }]);
    expect(screen.queryByTestId('clip-error')).not.toBeInTheDocument();
  });
});

describe('a conflicting edit (409)', () => {
  const STORED_ELSEWHERE = makeClip({
    revision: 2,
    title: 'Змінено деінде',
    start_ms: 1000,
    end_ms: 3000,
  });

  async function openWithConflict(): Promise<InstalledBridge> {
    let reads = 0;
    const { installed } = await openMoments([FIRST], {
      handlers: {
        'clips.list': () => {
          reads += 1;
          return ok({ items: [reads === 1 ? FIRST : STORED_ELSEWHERE], next_cursor: null });
        },
        'clips.patch': (params) =>
          params.expected_revision === 1
            ? failure('STALE_REVISION', STALE_MESSAGE)
            : ok({
                ...STORED_ELSEWHERE,
                revision: 3,
                title: params.title ?? STORED_ELSEWHERE.title,
                start_ms: params.start_ms ?? STORED_ELSEWHERE.start_ms,
                end_ms: params.end_ms ?? STORED_ELSEWHERE.end_ms,
              }),
      },
    });
    activate(CLIP_ID);
    fill('clip-title', 'Моя назва');
    leave('clip-title');
    await screen.findByTestId('clip-conflict');
    return installed;
  }

  it('reloads the clip, keeps the typed values in the fields and says what is stored now', async () => {
    const installed = await openWithConflict();

    const conflict = screen.getByTestId('clip-conflict');
    expect(conflict).toHaveAttribute('role', 'alert');
    expect(conflict).toHaveTextContent('змінено в іншому місці');
    expect(conflict).toHaveTextContent('Ваші значення залишаються в полях');
    expect(conflict).toHaveTextContent('«Змінено деінде», 00:00:01.000–00:00:03.000');
    // The user's own edit is still there; the field they did not touch follows the stored clip.
    expect(screen.getByTestId('clip-title')).toHaveValue('Моя назва');
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:01.000');
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:03.000');
    // The list shows the stored clip, and nothing was sent a second time by itself.
    expect(within(cardOf(CLIP_ID)).getByRole('heading', { name: 'Змінено деінде' })).toBeVisible();
    expect(paramsOf(installed, 'clips.list')).toHaveLength(2);
    expect(patchesOf(installed)).toHaveLength(1);
    expect(screen.getByTestId('clip-save-status')).not.toHaveTextContent('Збережено');
  });

  it('stores the typed values on the new revision when the user asks for it', async () => {
    const installed = await openWithConflict();

    fireEvent.click(screen.getByTestId('clip-resave'));

    await saved();
    // Only what the user edited goes out: the other writer's boundaries are not overwritten.
    expect(patchesOf(installed)[1]).toEqual({ id: CLIP_ID, expected_revision: 2, title: 'Моя назва' });
    expect(screen.queryByTestId('clip-conflict')).not.toBeInTheDocument();
    expect(screen.getByTestId('clip-title')).toHaveValue('Моя назва');
    expect(within(cardOf(CLIP_ID)).getByRole('heading', { name: 'Моя назва' })).toBeVisible();
  });

  it('shows the stored values only when the user asks for them', async () => {
    const installed = await openWithConflict();

    fireEvent.click(screen.getByTestId('clip-use-stored'));

    expect(screen.queryByTestId('clip-conflict')).not.toBeInTheDocument();
    expect(screen.getByTestId('clip-title')).toHaveValue('Змінено деінде');
    expect(patchesOf(installed)).toHaveLength(1);
  });

  it('does not call it a conflict when the other change stored exactly what was typed', async () => {
    let reads = 0;
    const { installed } = await openMoments([FIRST], {
      handlers: {
        'clips.list': () => {
          reads += 1;
          return ok({
            items: [reads === 1 ? FIRST : makeClip({ revision: 2, title: 'Моя назва' })],
            next_cursor: null,
          });
        },
        'clips.patch': () => failure('STALE_REVISION', STALE_MESSAGE),
      },
    });
    activate(CLIP_ID);

    fill('clip-title', 'Моя назва');
    leave('clip-title');

    await waitFor(() => {
      expect(paramsOf(installed, 'clips.list')).toHaveLength(2);
    });
    await waitFor(() => {
      expect(within(cardOf(CLIP_ID)).getByRole('heading', { name: 'Моя назва' })).toBeVisible();
    });
    expect(screen.queryByTestId('clip-conflict')).not.toBeInTheDocument();
    expect(screen.getByTestId('clip-title')).toHaveValue('Моя назва');
  });

  it('keeps the edit and says so even when the clip cannot be reloaded', async () => {
    let reads = 0;
    await openMoments([FIRST], {
      handlers: {
        'clips.list': () => {
          reads += 1;
          return reads === 1
            ? ok({ items: [FIRST], next_cursor: null })
            : failure('INTERNAL_ERROR', 'Внутрішня помилка', true);
        },
        'clips.patch': () => failure('STALE_REVISION', STALE_MESSAGE),
      },
    });
    activate(CLIP_ID);

    fill('clip-title', 'Моя назва');
    leave('clip-title');

    expect(await screen.findByTestId('clip-conflict')).toHaveTextContent('Не вдалося оновити дані фрагмента');
    expect(screen.getByTestId('clip-title')).toHaveValue('Моя назва');
  });
});

describe('save state in the project header (docs/03 section 2)', () => {
  const header = (): HTMLElement => screen.getByTestId('saved-state');

  async function headerSays(text: string): Promise<void> {
    await waitFor(() => {
      expect(header()).toHaveTextContent(text);
    });
  }

  it('says «Збережено» with a check mark while nothing is being edited', async () => {
    await openMoments();
    activate(CLIP_ID);

    expect(header()).toHaveTextContent('✓ Збережено');
    expect(header().closest('[role="status"]')).not.toBeNull();
  });

  it('says «Зберігаємо…» for as long as clips.patch is pending, then «Збережено»', async () => {
    const gate = deferred<null>();
    const backend = backendOf([FIRST]);
    const { installed } = await openMoments([FIRST], {
      backend,
      handlers: {
        'clips.patch': async (params) => {
          const result = backend.patch(params);
          await gate.promise;
          return result;
        },
      },
    });
    activate(CLIP_ID);

    fill('clip-title', 'Нова назва');
    leave('clip-title');

    await headerSays('Зберігаємо…');
    expect(header()).toHaveTextContent('◌');
    expect(header()).not.toHaveTextContent('Збережено');
    expect(patchesOf(installed)).toHaveLength(1);
    // The request is still out: the header does not give in.
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(header()).toHaveTextContent('Зберігаємо…');

    gate.release(null);

    await headerSays('Збережено');
    expect(header()).toHaveTextContent('✓');
    expect(header()).not.toHaveTextContent('Зберігаємо');
  });

  it('says that saving failed, and «Збережено» again once «Спробувати ще раз» works', async () => {
    let refused = true;
    const backend = backendOf([FIRST]);
    await openMoments([FIRST], {
      backend,
      handlers: {
        'clips.patch': (params) => {
          if (refused) {
            refused = false;
            return failure('INVALID_INTERVAL', 'Кінець виходить за межі відео');
          }
          return backend.patch(params);
        },
      },
    });
    activate(CLIP_ID);

    fill('clip-title', 'Нова назва');
    leave('clip-title');

    await headerSays('Не вдалося зберегти');
    expect(header()).toHaveTextContent('✕');
    expect(header()).not.toHaveTextContent('Збережено');
    // The details and the way out stay in the editor.
    expect(screen.getByTestId('clip-save-error')).toHaveTextContent('Кінець виходить за межі відео');

    fireEvent.click(screen.getByTestId('clip-resave'));

    await headerSays('Збережено');
    expect(header()).not.toHaveTextContent('Не вдалося');
  });

  it('says that there is a conflict on a 409, and «Збережено» once the user takes the stored values', async () => {
    let reads = 0;
    await openMoments([FIRST], {
      handlers: {
        'clips.list': () => {
          reads += 1;
          const stored = reads === 1 ? FIRST : makeClip({ revision: 2, title: 'Змінено деінде' });
          return ok({ items: [stored], next_cursor: null });
        },
        'clips.patch': () => failure('STALE_REVISION', STALE_MESSAGE),
      },
    });
    activate(CLIP_ID);

    fill('clip-title', 'Моя назва');
    leave('clip-title');

    await headerSays('Конфлікт змін');
    expect(header()).toHaveTextContent('⚠');
    expect(header()).not.toHaveTextContent('Збережено');
    expect(screen.getByTestId('clip-conflict')).toBeVisible();

    fireEvent.click(screen.getByTestId('clip-use-stored'));

    await headerSays('Збережено');
    expect(screen.queryByTestId('clip-conflict')).not.toBeInTheDocument();
  });

  it('goes back to «Збережено» when the Моменти tab is left, because its drafts are gone with it', async () => {
    await openMoments([FIRST], { handlers: { 'clips.patch': () => failure('INTERNAL_ERROR', 'Внутрішня помилка') } });
    activate(CLIP_ID);
    fill('clip-title', 'Нова назва');
    leave('clip-title');
    await headerSays('Не вдалося зберегти');

    fireEvent.click(screen.getByRole('tab', { name: 'Джерела' }));

    await headerSays('Збережено');
  });
});

describe('undo of a boundary change (Ctrl+Z)', () => {
  it('restores the previous boundaries and stores them', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);
    expect(screen.getByTestId('undo-bounds')).toBeDisabled();
    // Nothing to undo yet: the key is left to the browser.
    expect(press('z', UNDO)).toBe(true);
    expect(patchesOf(installed)).toEqual([]);

    fireEvent.click(screen.getByTestId('clip-end-plus'));
    await saved();
    await waitFor(() => {
      expect(screen.getByTestId('undo-bounds')).toBeEnabled();
    });
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:08.880');

    expect(press('z', UNDO)).toBe(false);

    await waitFor(() => {
      expect(patchesOf(installed)).toHaveLength(2);
    });
    expect(patchesOf(installed)[1]).toEqual({
      id: CLIP_ID,
      expected_revision: 2,
      start_ms: 4120,
      end_ms: 7880,
    });
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:07.880');
    await saved();
    expect(within(cardOf(CLIP_ID)).getByText(/00:00:04\.120–00:00:07\.880/)).toBeVisible();
    expect(screen.getByTestId('undo-bounds')).toBeDisabled();

    // Back at the start: another Ctrl+Z has nothing to do.
    expect(press('z', UNDO)).toBe(true);
    expect(patchesOf(installed)).toHaveLength(2);
  });

  it('goes back one change at a time', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);

    fireEvent.click(screen.getByTestId('clip-start-plus'));
    await saved();
    fireEvent.click(screen.getByTestId('clip-end-plus'));
    await waitFor(() => {
      expect(patchesOf(installed)).toHaveLength(2);
    });
    await waitFor(() => {
      expect(screen.getByTestId('undo-bounds')).toBeEnabled();
    });
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:05.120');
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:08.880');

    press('z', UNDO);
    await waitFor(() => {
      expect(patchesOf(installed)).toHaveLength(3);
    });
    await waitFor(() => {
      expect(screen.getByTestId('undo-bounds')).toBeEnabled();
    });
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:07.880');
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:05.120');

    press('z', UNDO);
    await waitFor(() => {
      expect(patchesOf(installed)).toHaveLength(4);
    });
    await waitFor(() => {
      expect(screen.getByTestId('clip-start')).toHaveValue('00:00:04.120');
    });
    expect(patchesOf(installed).slice(2)).toEqual([
      { id: CLIP_ID, expected_revision: 3, start_ms: 5120, end_ms: 7880 },
      { id: CLIP_ID, expected_revision: 4, start_ms: 4120, end_ms: 7880 },
    ]);
  });

  it('works from the button too', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);
    fireEvent.click(screen.getByTestId('clip-start-plus'));
    await saved();
    await waitFor(() => {
      expect(screen.getByTestId('undo-bounds')).toBeEnabled();
    });

    fireEvent.click(screen.getByTestId('undo-bounds'));

    await waitFor(() => {
      expect(patchesOf(installed)).toHaveLength(2);
    });
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:04.120');
  });

  it('does not take Ctrl+Z from a field: that is the text undo of the browser', async () => {
    const { installed } = await openMoments();
    activate(CLIP_ID);
    fireEvent.click(screen.getByTestId('clip-end-plus'));
    await saved();
    await waitFor(() => {
      expect(screen.getByTestId('undo-bounds')).toBeEnabled();
    });
    const title = screen.getByTestId('clip-title');
    title.focus();

    expect(press('z', UNDO, title)).toBe(true);

    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(patchesOf(installed)).toHaveLength(1);
  });
});

describe('keyboard shortcuts', () => {
  describe('playback', () => {
    it('Space plays and pauses, once the video has loaded', async () => {
      await openMoments();
      expect(press(' ')).toBe(false);
      expect(media.play).not.toHaveBeenCalled();
      const element = loadVideo();

      expect(press(' ')).toBe(false);
      expect(media.play).toHaveBeenCalledTimes(1);
      expect(media.pause).not.toHaveBeenCalled();

      Object.defineProperty(element, 'paused', { configurable: true, value: false });
      press(' ');
      expect(media.pause).toHaveBeenCalledTimes(1);
      expect(media.play).toHaveBeenCalledTimes(1);
    });

    it('Space leaves a focused button to press itself', async () => {
      await openMoments();
      loadVideo();
      const next = screen.getByTestId('next-clip');
      next.focus();

      expect(press(' ', {}, next)).toBe(true);

      expect(media.play).not.toHaveBeenCalled();
    });

    it('J and L move the playhead a second back and forward, inside the video', async () => {
      await openMoments();
      const element = loadVideo(5);

      expect(press('l')).toBe(false);
      expect(element.currentTime).toBe(6);
      expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:06.000');
      press('j');
      press('j');
      expect(element.currentTime).toBe(4);

      setPlayhead(element, 0.4);
      fireEvent.timeUpdate(element);
      press('j');
      expect(element.currentTime).toBe(0);

      setPlayhead(element, 11.5);
      fireEvent.timeUpdate(element);
      press('l');
      expect(element.currentTime).toBe(12);
    });

    it('J and L keep going while held, the other keys do not repeat', async () => {
      const { installed } = await openMoments();
      const element = loadVideo(5);
      activate(CLIP_ID);

      press('l', { repeat: true });
      press('l', { repeat: true });
      press('a', { repeat: true });

      expect(element.currentTime).toBe(6.12);
      expect(reviewsOf(installed)).toEqual([]);
    });

    it('does not move anything before the video has loaded', async () => {
      await openMoments();

      press('l');
      press(' ');

      expect(media.play).not.toHaveBeenCalled();
      expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:00.000');
    });
  });

  describe('boundaries (I and O)', () => {
    it('I and O store the playhead as the start and the end of the active clip', async () => {
      const { installed } = await openMoments();
      const element = loadVideo(0);
      activate(CLIP_ID);

      setPlayhead(element, 5.5);
      expect(press('i')).toBe(false);
      await saved();
      expect(patchesOf(installed)).toEqual([
        { id: CLIP_ID, expected_revision: 1, start_ms: 5500, end_ms: 7880 },
      ]);
      expect(screen.getByTestId('clip-start')).toHaveValue('00:00:05.500');

      setPlayhead(element, 9);
      press('o');
      await waitFor(() => {
        expect(patchesOf(installed)).toHaveLength(2);
      });
      expect(patchesOf(installed)[1]).toEqual({
        id: CLIP_ID,
        expected_revision: 2,
        start_ms: 5500,
        end_ms: 9000,
      });
      expect(screen.getByTestId('clip-end')).toHaveValue('00:00:09.000');
    });

    it('shows interval-error and stores nothing when the playhead would reverse the interval', async () => {
      const { installed } = await openMoments();
      const element = loadVideo(0);
      activate(CLIP_ID);

      setPlayhead(element, 8);
      press('i');

      expect(screen.getByTestId('interval-error')).toHaveTextContent('Кінець має бути пізніше за початок');
      expect(screen.getByTestId('clip-start')).toHaveValue('00:00:08.000');
      expect(patchesOf(installed)).toEqual([]);
    });

    it('I and O fill the form of a new clip while no clip is active', async () => {
      const { installed } = await openMoments([]);
      const element = loadVideo(0);
      fireEvent.click(screen.getByTestId('manual-clip'));
      screen.getByTestId('clip-title').blur();

      setPlayhead(element, 2.25);
      press('i');
      setPlayhead(element, 6.5);
      press('o');

      expect(screen.getByTestId('clip-start')).toHaveValue('00:00:02.250');
      expect(screen.getByTestId('clip-end')).toHaveValue('00:00:06.500');
      expect(screen.getByTestId('save-clip')).toBeEnabled();
      expect(patchesOf(installed)).toEqual([]);
    });

    it('does nothing before the video has loaded', async () => {
      const { installed } = await openMoments();
      activate(CLIP_ID);

      expect(press('i')).toBe(true);

      expect(screen.getByTestId('clip-start')).toHaveValue('00:00:04.120');
      expect(patchesOf(installed)).toEqual([]);
    });
  });

  describe('review (A and R)', () => {
    it('A approves the active clip and R rejects it, each with the revision', async () => {
      const { installed } = await openMoments([FIRST, SECOND]);
      activate(CLIP_ID);

      expect(press('a')).toBe(false);
      await waitFor(() => {
        expect(within(cardOf(CLIP_ID)).getByTestId('clip-review-status')).toHaveTextContent('Підтверджено');
      });
      expect(reviewsOf(installed)).toEqual([{ id: CLIP_ID, expected_revision: 1, status: 'approved' }]);

      activate(SECOND_ID);
      press('r');
      await waitFor(() => {
        expect(within(cardOf(SECOND_ID)).getByTestId('clip-review-status')).toHaveTextContent('Відхилено');
      });
      expect(reviewsOf(installed)).toEqual([
        { id: CLIP_ID, expected_revision: 1, status: 'approved' },
        { id: SECOND_ID, expected_revision: 1, status: 'rejected' },
      ]);
      // The other decision of the first clip is untouched, and nothing is ticked for export.
      expect(within(cardOf(CLIP_ID)).getByTestId('clip-review-status')).toHaveTextContent('Підтверджено');
      expect(within(cardOf(CLIP_ID)).getByTestId('select-clip')).not.toBeChecked();
    });

    it('act on a clip that is still to be reviewed, like the buttons of its card', async () => {
      const { installed } = await openMoments();
      activate(CLIP_ID);
      press('a');
      await waitFor(() => {
        expect(reviewsOf(installed)).toHaveLength(1);
      });
      await waitFor(() => {
        expect(within(cardOf(CLIP_ID)).getByTestId('clip-review-status')).toHaveTextContent('Підтверджено');
      });

      expect(press('a')).toBe(true);
      expect(press('r')).toBe(true);

      await new Promise((resolve) => setTimeout(resolve, 30));
      expect(reviewsOf(installed)).toHaveLength(1);
    });

    it('do nothing while no clip is active', async () => {
      const { installed } = await openMoments();

      expect(press('a')).toBe(true);
      expect(press('r')).toBe(true);

      await new Promise((resolve) => setTimeout(resolve, 30));
      expect(reviewsOf(installed)).toEqual([]);
    });

    it('A with a modifier is not a shortcut', async () => {
      const { installed } = await openMoments();
      activate(CLIP_ID);

      press('a', { ctrlKey: true });
      press('a', { altKey: true });
      press('r', { metaKey: true });
      press('r', { shiftKey: true });

      await new Promise((resolve) => setTimeout(resolve, 30));
      expect(reviewsOf(installed)).toEqual([]);
    });

    it('work on the Ukrainian layout, by the physical key', async () => {
      const { installed } = await openMoments();
      activate(CLIP_ID);

      press('ф', { code: 'KeyA' });

      await waitFor(() => {
        expect(reviewsOf(installed)).toHaveLength(1);
      });
    });
  });

  describe('while text is being typed', () => {
    async function typedKeysChangeNothing(testId: string): Promise<void> {
      const { installed } = await openMoments([FIRST]);
      const element = loadVideo(5);
      activate(CLIP_ID);
      setPlayhead(element, 5);
      const field = screen.getByTestId(testId);
      field.focus();
      fireEvent.change(field, { target: { value: '' } });

      typeInto(field, 'i o a r j l ');

      expect(field).toHaveValue('i o a r j l ');
      expect(field).toHaveFocus();
      expect(patchesOf(installed)).toEqual([]);
      expect(reviewsOf(installed)).toEqual([]);
      expect(media.play).not.toHaveBeenCalled();
      expect(media.pause).not.toHaveBeenCalled();
      expect(element.currentTime).toBe(5);
      expect(within(cardOf(CLIP_ID)).getByTestId('clip-review-status')).toHaveTextContent('На розгляді');
      for (const other of ['clip-title', 'clip-start', 'clip-end'].filter((id) => id !== testId)) {
        expect(screen.getByTestId(other)).not.toHaveValue('i o a r j l ');
      }
    }

    it.each(['clip-title', 'clip-start', 'clip-end'])(
      'the keys i o a r j l and Space type into %s of the clip editor and do nothing else',
      typedKeysChangeNothing,
    );

    it.each(['clip-title', 'clip-start', 'clip-end'])(
      'the keys i o a r j l and Space type into %s of the form of a new clip and do nothing else',
      async (testId) => {
        const { installed } = await openMoments([FIRST]);
        const element = loadVideo(5);
        fireEvent.click(screen.getByTestId('manual-clip'));
        const field = screen.getByTestId(testId);
        field.focus();

        typeInto(field, 'i o a r j l ');

        expect(field).toHaveValue('i o a r j l ');
        expect(field).toHaveFocus();
        expect(reviewsOf(installed)).toEqual([]);
        expect(patchesOf(installed)).toEqual([]);
        expect(media.play).not.toHaveBeenCalled();
        expect(element.currentTime).toBe(5);
        for (const other of ['clip-title', 'clip-start', 'clip-end'].filter((id) => id !== testId)) {
          expect(screen.getByTestId(other)).toHaveValue('');
        }
      },
    );

    it('a list that has the focus keeps its keys too', async () => {
      const { installed } = await openMoments();
      loadVideo(5);
      activate(CLIP_ID);
      const speed = screen.getByTestId('player-speed');
      speed.focus();

      expect(press('a', {}, speed)).toBe(true);
      expect(press('r', {}, speed)).toBe(true);
      expect(press(' ', {}, speed)).toBe(true);

      expect(reviewsOf(installed)).toEqual([]);
      expect(media.play).not.toHaveBeenCalled();
    });

    it('editable content keeps its keys', async () => {
      const { installed } = await openMoments();
      activate(CLIP_ID);
      const editable = document.createElement('div');
      editable.setAttribute('contenteditable', 'true');
      document.body.append(editable);

      expect(press('a', {}, editable)).toBe(true);
      expect(press('i', {}, editable)).toBe(true);

      editable.remove();
      expect(reviewsOf(installed)).toEqual([]);
      expect(patchesOf(installed)).toEqual([]);
    });
  });

  describe('a checkbox that has the focus', () => {
    /** The tick box of the active card, just clicked: the focus stays on it. */
    async function focusedSelectBox(): Promise<{ installed: InstalledBridge; box: HTMLElement }> {
      const opened = await openMoments();
      activate(CLIP_ID);
      const box = within(cardOf(CLIP_ID)).getByTestId('select-clip');
      fireEvent.click(box);
      box.focus();
      expect(box).toBeChecked();
      expect(box).toHaveFocus();
      return { installed: opened.installed, box };
    }

    it('keeps A and R: the active clip is reviewed, not typed into', async () => {
      const { installed, box } = await focusedSelectBox();

      expect(press('a', {}, box)).toBe(false);

      await waitFor(() => {
        expect(within(cardOf(CLIP_ID)).getByTestId('clip-review-status')).toHaveTextContent('Підтверджено');
      });
      expect(reviewsOf(installed)).toEqual([{ id: CLIP_ID, expected_revision: 1, status: 'approved' }]);
      expect(within(cardOf(CLIP_ID)).getByTestId('select-clip')).toBeChecked();
    });

    it('keeps R', async () => {
      const { installed, box } = await focusedSelectBox();

      press('r', {}, box);

      await waitFor(() => {
        expect(reviewsOf(installed)).toEqual([{ id: CLIP_ID, expected_revision: 1, status: 'rejected' }]);
      });
    });

    it('keeps I, O, J and L', async () => {
      const { installed } = await openMoments();
      const element = loadVideo(0);
      activate(CLIP_ID);
      const box = within(cardOf(CLIP_ID)).getByTestId('select-clip');
      fireEvent.click(box);
      box.focus();

      expect(press('l', {}, box)).toBe(false);
      expect(element.currentTime).toBe(5.12);
      expect(press('j', {}, box)).toBe(false);
      expect(element.currentTime).toBe(4.12);

      setPlayhead(element, 5.5);
      expect(press('i', {}, box)).toBe(false);
      await saved();
      setPlayhead(element, 9);
      press('o', {}, box);
      await waitFor(() => {
        expect(patchesOf(installed)).toHaveLength(2);
      });
      expect(patchesOf(installed)).toEqual([
        { id: CLIP_ID, expected_revision: 1, start_ms: 5500, end_ms: 7880 },
        { id: CLIP_ID, expected_revision: 2, start_ms: 5500, end_ms: 9000 },
      ]);
    });

    it('leaves Space to the checkbox: it is ticked by the browser, the playback is not toggled', async () => {
      const { installed, box } = await focusedSelectBox();
      loadVideo(0);

      // Not cancelled: the browser's own action for Space on a checkbox is to tick it.
      expect(press(' ', {}, box)).toBe(true);
      fireEvent.click(box);
      expect(box).not.toBeChecked();

      expect(media.play).not.toHaveBeenCalled();
      expect(media.pause).not.toHaveBeenCalled();
      expect(reviewsOf(installed)).toEqual([]);
    });
  });

  describe('only while Моменти is the open tab', () => {
    const KEYS: readonly (readonly [string, KeyboardEventInit])[] = [
      [' ', {}],
      ['i', {}],
      ['o', {}],
      ['a', {}],
      ['r', {}],
      ['j', {}],
      ['l', {}],
      ['z', UNDO],
    ];

    it('does nothing on another tab, and works again when Моменти is opened again', async () => {
      const { installed } = await openMoments();
      loadVideo(5);
      activate(CLIP_ID);
      fireEvent.click(screen.getByTestId('clip-end-plus'));
      await saved();

      fireEvent.click(screen.getByRole('tab', { name: 'Джерела' }));
      await screen.findByTestId('source-ready');
      for (const [key, init] of KEYS) {
        expect(press(key, init)).toBe(true);
      }

      await new Promise((resolve) => setTimeout(resolve, 30));
      expect(media.play).not.toHaveBeenCalled();
      expect(media.pause).not.toHaveBeenCalled();
      expect(reviewsOf(installed)).toEqual([]);
      expect(patchesOf(installed)).toHaveLength(1);

      fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
      await screen.findByTestId('player');
      loadVideo(5);
      await screen.findAllByTestId('clip-card');
      activate(CLIP_ID);
      press(' ');
      expect(media.play).toHaveBeenCalledTimes(1);
    });

    it('does nothing while the project is not open at all', async () => {
      const installed = installBridge({ handlers: { 'projects.list': listOf(makeProject()) } });
      render(<App />);
      await screen.findByTestId('project-item');

      for (const [key, init] of KEYS) {
        expect(press(key, init)).toBe(true);
      }

      expect(installed.call.mock.calls.map(([op]) => op)).not.toContain('clips.review');
      expect(media.play).not.toHaveBeenCalled();
    });

    it('does nothing in Моменти without sources', async () => {
      const installed = installBridge({ handlers: { 'projects.list': listOf(makeProject()) } });
      render(<App />);
      fireEvent.click(await screen.findByTestId('project-item'));
      fireEvent.click(await screen.findByRole('tab', { name: 'Моменти' }));
      await screen.findByTestId('clips-no-sources');

      for (const [key, init] of KEYS) {
        expect(press(key, init)).toBe(true);
      }

      expect(installed.call.mock.calls.map(([op]) => op)).not.toContain('clips.review');
    });
  });

  describe('dialogs and Escape', () => {
    it('leave every key to an open dialog, and work again when it is closed', async () => {
      const { installed } = await openMoments([FIRST, SECOND], {
        bridge: { chooseExportFolder: () => ({ token: FOLDER_TOKEN, displayName: 'Нарізка' }) },
      });
      loadVideo(5);
      activate(CLIP_ID);
      press('a');
      await waitFor(() => {
        expect(within(cardOf(CLIP_ID)).getByTestId('clip-review-status')).toHaveTextContent('Підтверджено');
      });
      fireEvent.click(within(cardOf(CLIP_ID)).getByTestId('select-clip'));
      activate(SECOND_ID);
      fireEvent.click(screen.getByTestId('export-selected'));
      const dialog = await screen.findByTestId('export-summary');
      const reviewsBefore = reviewsOf(installed).length;
      const audio = within(dialog).getByTestId('export-audio');

      for (const target of [document.body, dialog, audio, screen.getByTestId('player-play')]) {
        for (const key of [' ', 'a', 'r', 'i', 'o', 'j', 'l']) {
          expect(press(key, {}, target)).toBe(true);
        }
        expect(press('z', UNDO, target)).toBe(true);
      }

      expect(reviewsOf(installed)).toHaveLength(reviewsBefore);
      expect(patchesOf(installed)).toEqual([]);
      expect(media.play).not.toHaveBeenCalled();

      // Escape closes the local dialog.
      fireEvent.keyDown(audio, { key: 'Escape', code: 'Escape' });
      expect(screen.queryByTestId('export-summary')).not.toBeInTheDocument();
      expect(installed.call.mock.calls.map(([op]) => op)).not.toContain('jobs.cancel');

      press('r');
      await waitFor(() => {
        expect(reviewsOf(installed)).toHaveLength(reviewsBefore + 1);
      });
      expect(reviewsOf(installed).at(-1)).toEqual({ id: SECOND_ID, expected_revision: 1, status: 'rejected' });
    });

    it('Escape never cancels the job that prepares the preview, wherever the focus is', async () => {
      const { installed } = await openMoments([FIRST], {
        source: makeSource({
          playback: { direct: false, reason: 'NONZERO_ORIGIN', direct_audio_stream_index: null },
        }),
        handlers: {
          'previews.create': () => ok({ id: PREVIEW_ID, job_id: JOB_ID, status: 'queued' }),
          'previews.get': () =>
            ok({
              id: PREVIEW_ID,
              source_id: FIRST.source_id,
              audio_stream_index: 1,
              status: 'running',
              job_id: JOB_ID,
              error: null,
            }),
          'jobs.get': () =>
            ok({ id: JOB_ID, type: 'preview', state: 'running', progress: { percent: 10 }, error: null }),
          'jobs.cancel': () =>
            ok({ id: JOB_ID, type: 'preview', state: 'cancelling', progress: {}, error: null }),
        },
      });
      await screen.findByTestId('proxy-progress');
      const cancel = screen.getByTestId('proxy-cancel');
      cancel.focus();

      for (const target of [document.body, cancel, screen.getByTestId('manual-clip'), cardOf(CLIP_ID)]) {
        fireEvent.keyDown(target, { key: 'Escape', code: 'Escape' });
        fireEvent.keyUp(target, { key: 'Escape', code: 'Escape' });
      }
      fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' });

      expect(installed.call.mock.calls.map(([op]) => op)).not.toContain('jobs.cancel');
      expect(screen.getByTestId('proxy-progress')).toBeVisible();
    });
  });
});

describe('«Наступний»', () => {
  const clips = [
    makeClip({ id: CLIP_ID, title: 'Перший', start_ms: 1000, end_ms: 2000 }),
    makeClip({ id: SECOND_ID, title: 'Другий', start_ms: 2000, end_ms: 3000, review_status: 'approved' }),
    makeClip({ id: THIRD_ID, title: 'Третій', start_ms: 3000, end_ms: 4000 }),
    makeClip({ id: FOURTH_ID, title: 'Четвертий', start_ms: 4000, end_ms: 5000, review_status: 'rejected' }),
  ];

  it('is not shown without clips', async () => {
    await openMoments([]);

    expect(screen.queryByTestId('next-clip')).not.toBeInTheDocument();
  });

  it('moves to the next clip still to review, in list order, skipping the reviewed ones', async () => {
    await openMoments(clips);
    const element = loadVideo(0);

    fireEvent.click(screen.getByTestId('next-clip'));
    expect(activeCardIds()).toEqual([CLIP_ID]);
    expect(element.currentTime).toBe(1);
    expect(screen.getByTestId('clip-title')).toHaveValue('Перший');

    fireEvent.click(screen.getByTestId('next-clip'));
    expect(activeCardIds()).toEqual([THIRD_ID]);
    expect(element.currentTime).toBe(3);
    expect(screen.getByTestId('clip-title')).toHaveValue('Третій');
  });

  it('goes on from the clip that was just approved, and wraps around to the start', async () => {
    const { installed } = await openMoments([
      ...clips.slice(0, 1),
      makeClip({ id: SECOND_ID, title: 'Другий', start_ms: 2000, end_ms: 3000 }),
      ...clips.slice(2, 3),
    ]);
    loadVideo(0);
    activate(SECOND_ID);

    press('a');
    await waitFor(() => {
      expect(reviewsOf(installed)).toHaveLength(1);
    });
    await waitFor(() => {
      expect(within(cardOf(SECOND_ID)).getByTestId('clip-review-status')).toHaveTextContent('Підтверджено');
    });
    fireEvent.click(screen.getByTestId('next-clip'));
    expect(activeCardIds()).toEqual([THIRD_ID]);

    fireEvent.click(screen.getByTestId('next-clip'));
    expect(activeCardIds()).toEqual([CLIP_ID]);
  });

  it('says so and stays where it is when nothing else is waiting for review', async () => {
    await openMoments([
      makeClip({ id: CLIP_ID, title: 'Перший' }),
      makeClip({ id: SECOND_ID, title: 'Другий', review_status: 'approved' }),
    ]);
    activate(CLIP_ID);

    fireEvent.click(screen.getByTestId('next-clip'));

    expect(screen.getByTestId('next-clip-status')).toHaveTextContent(
      'Немає інших фрагментів, що чекають на перевірку',
    );
    expect(activeCardIds()).toEqual([CLIP_ID]);

    activate(SECOND_ID);
    expect(screen.queryByTestId('next-clip-status')).not.toBeInTheDocument();
  });
});

describe('the keys are shown', () => {
  it('on the play button, the boundary buttons, the undo button and the active card only', async () => {
    await openMoments([FIRST, SECOND]);
    loadVideo(0);

    expect(screen.getByTestId('player-play')).toHaveAttribute('aria-keyshortcuts', 'Space');
    expect(screen.getByTestId('player-play')).toHaveAttribute('title', 'Грати (Пробіл)');
    expect(screen.getByTestId('player-seek')).toHaveAttribute('aria-keyshortcuts', 'J L');
    expect(screen.getByTestId('player-seek').getAttribute('title')).toContain('J: −1 с, L: +1 с');
    // Nothing is active yet: no card promises that A and R act on it.
    for (const button of screen.getAllByTestId('approve-clip')) {
      expect(button).not.toHaveAttribute('aria-keyshortcuts');
    }

    activate(CLIP_ID);

    expect(screen.getByTestId('clip-start-here')).toHaveAttribute('aria-keyshortcuts', 'I');
    expect(screen.getByTestId('clip-start-here')).toHaveAttribute('title', 'Встановити початок тут (I)');
    expect(screen.getByTestId('clip-end-here')).toHaveAttribute('aria-keyshortcuts', 'O');
    expect(screen.getByTestId('clip-end-here')).toHaveAttribute('title', 'Встановити кінець тут (O)');
    expect(screen.getByTestId('undo-bounds')).toHaveAttribute('aria-keyshortcuts', 'Control+Z');
    expect(screen.getByTestId('undo-bounds')).toHaveAttribute('title', 'Скасувати останню зміну меж (Ctrl+Z)');
    const approve = within(cardOf(CLIP_ID)).getByTestId('approve-clip');
    expect(approve).toHaveAttribute('aria-keyshortcuts', 'A');
    expect(approve).toHaveAttribute('title', 'Підтвердити (A)');
    const reject = within(cardOf(CLIP_ID)).getByTestId('reject-clip');
    expect(reject).toHaveAttribute('aria-keyshortcuts', 'R');
    expect(reject).toHaveAttribute('title', 'Відхилити (R)');
    expect(within(cardOf(SECOND_ID)).getByTestId('approve-clip')).not.toHaveAttribute('aria-keyshortcuts');
    expect(within(cardOf(SECOND_ID)).getByTestId('approve-clip')).not.toHaveAttribute('title');
  });

  it('as a line of text that lists all of them, and the form of a new clip names I and O', async () => {
    await openMoments([FIRST]);

    const hint = screen.getByTestId('hotkeys-hint');
    for (const part of ['Пробіл', 'I і O', 'A і R', 'J і L', 'Ctrl+Z', 'курсор у полі']) {
      expect(hint).toHaveTextContent(part);
    }

    fireEvent.click(screen.getByTestId('manual-clip'));

    expect(screen.getByTestId('clip-start-here')).toHaveAttribute('aria-keyshortcuts', 'I');
    expect(screen.getByTestId('clip-end-here')).toHaveAttribute('title', 'Встановити кінець тут (O)');
  });
});
