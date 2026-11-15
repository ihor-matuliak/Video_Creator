import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ClipDto, JobDto, PlaybackDto, PreviewDto, SourceDto } from '../shared/api';
import { App } from './App';
import {
  SOURCE_ID,
  failure,
  installBridge,
  listOf,
  makeClip,
  makeProject,
  makeSource,
  ok,
} from './test-bridge';
import type { ApiHandlers, InstalledBridge } from './test-bridge';

const PREVIEW_ID = '7d9f2b4a-6c1e-4f3a-9b5d-1a2b3c4d5e6f';
const JOB_ID = '2e4a6c8b-1d3f-4a5b-8c7d-9e0f1a2b3c4e';
const SECOND_SOURCE_ID = '9b2e5a7d-3c4f-4d6e-9f80-1b0c2d3e4f5a';

const TWO_TRACKS: SourceDto['audio_streams'] = [
  { index: 1, codec: 'aac', channels: 2, language: 'uk', title: 'Гра', default: true, start_offset_ms: 0 },
  { index: 2, codec: 'aac', channels: 2, language: null, title: 'Мікрофон', default: false, start_offset_ms: 0 },
];

const NEEDS_PROXY: PlaybackDto = {
  direct: false,
  reason: 'NONZERO_ORIGIN',
  direct_audio_stream_index: null,
};

let media: { play: ReturnType<typeof stubPlay>; pause: ReturnType<typeof stubPause> };

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

/** A fake backend for clips: the list, create and review calls act on one array. */
function clipBackend(initial: readonly ClipDto[] = []): { clips: ClipDto[]; handlers: ApiHandlers } {
  const clips = [...initial];
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
    'clips.review': (params) => {
      const index = clips.findIndex((clip) => clip.id === params.id);
      const current = clips[index];
      if (current === undefined) {
        return failure('CLIP_NOT_FOUND', 'Кліп не знайдено');
      }
      if (current.revision !== params.expected_revision) {
        return failure('STALE_REVISION', 'Кліп змінено в іншому місці. Оновіть дані та повторіть');
      }
      const next = { ...current, revision: current.revision + 1, review_status: params.status };
      clips[index] = next;
      return ok(next);
    },
  };
  return { clips, handlers };
}

function previewOf(status: string, overrides: Partial<PreviewDto> = {}): PreviewDto {
  return {
    id: PREVIEW_ID,
    source_id: SOURCE_ID,
    audio_stream_index: 1,
    status,
    job_id: JOB_ID,
    error: null,
    ...overrides,
  };
}

function jobOf(percent: number): JobDto {
  return { id: JOB_ID, type: 'preview', state: 'running', progress: { stage: 'render', percent }, error: null };
}

const CREATED = (status: string) => ok({ id: PREVIEW_ID, job_id: JOB_ID, status });

/** The project «Перевірка інтерфейсу» with `source` imported, on the Sources tab. */
async function openWithSource(source: SourceDto, handlers: ApiHandlers = {}): Promise<InstalledBridge> {
  const installed = installBridge({
    importVideo: () => ok(source),
    handlers: { 'projects.list': listOf(makeProject()), ...clipBackend().handlers, ...handlers },
  });
  render(<App />);
  fireEvent.click(await screen.findByTestId('project-item'));
  fireEvent.click(await screen.findByTestId('import-video'));
  await screen.findByTestId('source-ready');
  return installed;
}

async function openMoments(source: SourceDto, handlers: ApiHandlers = {}): Promise<InstalledBridge> {
  const installed = await openWithSource(source, handlers);
  fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
  await screen.findByTestId('player');
  return installed;
}

async function openManualForm(source: SourceDto, handlers: ApiHandlers = {}): Promise<InstalledBridge> {
  const installed = await openWithSource(source, handlers);
  fireEvent.click(screen.getByTestId('manual-clip'));
  await screen.findByTestId('clip-title');
  return installed;
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

/** The params of every call of one op, in order. */
function paramsOf(call: InstalledBridge['call'], op: string): unknown[] {
  return call.mock.calls.filter(([called]) => called === op).map(([, params]) => params);
}

function fill(testId: string, value: string): void {
  fireEvent.change(screen.getByTestId(testId), { target: { value } });
}

function fillInterval(start: string, end: string): void {
  fill('clip-start', start);
  fill('clip-end', end);
}

describe('Моменти: entry', () => {
  it('shows a visible «Створити фрагмент вручну» as soon as the source is ready', async () => {
    await openWithSource(makeSource());

    const button = screen.getByTestId('manual-clip');
    expect(button).toBeVisible();
    expect(button).toHaveTextContent('Створити фрагмент вручну');
    expect(screen.getByTestId('source-ready')).toBeVisible();
  });

  it('opens Моменти with the manual form and the cursor in the title', async () => {
    const { call } = await openManualForm(makeSource());

    expect(screen.getByRole('tab', { name: 'Моменти' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel', { name: 'Моменти' })).toBeVisible();
    for (const id of ['clip-title', 'clip-start', 'clip-end', 'save-clip']) {
      expect(screen.getByTestId(id)).toBeVisible();
    }
    expect(screen.getByTestId('clip-title')).toHaveFocus();
    expect(call).toHaveBeenCalledWith('clips.list', { source_id: SOURCE_ID });
  });

  it('opens Моменти from the tab without the form, which «Створити фрагмент вручну» then shows', async () => {
    await openMoments(makeSource());

    expect(screen.queryByTestId('clip-title')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('manual-clip'));

    expect(screen.getByTestId('clip-title')).toHaveFocus();
  });

  it('asks to add a video first when the project has no sources', async () => {
    installBridge({ handlers: { 'projects.list': listOf(makeProject()) } });
    render(<App />);
    fireEvent.click(await screen.findByTestId('project-item'));
    fireEvent.click(await screen.findByRole('tab', { name: 'Моменти' }));

    expect(await screen.findByTestId('clips-no-sources')).toHaveTextContent('Спочатку додай відео');
    fireEvent.click(screen.getByRole('button', { name: 'Перейти до джерел' }));

    expect(await screen.findByTestId('import-video')).toBeVisible();
  });
});

describe('Моменти: player source (ADR 021)', () => {
  it('plays the original directly for a direct source and the first audio stream', async () => {
    const { call } = await openMoments(makeSource());

    expect(video()).toHaveAttribute('src', `app-media://source/${SOURCE_ID}`);
    expect(screen.getByTestId('player')).toHaveAttribute('data-mode', 'direct');
    expect(screen.queryByTestId('proxy-reason')).not.toBeInTheDocument();
    expect(screen.queryByTestId('proxy-progress')).not.toBeInTheDocument();
    expect(call).not.toHaveBeenCalledWith('previews.create', expect.anything() as never);
  });

  it('does not start playing by itself and starts at the beginning', async () => {
    await openMoments(makeSource());

    expect(video()).not.toHaveAttribute('autoplay');
    expect(media.play).not.toHaveBeenCalled();
  });

  it('asks for a proxy when the selected audio track is not the direct one', async () => {
    const { call } = await openMoments(makeSource({ audio_streams: TWO_TRACKS }), {
      'previews.create': () => CREATED('ready'),
    });
    expect(video()).toHaveAttribute('src', `app-media://source/${SOURCE_ID}`);

    fireEvent.change(screen.getByTestId('player-audio'), { target: { value: '2' } });

    await waitFor(() => {
      expect(video()).toHaveAttribute('src', `app-media://preview/${PREVIEW_ID}`);
    });
    expect(paramsOf(call, 'previews.create')).toEqual([
      { source_id: SOURCE_ID, audio_stream_index: 2 },
    ]);
    expect(screen.getByTestId('player')).toHaveAttribute('data-mode', 'proxy');
    expect(screen.getByTestId('proxy-reason')).toHaveTextContent('обрана звукова доріжка');

    fireEvent.change(screen.getByTestId('player-audio'), { target: { value: '1' } });

    expect(video()).toHaveAttribute('src', `app-media://source/${SOURCE_ID}`);
    expect(screen.queryByTestId('proxy-reason')).not.toBeInTheDocument();
  });

  it('lists the audio tracks only when there is a choice', async () => {
    await openMoments(makeSource({ audio_streams: TWO_TRACKS }));
    const select = screen.getByTestId('player-audio');

    expect(within(select).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Доріжка 1 · uk · Гра',
      'Доріжка 2 · Мікрофон',
    ]);
    expect(select).toHaveValue('1');
  });

  it('goes straight to the proxy for a source main marked as not direct, with the reason', async () => {
    const { call } = await openMoments(makeSource({ playback: NEEDS_PROXY }), {
      'previews.create': () => CREATED('queued'),
      'previews.get': () => ok(previewOf('running')),
      'jobs.get': () => ok(jobOf(10)),
    });

    expect(await screen.findByTestId('proxy-progress')).toHaveTextContent('Готуємо версію для перегляду…');
    expect(screen.queryByTestId('player-video')).not.toBeInTheDocument();
    expect(call).toHaveBeenCalledWith('previews.create', { source_id: SOURCE_ID, audio_stream_index: 1 });
    expect(screen.getByTestId('proxy-reason')).toHaveTextContent(
      'відео починається не з нульової позначки часу',
    );
    expect(screen.getByTestId('proxy-reason')).toHaveTextContent('Експорт використовує оригінал');
  });

  it('polls once a second with progress, then swaps the source to the ready proxy', async () => {
    let reads = 0;
    const { call } = await openMoments(makeSource({ playback: NEEDS_PROXY }), {
      'previews.create': () => CREATED('queued'),
      'previews.get': () => {
        reads += 1;
        return ok(previewOf(reads < 2 ? 'running' : 'ready'));
      },
      'jobs.get': () => ok(jobOf(42)),
    });
    await screen.findByTestId('proxy-progress');
    expect(call).not.toHaveBeenCalledWith('previews.get', expect.anything() as never);

    await waitFor(() => {
      expect(screen.getByTestId('proxy-progress')).toHaveTextContent('42%');
    }, { timeout: 3000 });
    expect(call).toHaveBeenCalledWith('previews.get', { id: PREVIEW_ID });
    expect(call).toHaveBeenCalledWith('jobs.get', { id: JOB_ID });

    await waitFor(() => {
      expect(video()).toHaveAttribute('src', `app-media://preview/${PREVIEW_ID}`);
    }, { timeout: 4000 });
    expect(screen.queryByTestId('proxy-progress')).not.toBeInTheDocument();
    expect(reads).toBe(2);
    expect(screen.getByTestId('proxy-reason')).toBeVisible();
  }, 10_000);

  it('uses a ready preview at once without polling', async () => {
    const { call } = await openMoments(makeSource({ playback: NEEDS_PROXY }), {
      'previews.create': () => CREATED('ready'),
    });

    await waitFor(() => {
      expect(video()).toHaveAttribute('src', `app-media://preview/${PREVIEW_ID}`);
    });
    expect(call).not.toHaveBeenCalledWith('previews.get', expect.anything() as never);
  });

  it('falls back to the proxy when the direct <video> fails, and keeps the position', async () => {
    const { call } = await openMoments(makeSource(), {
      'previews.create': () => CREATED('ready'),
    });
    const direct = loadVideo(3.5);
    fireEvent.timeUpdate(direct);

    fireEvent.error(direct);

    await waitFor(() => {
      expect(video()).toHaveAttribute('src', `app-media://preview/${PREVIEW_ID}`);
    });
    expect(paramsOf(call, 'previews.create')).toEqual([
      { source_id: SOURCE_ID, audio_stream_index: 1 },
    ]);
    expect(screen.getByTestId('player')).toHaveAttribute('data-mode', 'proxy');
    expect(screen.getByTestId('proxy-reason')).toHaveTextContent('не вдалося відтворити напряму');

    const proxy = video();
    setPlayhead(proxy, 0);
    fireEvent.loadedMetadata(proxy);
    expect(proxy.currentTime).toBe(3.5);
  });

  it('shows the error of a failed proxy and starts again on retry', async () => {
    let creates = 0;
    const { call } = await openMoments(makeSource({ playback: NEEDS_PROXY }), {
      'previews.create': () => {
        creates += 1;
        return CREATED(creates === 1 ? 'queued' : 'ready');
      },
      'previews.get': () =>
        ok(
          previewOf('failed', {
            error: { code: 'PREVIEW_FAILED', message: 'Не вдалося підготувати перегляд: файл пошкоджено' },
          }),
        ),
    });

    const alert = await screen.findByTestId('proxy-error', {}, { timeout: 3000 });
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert).toHaveTextContent('Не вдалося підготувати версію для перегляду');
    expect(alert).toHaveTextContent('файл пошкоджено');
    expect(screen.queryByTestId('player-video')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('proxy-retry'));

    await waitFor(() => {
      expect(video()).toHaveAttribute('src', `app-media://preview/${PREVIEW_ID}`);
    });
    expect(paramsOf(call, 'previews.create')).toHaveLength(2);
    expect(screen.queryByTestId('proxy-error')).not.toBeInTheDocument();
  }, 10_000);

  it('reports a backend that refuses to create the preview', async () => {
    await openMoments(makeSource({ playback: NEEDS_PROXY }), {
      'previews.create': () => failure('MEDIA_TOOLS_UNAVAILABLE', 'FFmpeg недоступний'),
    });

    expect(await screen.findByTestId('proxy-error')).toHaveTextContent('FFmpeg недоступний');
    expect(screen.getByTestId('proxy-retry')).toBeEnabled();
  });

  it('shows an error and a retry when the proxy itself cannot be played', async () => {
    await openMoments(makeSource({ playback: NEEDS_PROXY }), {
      'previews.create': () => CREATED('ready'),
    });
    await waitFor(() => {
      expect(video()).toHaveAttribute('src', `app-media://preview/${PREVIEW_ID}`);
    });

    fireEvent.error(video());

    expect(await screen.findByTestId('proxy-error')).toHaveTextContent(
      'Не вдалося відтворити версію для перегляду',
    );
    expect(screen.queryByTestId('player-video')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('proxy-retry'));
    await waitFor(() => {
      expect(video()).toHaveAttribute('src', `app-media://preview/${PREVIEW_ID}`);
    });
  });

  it('cancels the preparation through the job and then offers to try again', async () => {
    let cancelled = false;
    const { call } = await openMoments(makeSource({ playback: NEEDS_PROXY }), {
      'previews.create': () => CREATED('queued'),
      'previews.get': () => ok(previewOf(cancelled ? 'cancelled' : 'running')),
      'jobs.get': () => ok(jobOf(5)),
      'jobs.cancel': () => {
        cancelled = true;
        return ok({ ...jobOf(5), state: 'cancelling' });
      },
    });
    await screen.findByTestId('proxy-progress');
    await waitFor(() => {
      expect(screen.getByTestId('proxy-cancel')).toBeEnabled();
    });

    fireEvent.click(screen.getByTestId('proxy-cancel'));

    expect(call).toHaveBeenCalledWith('jobs.cancel', { id: JOB_ID });
    expect(screen.getByTestId('proxy-progress')).toHaveTextContent('Скасовуємо підготовку');
    expect(screen.getByTestId('proxy-cancel')).toBeDisabled();
    expect(await screen.findByTestId('proxy-cancelled', {}, { timeout: 3000 })).toHaveTextContent(
      'скасовано',
    );
    expect(screen.getByTestId('proxy-retry')).toBeEnabled();
  }, 10_000);

  it('stops polling when the tab is left', async () => {
    let reads = 0;
    const { call } = await openMoments(makeSource({ playback: NEEDS_PROXY }), {
      'previews.create': () => CREATED('queued'),
      'previews.get': () => {
        reads += 1;
        return ok(previewOf('running'));
      },
      'jobs.get': () => ok(jobOf(1)),
    });
    await waitFor(() => {
      expect(reads).toBeGreaterThan(0);
    }, { timeout: 3000 });

    fireEvent.click(screen.getByRole('tab', { name: 'Джерела' }));
    await screen.findByTestId('source-ready');
    const readsOnLeaving = paramsOf(call, 'previews.get').length;
    await new Promise((resolve) => setTimeout(resolve, 1500));

    expect(paramsOf(call, 'previews.get')).toHaveLength(readsOnLeaving);
  }, 10_000);

  it('does not poll while the page is hidden and resumes when it is visible again', async () => {
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    const { call } = await openMoments(makeSource({ playback: NEEDS_PROXY }), {
      'previews.create': () => CREATED('queued'),
      'previews.get': () => ok(previewOf('running')),
      'jobs.get': () => ok(jobOf(1)),
    });
    await screen.findByTestId('proxy-progress');

    await new Promise((resolve) => setTimeout(resolve, 1600));
    expect(call).not.toHaveBeenCalledWith('previews.get', expect.anything() as never);

    visibility.mockReturnValue('visible');
    await waitFor(() => {
      expect(call).toHaveBeenCalledWith('previews.get', { id: PREVIEW_ID });
    }, { timeout: 3000 });
  }, 10_000);
});

describe('Моменти: player controls', () => {
  it('is not controllable until the video has loaded, and then plays and pauses', async () => {
    await openMoments(makeSource());
    expect(screen.getByTestId('player-play')).toBeDisabled();

    const element = loadVideo();
    expect(screen.getByTestId('player-play')).toBeEnabled();
    expect(screen.getByTestId('player-play')).toHaveTextContent('Грати');

    fireEvent.click(screen.getByTestId('player-play'));
    expect(media.play).toHaveBeenCalledTimes(1);
    fireEvent.play(element);
    expect(screen.getByTestId('player-play')).toHaveTextContent('Пауза');

    Object.defineProperty(element, 'paused', { configurable: true, value: false });
    fireEvent.click(screen.getByTestId('player-play'));
    expect(media.pause).toHaveBeenCalledTimes(1);
  });

  it('shows the position and the duration as HH:MM:SS.mmm', async () => {
    await openMoments(makeSource());
    expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:00.000 / 00:00:12.000');

    const element = loadVideo();
    setPlayhead(element, 4.12);
    fireEvent.timeUpdate(element);

    expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:04.120 / 00:00:12.000');
  });

  it('seeks with the position slider', async () => {
    await openMoments(makeSource());
    const element = loadVideo();

    fireEvent.change(screen.getByTestId('player-seek'), { target: { value: '7880' } });

    expect(element.currentTime).toBe(7.88);
    expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:07.880');
  });

  it('offers the speeds 0.5, 1, 1.5 and 2 and applies the chosen one', async () => {
    await openMoments(makeSource());
    const element = loadVideo();
    const speed = screen.getByTestId('player-speed');

    expect(within(speed).getAllByRole('option').map((option) => option.textContent)).toEqual([
      '0.5×',
      '1×',
      '1.5×',
      '2×',
    ]);
    expect(speed).toHaveValue('1');
    fireEvent.change(speed, { target: { value: '1.5' } });

    expect(element.playbackRate).toBe(1.5);
  });

  it('sets the volume', async () => {
    await openMoments(makeSource());
    const element = loadVideo();

    fireEvent.change(screen.getByTestId('player-volume'), { target: { value: '0.5' } });

    expect(element.volume).toBe(0.5);
  });

  it('asks the player area, controls included, to go fullscreen', async () => {
    Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: true });
    const request = vi.fn(() => Promise.resolve());
    Object.defineProperty(HTMLElement.prototype, 'requestFullscreen', {
      configurable: true,
      value: request,
    });
    try {
      await openMoments(makeSource());

      fireEvent.click(screen.getByTestId('player-fullscreen'));

      expect(request).toHaveBeenCalledTimes(1);
      expect(request.mock.contexts[0]).toBe(screen.getByTestId('player'));
    } finally {
      Reflect.deleteProperty(document, 'fullscreenEnabled');
      Reflect.deleteProperty(HTMLElement.prototype, 'requestFullscreen');
    }
  });
});

describe('Моменти: manual clip form', () => {
  it('is usable before the video has loaded: times can be typed and saved', async () => {
    const { call } = await openManualForm(makeSource());
    expect(screen.queryByTestId('player-video')).toBeInTheDocument();

    expect(screen.getByTestId('clip-start-here')).toBeDisabled();
    expect(screen.getByTestId('clip-end-here')).toBeDisabled();
    expect(screen.getByTestId('preview-clip')).toBeDisabled();
    fillInterval('00:00:04.120', '00:00:07.880');
    expect(screen.getByTestId('save-clip')).toBeEnabled();
    fireEvent.click(screen.getByTestId('save-clip'));

    await screen.findByTestId('clip-saved');
    expect(call).toHaveBeenCalledWith('clips.create', expect.objectContaining({ start_ms: 4120 }) as never);
  });

  it('saves the typed text as it is, without a blur: clip 4120-7880', async () => {
    const { call } = await openManualForm(makeSource());

    fill('clip-title', 'Мій фрагмент');
    fillInterval('00:00:04.120', '00:00:07.880');
    // No blur anywhere: the click is the first thing that happens after typing.
    fireEvent.click(screen.getByTestId('save-clip'));

    const card = await screen.findByTestId('clip-card');
    expect(paramsOf(call, 'clips.create')).toEqual([
      {
        source_id: SOURCE_ID,
        title: 'Мій фрагмент',
        start_ms: 4120,
        end_ms: 7880,
        categories: [],
      },
    ]);
    expect(card).toHaveAttribute('data-clip-id');
    expect(card).toHaveTextContent('Мій фрагмент');
    expect(card).toHaveTextContent('00:00:03.760 · 00:00:04.120–00:00:07.880');
    expect(screen.getByTestId('clip-saved')).toHaveTextContent('Фрагмент «Мій фрагмент» збережено');
    // Ready for the next clip.
    expect(screen.getByTestId('clip-title')).toHaveValue('');
    expect(screen.getByTestId('clip-start')).toHaveValue('');
    expect(screen.getByTestId('clip-end')).toHaveValue('');
    expect(screen.getByTestId('save-clip')).toBeDisabled();
  });

  it('names a clip without a title after its interval', async () => {
    const { call } = await openManualForm(makeSource());

    fillInterval('00:00:04.120', '00:00:07.880');
    fireEvent.click(screen.getByTestId('save-clip'));

    await screen.findByTestId('clip-card');
    expect(call).toHaveBeenCalledWith(
      'clips.create',
      expect.objectContaining({ title: 'Фрагмент 00:00:04.120–00:00:07.880' }) as never,
    );
  });

  it('keeps the form and shows the backend refusal when saving fails', async () => {
    await openManualForm(makeSource(), {
      'clips.create': () => failure('INVALID_INTERVAL', 'Кінець виходить за межі відео'),
    });
    fillInterval('00:00:04.120', '00:00:07.880');

    fireEvent.click(screen.getByTestId('save-clip'));

    expect(await screen.findByTestId('save-clip-error')).toHaveTextContent('Кінець виходить за межі відео');
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:04.120');
    expect(screen.getByTestId('save-clip')).toBeEnabled();
    expect(screen.queryByTestId('clip-card')).not.toBeInTheDocument();
  });

  it('shows interval-error for a reversed interval and keeps save-clip disabled', async () => {
    const { call } = await openManualForm(makeSource());

    fillInterval('00:00:08.000', '00:00:04.000');
    expect(screen.getByTestId('save-clip')).toBeDisabled();
    expect(screen.queryByTestId('interval-error')).not.toBeInTheDocument();
    fireEvent.blur(screen.getByTestId('clip-end'));

    const error = screen.getByTestId('interval-error');
    expect(error).toBeVisible();
    expect(error).toHaveTextContent('Кінець має бути пізніше за початок');
    expect(screen.getByTestId('save-clip')).toBeDisabled();
    expect(screen.getByTestId('clip-end')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByTestId('clip-end')).toHaveAccessibleDescription(/Кінець має бути пізніше/);
    fireEvent.click(screen.getByTestId('save-clip'));
    expect(call).not.toHaveBeenCalledWith('clips.create', expect.anything() as never);

    fill('clip-end', '00:00:09.000');
    expect(screen.queryByTestId('interval-error')).not.toBeInTheDocument();
    expect(screen.getByTestId('save-clip')).toBeEnabled();
  });

  it('shows interval-error when the end equals the start', async () => {
    await openManualForm(makeSource());

    fillInterval('00:00:02.000', '00:00:02.000');
    fireEvent.blur(screen.getByTestId('clip-end'));

    expect(screen.getByTestId('interval-error')).toHaveTextContent('Кінець має бути пізніше за початок');
    expect(screen.getByTestId('save-clip')).toBeDisabled();
  });

  it('shows interval-error outside [0, duration] and says where the video ends', async () => {
    await openManualForm(makeSource());

    fillInterval('00:00:04.000', '00:00:12.001');
    fireEvent.blur(screen.getByTestId('clip-end'));

    expect(screen.getByTestId('interval-error')).toHaveTextContent('до 00:00:12.000');
    expect(screen.getByTestId('save-clip')).toBeDisabled();
  });

  it('shows interval-error for text that is not a time', async () => {
    await openManualForm(makeSource());

    fillInterval('abc', '00:00:04.000');
    fireEvent.blur(screen.getByTestId('clip-start'));

    expect(screen.getByTestId('interval-error')).toHaveTextContent('Початок: введіть час у форматі');
    expect(screen.getByTestId('save-clip')).toBeDisabled();
  });

  it('does not scold an empty form that was only opened', async () => {
    await openManualForm(makeSource());

    expect(screen.queryByTestId('interval-error')).not.toBeInTheDocument();
    expect(screen.getByTestId('save-clip')).toBeDisabled();
  });

  it('moves a boundary by a second and keeps it inside the video', async () => {
    await openManualForm(makeSource());
    expect(screen.getByTestId('clip-start-plus')).toBeDisabled();

    fillInterval('00:00:04.120', '00:00:11.500');
    fireEvent.click(screen.getByTestId('clip-start-plus'));
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:05.120');
    fireEvent.click(screen.getByTestId('clip-start-minus'));
    fireEvent.click(screen.getByTestId('clip-start-minus'));
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:03.120');
    fireEvent.click(screen.getByTestId('clip-end-plus'));
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:12.000');
    fill('clip-start', '00:00:00.400');
    fireEvent.click(screen.getByTestId('clip-start-minus'));
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:00.000');
  });

  it('takes a boundary from the player: canonical ms = round(currentTime * 1000)', async () => {
    const { call } = await openManualForm(makeSource());
    const element = loadVideo();

    setPlayhead(element, 4.12);
    fireEvent.click(screen.getByTestId('clip-start-here'));
    setPlayhead(element, 7.8804);
    fireEvent.click(screen.getByTestId('clip-end-here'));

    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:04.120');
    expect(screen.getByTestId('clip-end')).toHaveValue('00:00:07.880');
    fireEvent.click(screen.getByTestId('save-clip'));
    await screen.findByTestId('clip-card');
    expect(call).toHaveBeenCalledWith(
      'clips.create',
      expect.objectContaining({ start_ms: 4120, end_ms: 7880 }) as never,
    );
  });

  it('plays the fragment from its start and pauses after its end', async () => {
    await openManualForm(makeSource());
    const element = loadVideo();
    fillInterval('00:00:04.120', '00:00:07.880');

    fireEvent.click(screen.getByTestId('preview-clip'));

    expect(element.currentTime).toBe(4.12);
    expect(media.play).toHaveBeenCalledTimes(1);
    expect(media.pause).not.toHaveBeenCalled();

    setPlayhead(element, 7.9);
    await waitFor(() => {
      expect(media.pause).toHaveBeenCalledTimes(1);
    });
    expect(element.currentTime).toBe(7.88);
    expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:07.880');
  });

  it('does not play a fragment whose interval is invalid', async () => {
    await openManualForm(makeSource());
    loadVideo();

    fillInterval('00:00:08.000', '00:00:04.000');

    expect(screen.getByTestId('preview-clip')).toBeDisabled();
  });

  it('keeps what is typed in the title: no shortcut takes over letters like «air o»', async () => {
    await openManualForm(makeSource());
    const element = loadVideo();
    const title = screen.getByTestId('clip-title');

    fill('clip-title', '');
    let typed = '';
    for (const key of 'air o') {
      fireEvent.keyDown(title, { key });
      typed += key;
      fireEvent.change(title, { target: { value: typed } });
      fireEvent.keyUp(title, { key });
    }

    expect(title).toHaveValue('air o');
    expect(title).toHaveFocus();
    expect(screen.getByTestId('clip-start')).toHaveValue('');
    expect(screen.getByTestId('clip-end')).toHaveValue('');
    expect(media.play).not.toHaveBeenCalled();
    expect(media.pause).not.toHaveBeenCalled();
    expect(element.currentTime).toBe(0);
  });
});

describe('Моменти: clips of the source', () => {
  it('loads the clips of the source and shows a card with times and review status', async () => {
    const { handlers } = clipBackend([makeClip()]);
    const { call } = await openMoments(makeSource(), handlers);

    const card = await screen.findByTestId('clip-card');
    expect(call).toHaveBeenCalledWith('clips.list', { source_id: SOURCE_ID });
    expect(card).toHaveAttribute('data-clip-id', makeClip().id);
    expect(within(card).getByRole('heading', { name: 'Мій фрагмент' })).toBeVisible();
    expect(card).toHaveTextContent('00:00:03.760 · 00:00:04.120–00:00:07.880');
    expect(within(card).getByTestId('clip-review-status')).toHaveTextContent('На розгляді');
    expect(within(card).getByTestId('select-clip')).toHaveAttribute('data-clip-id', makeClip().id);
    expect(within(card).getByTestId('select-clip')).not.toBeChecked();
  });

  it('says there are no clips yet and what to do', async () => {
    await openMoments(makeSource());

    expect(await screen.findByTestId('empty-candidates')).toHaveTextContent('Фрагментів ще немає');
    expect(screen.queryByTestId('clip-card')).not.toBeInTheDocument();
  });

  it('shows a loading status, then an error with a retry that can succeed', async () => {
    let attempts = 0;
    const { call } = await openMoments(makeSource(), {
      'clips.list': () => {
        attempts += 1;
        return attempts === 1
          ? failure('INTERNAL_ERROR', 'Внутрішня помилка', true)
          : ok({ items: [makeClip()], next_cursor: null });
      },
    });

    const alert = await screen.findByTestId('clips-error');
    expect(alert).toHaveTextContent('Не вдалося завантажити фрагменти');
    expect(alert).toHaveTextContent('Внутрішня помилка');

    fireEvent.click(screen.getByTestId('clips-retry'));

    expect(await screen.findByTestId('clip-card')).toBeVisible();
    expect(screen.queryByTestId('clips-error')).not.toBeInTheDocument();
    expect(paramsOf(call, 'clips.list')).toHaveLength(2);
  });

  it('reads every page of the list', async () => {
    const second = makeClip({ id: '6d8f0a2c-4e5a-4b7c-9d9e-0f1a2b3c4d5e', title: 'Другий фрагмент' });
    const { call } = await openMoments(makeSource(), {
      'clips.list': (params) =>
        params.cursor === undefined
          ? ok({ items: [makeClip()], next_cursor: 'page-2' })
          : ok({ items: [second], next_cursor: null }),
    });

    await waitFor(() => {
      expect(screen.getAllByTestId('clip-card')).toHaveLength(2);
    });
    expect(call).toHaveBeenCalledWith('clips.list', { source_id: SOURCE_ID, cursor: 'page-2' });
  });

  it('approves a clip with its revision, then the clip can be selected: two separate decisions', async () => {
    const { handlers } = clipBackend([makeClip()]);
    const { call } = await openMoments(makeSource(), handlers);
    const card = await screen.findByTestId('clip-card');

    fireEvent.click(within(card).getByTestId('approve-clip'));

    await waitFor(() => {
      expect(within(card).getByTestId('clip-review-status')).toHaveTextContent('Підтверджено');
    });
    expect(call).toHaveBeenCalledWith('clips.review', {
      id: makeClip().id,
      expected_revision: 1,
      status: 'approved',
    });
    expect(within(card).queryByTestId('approve-clip')).not.toBeInTheDocument();
    expect(within(card).getByRole('button', { name: 'Повернути до розгляду' })).toBeVisible();
    // Approving did not select the clip for export.
    expect(within(card).getByTestId('select-clip')).not.toBeChecked();

    fireEvent.click(within(card).getByTestId('select-clip'));

    expect(within(card).getByTestId('select-clip')).toBeChecked();
    // Selecting did not review it again.
    expect(paramsOf(call, 'clips.review')).toHaveLength(1);
    expect(within(card).getByTestId('clip-review-status')).toHaveTextContent('Підтверджено');
  });

  it('keeps the selection while the review status changes', async () => {
    const { handlers } = clipBackend([makeClip()]);
    const { call } = await openMoments(makeSource(), handlers);
    const card = await screen.findByTestId('clip-card');
    fireEvent.click(within(card).getByTestId('select-clip'));

    fireEvent.click(within(card).getByTestId('approve-clip'));
    await within(card).findByRole('button', { name: 'Повернути до розгляду' });
    fireEvent.click(within(card).getByRole('button', { name: 'Повернути до розгляду' }));
    await within(card).findByTestId('approve-clip');

    expect(within(card).getByTestId('select-clip')).toBeChecked();
    expect(within(card).getByTestId('clip-review-status')).toHaveTextContent('На розгляді');
    expect(call).toHaveBeenLastCalledWith('clips.review', {
      id: makeClip().id,
      expected_revision: 2,
      status: 'suggested',
    });
  });

  it('rejects a clip and returns it to review', async () => {
    const { handlers } = clipBackend([makeClip()]);
    const { call } = await openMoments(makeSource(), handlers);
    const card = await screen.findByTestId('clip-card');

    fireEvent.click(within(card).getByTestId('reject-clip'));

    await waitFor(() => {
      expect(within(card).getByTestId('clip-review-status')).toHaveTextContent('Відхилено');
    });
    expect(call).toHaveBeenCalledWith('clips.review', {
      id: makeClip().id,
      expected_revision: 1,
      status: 'rejected',
    });
    expect(within(card).queryByTestId('approve-clip')).not.toBeInTheDocument();

    fireEvent.click(within(card).getByRole('button', { name: 'Повернути до розгляду' }));

    await waitFor(() => {
      expect(within(card).getByTestId('clip-review-status')).toHaveTextContent('На розгляді');
    });
  });

  it('blocks a second click while the review is in flight', async () => {
    const { call } = await openMoments(makeSource(), {
      'clips.list': () => ok({ items: [makeClip()], next_cursor: null }),
      'clips.review': () => new Promise<never>(() => undefined),
    });
    const card = await screen.findByTestId('clip-card');

    fireEvent.click(within(card).getByTestId('approve-clip'));
    fireEvent.click(within(card).getByTestId('approve-clip'));

    expect(within(card).getByTestId('approve-clip')).toBeDisabled();
    expect(within(card).getByTestId('reject-clip')).toBeDisabled();
    expect(paramsOf(call, 'clips.review')).toHaveLength(1);
  });

  it('shows a stale revision on the card and reloads the list', async () => {
    let reads = 0;
    const { call } = await openMoments(makeSource(), {
      'clips.list': () => {
        reads += 1;
        return ok({
          items: [makeClip({ revision: reads === 1 ? 1 : 2, title: reads === 1 ? 'Старий' : 'Новий' })],
          next_cursor: null,
        });
      },
      'clips.review': () =>
        failure('STALE_REVISION', 'Кліп змінено в іншому місці. Оновіть дані та повторіть'),
    });
    const card = await screen.findByTestId('clip-card');

    fireEvent.click(within(card).getByTestId('approve-clip'));

    await waitFor(() => {
      expect(screen.getByTestId('clip-card')).toHaveTextContent('Новий');
    });
    expect(screen.getByTestId('clip-error')).toHaveTextContent('Кліп змінено в іншому місці');
    expect(paramsOf(call, 'clips.list')).toHaveLength(2);
  });

  it('keeps the export selection when the user switches tabs and back', async () => {
    const { handlers } = clipBackend([makeClip()]);
    await openMoments(makeSource(), handlers);
    fireEvent.click(within(await screen.findByTestId('clip-card')).getByTestId('select-clip'));

    fireEvent.click(screen.getByRole('tab', { name: 'Джерела' }));
    await screen.findByTestId('source-ready');
    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));

    expect(within(await screen.findByTestId('clip-card')).getByTestId('select-clip')).toBeChecked();
  });

  it('lists the clips of the source that is picked, not of the others', async () => {
    const second = makeSource({ id: SECOND_SOURCE_ID, display_name: 'two_tracks.mkv' });
    const sources = [makeSource(), second];
    const secondClip = makeClip({
      id: '6d8f0a2c-4e5a-4b7c-9d9e-0f1a2b3c4d5e',
      source_id: SECOND_SOURCE_ID,
      title: 'З другого джерела',
    });
    const { call } = installBridge({
      importVideo: () => ok(sources.shift() ?? second),
      handlers: {
        'projects.list': listOf(makeProject()),
        'clips.list': (params) =>
          ok({
            items: [makeClip(), secondClip].filter((clip) => clip.source_id === params.source_id),
            next_cursor: null,
          }),
      },
    });
    render(<App />);
    fireEvent.click(await screen.findByTestId('project-item'));
    fireEvent.click(await screen.findByTestId('import-video'));
    await screen.findByTestId('source-ready');
    fireEvent.click(screen.getByTestId('import-video'));
    await screen.findByRole('list', { name: 'Джерела проєкту' });
    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));

    // The source added last is the current one.
    expect(await screen.findByText('З другого джерела')).toBeVisible();
    expect(screen.getByTestId('clips-source')).toHaveValue(SECOND_SOURCE_ID);

    fireEvent.change(screen.getByTestId('clips-source'), { target: { value: SOURCE_ID } });

    expect(await screen.findByText('Мій фрагмент')).toBeVisible();
    expect(screen.queryByText('З другого джерела')).not.toBeInTheDocument();
    expect(call).toHaveBeenCalledWith('clips.list', { source_id: SOURCE_ID });
  });
});
